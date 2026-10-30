#!/usr/bin/env python3
"""Audit in-text callouts to Figures, Panels, Supplementary Figures, Tables, Videos, and Methods subsections.

Usage:
  callout_audit.py TEXTFILE

Works on plain or `--indexed` output (`[P12] ...`, `[T1] ...`) from `docx2txt.py`.
Supports both end-of-manuscript Figure Legend sections AND inline/table-wrapped legends
(e.g. `[[IMAGE:image3.png]]Figure 3: ...` or `[T2] [[IMAGE:img.png]] | Figure 3. ...`).

Reports:
  1. Inventory of defined Figures, Supplementary Figures, Tables, and Videos + their panels (A, B, C...)
  2. Uncited figures, panels, tables, or videos
  3. Phantom callouts (cited in text, but no matching figure/panel/table/video in legends)
  4. Out-of-order first callouts (e.g. Figure 4D cited before 4C) & overlapping adjacent callouts
  5. Whole-figure callouts to multi-panel figures (where a specific panel letter should be given)
  6. Bidirectional Materials & Methods Coverage:
     - Orphan Methods subsections (methods described that have no corresponding data in Results/Legends)
     - Figure/Results assays or reagents missing from Materials & Methods
"""
import sys
import os
import re
from collections import OrderedDict, defaultdict


def strip_ptag(line):
    m = re.match(r'^\s*(\[(?:P\d+(?:\.\d+)?|T\d+|TABLE\s+\d+|Row\s+\d+|[^\]:]+:(?:P|L)\d+(?:\.\d+)?)\])\s*(.*)', line)
    if m:
        return m.group(1), m.group(2)
    return '', line


def strip_image_tags(text):
    return re.sub(r'\[\[IMAGE:[^\]]+\]\]\s*', '', text).strip()


def _filter_sequential_panels(candidates):
    """Keep panel letters only if they form a sequential alphabetical series starting at 'A'
    (prevents isolated math/unit symbols like '(N)' or '(V)' from being mistaken for panel letters)."""
    uniq = []
    for c in candidates:
        cu = c.upper()
        if 'A' <= cu <= 'Z' and cu not in uniq:
            uniq.append(cu)
    if 'A' not in uniq:
        return []
    ordered = sorted(uniq)
    kept = ['A']
    for ch in ordered[1:]:
        # Allow at most a 1-letter gap (e.g. if a legend accidentally omits one panel letter)
        if ord(ch) - ord(kept[-1]) <= 2:
            kept.append(ch)
        else:
            break
    return [c for c in uniq if c in kept]


def parse_panel_letters(legend_text):
    """Extract panel letters defined via (A), (B,C), (A-D), A), B) or lowercase (a), (b,c), (a-d) in a figure legend."""
    upper_raw = []
    for m in re.finditer(r'(?:\(([A-Z](?:\s*[,;\u2013\u2014-]\s*[A-Z])*)\)|(?:^|(?<=[.?!:;]\s))([A-H])\)(?=\s+[A-Z0-9]))', legend_text):
        expr = m.group(1) or m.group(2)
        for tok in re.split(r'\s*[,;]\s*', expr):
            if any(d in tok for d in ('\u2013', '\u2014', '-')):
                a, b = re.split(r'\s*[\u2013\u2014-]\s*', tok)
                if len(a) == 1 and len(b) == 1 and 'A' <= a <= b <= 'Z':
                    for code in range(ord(a), ord(b) + 1):
                        upper_raw.append(chr(code))
            elif len(tok) == 1 and 'A' <= tok <= 'Z':
                upper_raw.append(tok)

    upper_filtered = _filter_sequential_panels(upper_raw)

    lower_raw = []
    for m in re.finditer(r'(?:\(([a-n](?:\s*[,;\u2013\u2014-]\s*[a-n])*)\)|(?:^|(?<=[.?!:;]\s))([a-h])\)(?=\s+[A-Za-z0-9]))', legend_text):
        expr = m.group(1) or m.group(2)
        for tok in re.split(r'\s*[,;]\s*', expr):
            if any(d in tok for d in ('\u2013', '\u2014', '-')):
                a, b = re.split(r'\s*[\u2013\u2014-]\s*', tok)
                if len(a) == 1 and len(b) == 1 and 'a' <= a <= b <= 'n':
                    for code in range(ord(a), ord(b) + 1):
                        lower_raw.append(chr(code).upper())
            elif len(tok) == 1 and 'a' <= tok <= 'n':
                lower_raw.append(tok.upper())

    lower_filtered = _filter_sequential_panels(lower_raw)
    if len(lower_filtered) > len(upper_filtered):
        return lower_filtered
    return upper_filtered


def expand_callout_expr(prefix, expr, is_supp=False, supp_prefix='S'):
    """Parse callout expressions like '4C, S4A', 'EV1A-C', '6-9', '1 D-E', '5A,B and 6B,C' into [(fig_id, panel_or_None)]."""
    results = []
    # Preserve 'and a <word>' so English article 'a' is not mistaken for panel A
    expr = re.sub(r'\b(?:and|or)\s+a\s+[a-z]{2,}\b', '', expr)
    expr = re.sub(r'\b(?:and|or)\b', ',', expr, flags=re.I)
    # Collapse internal space between number and panel letter/range: e.g. "1 D-E" -> "1D-E", "2 A" -> "2A"
    expr = re.sub(r'\b((?:EV|ED|S)?\d+)\s+([A-Ja-j](?:\s*[\u2013\u2014-]\s*[A-Ja-j])?)\b(?!\s+[a-z]{2,})', r'\1\2', expr)
    tokens = [t.strip() for t in re.split(r'[,;]', expr) if t.strip()]
    cur_fig = None
    for tok in tokens:
        tok_clean = re.sub(r'\b(?:left|right|top|bottom|upper|lower|middle|inset|panel|panels)\b', '', tok, flags=re.I).strip()
        if not tok_clean:
            continue
        # Range of item numbers (without panel letters): e.g. "6-9", "S6-S9", "EV1-EV5"
        m_num_range = re.match(r'^((?:EV|ED|S)?)(\d+)\s*[\u2013\u2014-]\s*((?:EV|ED|S)?)(\d+)$', tok_clean, re.I)
        if m_num_range:
            p1 = (m_num_range.group(1) or m_num_range.group(3) or (supp_prefix if is_supp else '')).upper()
            n1, n2 = int(m_num_range.group(2)), int(m_num_range.group(4))
            if n1 <= n2 and (n2 - n1) <= 30:
                for n in range(n1, n2 + 1):
                    cur_fig = f"{prefix} {p1}{n}"
                    results.append((cur_fig, None))
            continue
        # Range of panels on a figure: e.g. "1A-C", "1a-c", "S2A-B", "EV1A-D"
        m_range = re.match(r'^((?:EV|ED|S)?\d+)\s*([A-Za-z])\s*[\u2013\u2014-]\s*(?:(?:EV|ED|S)?\d+\s*)?([A-Za-z])$', tok_clean, re.I)
        if m_range:
            fnum = m_range.group(1).upper()
            if is_supp and not fnum.startswith(('S', 'EV', 'ED')):
                fnum = supp_prefix + fnum
            cur_fig = f"{prefix} {fnum}"
            p_start, p_end = m_range.group(2).upper(), m_range.group(3).upper()
            if p_start <= p_end and (ord(p_end) - ord(p_start)) <= 15:
                for c in range(ord(p_start), ord(p_end) + 1):
                    results.append((cur_fig, chr(c)))
            continue
        # Range of panel letters alone: e.g. "B-D" or "b-d" after "Figure 1A"
        m_prange = re.match(r'^([A-Za-z])\s*[\u2013\u2014-]\s*([A-Za-z])$', tok_clean)
        if m_prange and cur_fig:
            p_start, p_end = m_prange.group(1).upper(), m_prange.group(2).upper()
            if p_start <= p_end and (ord(p_end) - ord(p_start)) <= 15:
                for c in range(ord(p_start), ord(p_end) + 1):
                    results.append((cur_fig, chr(c)))
            continue
        # Single figure + optional panel: e.g. "4C", "2a", "S4A", "EV1B", "3"
        m_single = re.match(r'^((?:EV|ED|S)?\d+)([A-Za-z])?$', tok_clean, re.I)
        if m_single:
            fig_num = m_single.group(1).upper()
            if is_supp and not fig_num.startswith(('S', 'EV', 'ED')):
                fig_num = supp_prefix + fig_num
            pan = m_single.group(2).upper() if m_single.group(2) else None
            cur_fig = f"{prefix} {fig_num}"
            results.append((cur_fig, pan))
            continue
        # Bare panel letter continuing previous figure: e.g. "B" in "Figure 7B,C" or "b" in "Figure 2a, b"
        m_panel = re.match(r'^([A-Ja-j])$', tok_clean)
        if m_panel and cur_fig:
            results.append((cur_fig, m_panel.group(1).upper()))
            continue
    return results


def load_ignore_set():
    """Load user-specified ignore tokens from --ignore flag and/or scratch_audit/audit_ignore.txt."""
    ignore = set()
    if '--ignore' in sys.argv:
        idx = sys.argv.index('--ignore')
        if idx + 1 < len(sys.argv):
            val = sys.argv[idx + 1]
            if os.path.isfile(val):
                for ln in open(val, encoding='utf-8'):
                    t = ln.split('#')[0].strip().lower()
                    if t:
                        ignore.add(t)
            else:
                for tok in re.split(r'[,;]', val):
                    if tok.strip():
                        ignore.add(tok.strip().lower())
    for default_p in ('scratch_audit/audit_ignore.txt', 'audit_ignore.txt'):
        if os.path.isfile(default_p):
            for ln in open(default_p, encoding='utf-8'):
                t = ln.split('#')[0].strip().lower()
                if t:
                    ignore.add(t)
    return ignore


def _extract_docx_lines_simple(docx_path):
    """Extract plain-text lines from a companion .docx file (e.g., Movie_EV1_legend.docx)."""
    import zipfile
    import xml.etree.ElementTree as ET
    out_lines = []
    try:
        with zipfile.ZipFile(docx_path, "r") as zf:
            root = ET.fromstring(zf.read("word/document.xml"))
        w_p = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
        w_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
        for idx, p in enumerate(root.iter(w_p), 1):
            txt = "".join(t.text or "" for t in p.iter(w_t)).strip()
            if txt:
                out_lines.append(f"[{os.path.basename(docx_path)}:P{idx}] {txt}")
    except Exception:
        pass
    return out_lines


def _load_external_legend_lines(main_txt_path):
    """Discover and load lines from external movie/figure/table legend files (--legend-dir or auto-discovered)."""
    cand_dirs = []
    if "--legend-dir" in sys.argv:
        idx = sys.argv.index("--legend-dir")
        if idx + 1 < len(sys.argv):
            cand_dirs.append(sys.argv[idx + 1])
    for d in (
        "scratch_audit/videos/_unzipped_legends",
        os.path.join(os.path.dirname(os.path.abspath(main_txt_path)), "videos", "_unzipped_legends"),
        os.path.dirname(os.path.abspath(main_txt_path)),
        ".",
    ):
        if os.path.isdir(d) and d not in cand_dirs:
            cand_dirs.append(d)

    extra = []
    seen_files = set()
    for d in cand_dirs:
        is_dedicated_legend_dir = "_unzipped_legends" in d or ("--legend-dir" in sys.argv and d == cand_dirs[0])
        for fn in sorted(os.listdir(d)):
            full = os.path.join(d, fn)
            if not os.path.isfile(full) or os.path.abspath(full) == os.path.abspath(main_txt_path):
                continue
            if os.path.abspath(full) in seen_files:
                continue
            low = fn.lower()
            if not is_dedicated_legend_dir and not (
                ("legend" in low or "movie" in low or "video" in low) and low.endswith((".docx", ".txt"))
            ):
                continue
            seen_files.add(os.path.abspath(full))
            if low.endswith(".docx"):
                extra.extend(_extract_docx_lines_simple(full))
            elif low.endswith(".txt"):
                for idx, ln in enumerate(open(full, encoding="utf-8", errors="replace").read().splitlines(), 1):
                    if ln.strip() and not ln.startswith("#"):
                        extra.append(f"[{fn}:L{idx}] {ln.strip()}")
    return extra


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        sys.exit(__doc__)

    ignore_set = load_ignore_set()
    lines = [l for l in open(sys.argv[1], encoding='utf-8').read().splitlines() if not l.startswith('# MANUSCRIPT-AUDIT EXTRACT')]
    ext_legend_lines = _load_external_legend_lines(sys.argv[1])
    body_line_count = len(lines)
    if ext_legend_lines:
        lines.extend(ext_legend_lines)

    # 1. Identify Legend definitions vs Body lines
    defined_figs = OrderedDict()   # "Figure 1" -> {'ptag': ..., 'title': ..., 'panels': [...], 'text': ...}
    defined_tables = OrderedDict() # "Table 1" -> {'ptag': ..., 'title': ...}
    defined_videos = OrderedDict() # "Video S1" -> {'ptag': ..., 'title': ...}

    legend_line_indices = set(range(body_line_count, len(lines)))
    in_legends_section = False
    cur_tbl = None

    for i, raw in enumerate(lines):
        m_tbl_hdr = re.match(r'^\s*\[TABLE\s+(\d+)\]\s*$', raw)
        if m_tbl_hdr:
            cur_tbl = f"T{m_tbl_hdr.group(1)}"
            continue
        ptag, clean = strip_ptag(raw)
        if ptag.startswith('[Row') and cur_tbl:
            ptag = f"[{cur_tbl}, {ptag[1:-1]}]"
        clean_no_img = strip_image_tags(clean)

        if re.match(
            r'^\s*(?:main\s+|supplementary\s+|supplemental\s+|expanded\s+view\s+|extended\s+data\s+|video\s+|movie\s+)?'
            r'(?:figure\s+legends?|figures?|supplementary\s+figures?|expanded\s+view\s+figures?|video\s+legends?|movie\s+legends?)\s*:?\s*$',
            clean_no_img,
            re.I,
        ):
            in_legends_section = True
            legend_line_indices.add(i)
            continue

        # Split table rows (`[Row #] cell1 | cell2` or `cell1 / Figure 4: ...`) and inline image tags into candidate segments
        if ptag.startswith(('[T', '[TABLE', '[Row')) or '|' in clean or '[[IMAGE:' in clean:
            raw_segs = re.split(r'\s*\|\s*|\[\[IMAGE:[^\]]+\]\]|\s+/\s+(?=(?:Supplementary\s+|Expanded\s+View\s+)?(?:Figure|Fig\.?|Table|Video|Movie)\s+(?:EV|ED|S)?\d+)', clean)
            segments = [strip_image_tags(s) for s in raw_segs if strip_image_tags(s)]
        else:
            segments = [clean_no_img]

        for seg in segments:
            if not seg:
                continue

            # Check Table title definition
            m_tbl = re.match(r'^(?:(Supplementary|Supplemental|Expanded\s+View|Extended\s+Data)\s+)?Table\s+((?:EV|ED|S)?\d+)\s*([.:–—-])?\s*(.*)', seg, re.I)
            if m_tbl:
                has_sep = bool(m_tbl.group(3))
                rest_tbl = m_tbl.group(4) or ''
                is_prose_verb = bool(re.match(r'^(?:shows|summarizes|lists|presents|compares|is|was|in|from|and)\b', rest_tbl, re.I))
                if not is_prose_verb and (has_sep or len(seg) < 240 or not seg.endswith('.')):
                    tnum = m_tbl.group(2).upper()
                    mod = (m_tbl.group(1) or '').lower()
                    if mod and not tnum.startswith(('S', 'EV', 'ED')):
                        tnum = ('EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')) + tnum
                    tid = f"Table {tnum}"
                    defined_tables[tid] = {'ptag': ptag or f"L{i+1}", 'title': seg}
                    legend_line_indices.add(i)
                    continue

            # Check Video / Movie legend definition
            m_vid = re.match(r'^(?:(Supplementary|Supplemental|Expanded\s+View)\s+)?(?:Video|Movie)\s+((?:EV|ED|S)?\d+)\s*[.:–—-]?\s+(.+)', seg, re.I)
            if m_vid and (i >= body_line_count or re.search(r'[.:–—-]', seg[:30])):
                vnum = m_vid.group(2).upper()
                mod = (m_vid.group(1) or '').lower()
                if mod and not vnum.startswith(('S', 'EV', 'ED')):
                    vnum = ('EV' if 'expanded' in mod else 'S') + vnum
                vid = f"Video {vnum}"
                defined_videos[vid] = {'ptag': ptag or f"L{i+1}", 'title': seg}
                legend_line_indices.add(i)
                continue

            # Check Figure legend definition:
            # Matches `Figure 3. ...`, `Figure EV1: ...`, `Fig. S1: ...`, `Supplementary Figure 1. ...`, `Expanded View Figure 1. ...`
            m_fig = re.match(r'^(?:(Supplementary|Supplemental|Supp\.?|Expanded\s+View|Extended\s+Data)\s+)?(?:Figure|Fig\.?)\s+((?:EV|ED|S)?\d+)\s*[.:–—-]\s*(.+)', seg, re.I)
            if m_fig:
                fnum = m_fig.group(2).upper()
                mod = (m_fig.group(1) or '').lower()
                if mod and not fnum.startswith(('S', 'EV', 'ED')):
                    fnum = ('EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')) + fnum
                fid = f"Figure {fnum}"
                legend_line_indices.add(i)
                full_leg = seg
                # Check if subsequent lines are continuation paragraphs of this figure's legend
                k_next = i + 1
                while k_next < len(lines) and not ptag.startswith('[T'):
                    _, next_clean = strip_ptag(lines[k_next])
                    next_no_img = strip_image_tags(next_clean)
                    if not next_no_img:
                        k_next += 1
                        continue
                    if re.match(r'^(?:Supplementary\s+|Supplemental\s+|Supp\.?\s+|Expanded\s+View\s+|Extended\s+Data\s+)?(?:Figure|Fig\.?|Video|Movie|Table)\s+(?:EV|ED|S)?\d+\s*[.:–—-]', next_no_img, re.I):
                        break
                    if re.match(r'^\s*(?:references|bibliography|acknowledg(?:e)?ments?|materials?\s+and\s+methods|supplementary\s+(?:figures?|tables?|materials?|information)|expanded\s+view)\s*:?\s*$', next_no_img, re.I):
                        break
                    if re.match(r'^(?:\([A-Za-z]|[A-Ha-h]\)\s)', next_no_img) or in_legends_section:
                        full_leg += " " + next_no_img
                        legend_line_indices.add(k_next)
                        k_next += 1
                    else:
                        break
                panels = parse_panel_letters(full_leg)
                defined_figs[fid] = {
                    'ptag': ptag or f"L{i+1}",
                    'title': seg,
                    'panels': panels,
                    'text': full_leg,
                }

    print("== 1. DEFINED FIGURES, TABLES & VIDEOS INVENTORY ==")
    print(f"Defined Figures ({len(defined_figs)}):")
    for fid, info in defined_figs.items():
        p_str = ', '.join(info['panels']) if info['panels'] else '(no panel letters)'
        print(f"  - {fid:<10s} ({info['ptag']}): panels [{p_str}] | {info['title'][:85]}")
    print(f"Defined Tables ({len(defined_tables)}):")
    for tid, info in defined_tables.items():
        print(f"  - {tid:<10s} ({info['ptag']}): {info['title'][:85]}")
    print(f"Defined Videos ({len(defined_videos)}):")
    for vid, info in defined_videos.items():
        print(f"  - {vid:<10s} ({info['ptag']}): {info['title'][:85]}")

    # 2. Scan Body Lines for Callouts
    fig_callouts = []     # list of (ptag, fig_id, panel_or_None, raw_match)
    table_callouts = []   # list of (ptag, table_id, raw_match)
    video_callouts = []   # list of (ptag, video_id, raw_match)
    overlapping_callouts = []

    fig_pat = re.compile(
        r'\b(?:(Supplementary|Supplemental|Supp\.?|Expanded\s+View|Extended\s+Data)\s+)?(?:Figures?|Figs?\.?)\s+'
        r'((?:EV|ED|S)?\d+(?:\s*[A-Ja-j](?![a-z]))?(?:\s*(?:[,;\u2013\u2014-]|\band\b|\bor\b)\s*(?:(?:EV|ED|S)?\d+(?:\s*[A-Ja-j](?![a-z]))?|[A-Ja-j](?![a-z])|\b(?:left|right|top|bottom|inset)\b))*)',
        re.I,
    )
    tbl_pat = re.compile(
        r'\b(?:(Supplementary|Supplemental|Supp\.?|Expanded\s+View|Extended\s+Data)\s+)?Tables?\s+'
        r'((?:EV|ED|S)?\d+\b(?:\s*(?:[,;\u2013\u2014-]|\band\b|\bor\b)\s*(?:EV|ED|S)?\d+\b)*)',
        re.I,
    )
    vid_pat = re.compile(
        r'\b(?:(Supplementary|Supplemental|Supp\.?|Expanded\s+View)\s+)?(?:Videos?|Movies?)\s+'
        r'((?:EV|ED|S)?\d+\b(?:\s*(?:[,;\u2013\u2014-]|\band\b|\bor\b)\s*(?:EV|ED|S)?\d+\b)*)',
        re.I,
    )

    cur_tbl_body = None
    for i, raw in enumerate(lines[:body_line_count]):
        m_tbl_hdr = re.match(r'^\s*\[TABLE\s+(\d+)\]\s*$', raw)
        if m_tbl_hdr:
            cur_tbl_body = f"T{m_tbl_hdr.group(1)}"
            continue
        if i in legend_line_indices:
            continue
        ptag, clean = strip_ptag(raw)
        if ptag.startswith('[Row') and cur_tbl_body:
            ptag = f"[{cur_tbl_body}, {ptag[1:-1]}]"
        loc = ptag or f"L{i+1}"
        if re.match(r'^\s*(?:references|bibliography|literature\s+cited)\s*$', clean, re.I):
            break

        line_fig_matches = []
        for m in fig_pat.finditer(clean):
            expr = m.group(2)
            mod = (m.group(1) or '').lower()
            s_pref = 'EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')
            expanded = expand_callout_expr("Figure", expr, is_supp=bool(m.group(1)), supp_prefix=s_pref)
            for fid, pan in expanded:
                fig_callouts.append((loc, fid, pan, m.group(0)))
            line_fig_matches.append((m.group(0), set(expanded)))

        # Check adjacent overlapping panel callouts in the same paragraph (e.g. "(Figure 3C-D) ... (Figure 3D)")
        for idx_m in range(len(line_fig_matches) - 1):
            raw1, set1 = line_fig_matches[idx_m]
            raw2, set2 = line_fig_matches[idx_m + 1]
            overlap = {x for x in (set1 & set2) if x[1] is not None}
            if overlap and set1 != set2:
                overlapping_callouts.append(
                    f"  - {loc}: adjacent callouts '{raw1}' and '{raw2}' overlap on {[f'{f}{p}' for f, p in sorted(overlap)]}"
                )

        for m in tbl_pat.finditer(clean):
            expr = m.group(2)
            mod = (m.group(1) or '').lower()
            s_pref = 'EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')
            expanded_tbls = expand_callout_expr("Table", expr, is_supp=bool(m.group(1)), supp_prefix=s_pref)
            for tid, _ in expanded_tbls:
                table_callouts.append((loc, tid, m.group(0)))

        for m in vid_pat.finditer(clean):
            expr = m.group(2)
            mod = (m.group(1) or '').lower()
            s_pref = 'EV' if 'expanded' in mod else 'S'
            expanded_vids = expand_callout_expr("Video", expr, is_supp=bool(m.group(1)), supp_prefix=s_pref)
            for vid_id, _ in expanded_vids:
                video_callouts.append((loc, vid_id, m.group(0)))

    # 3. Check Uncited & Phantom items
    print("\n== 2. UNCITED FIGURES, PANELS, TABLES & VIDEOS ==")
    cited_fig_ids = {f for _, f, _, _ in fig_callouts}
    cited_fig_panels = {(f, p) for _, f, p, _ in fig_callouts if p}
    cited_tbl_ids = {t for _, t, _ in table_callouts}
    cited_vid_ids = {v for _, v, _ in video_callouts}

    any_uncited = False
    for fid, info in defined_figs.items():
        if fid not in cited_fig_ids:
            print(f"  !! UNCITED FIGURE: {fid} ({info['ptag']}) is never cited in the main text!")
            any_uncited = True
        else:
            for p in info['panels']:
                if (fid, p) not in cited_fig_panels:
                    print(f"  !! UNCITED PANEL: {fid}{p} ({info['ptag']}) is never specifically cited in the main text!")
                    any_uncited = True
    for tid, info in defined_tables.items():
        if tid not in cited_tbl_ids:
            print(f"  !! UNCITED TABLE: {tid} ({info['ptag']}) is never cited in the main text!")
            any_uncited = True
    for vid, info in defined_videos.items():
        if vid not in cited_vid_ids:
            print(f"  !! UNCITED VIDEO: {vid} ({info['ptag']}) is never cited in the main text!")
            any_uncited = True
    if not any_uncited:
        print("  All defined figures, panels, tables, and videos are cited at least once.")

    print("\n== 3. PHANTOM CALLOUTS (CITED IN TEXT BUT MISSING FROM LEGENDS) ==")
    phantoms = []
    for loc, fid, pan, raw_m in fig_callouts:
        if fid not in defined_figs:
            phantoms.append(f"  !! {loc}: cites '{fid}' (in '{raw_m}'), which has no figure legend!")
        elif pan and defined_figs[fid]['panels'] and pan not in defined_figs[fid]['panels']:
            phantoms.append(f"  !! {loc}: cites panel '{fid}{pan}' (in '{raw_m}'), but {fid} legend only defines panels {defined_figs[fid]['panels']}!")
    for loc, tid, raw_m in table_callouts:
        if tid not in defined_tables:
            phantoms.append(f"  !! {loc}: cites '{tid}' (in '{raw_m}'), which is not defined!")
    for loc, vid, raw_m in video_callouts:
        if vid not in defined_videos:
            phantoms.append(f"  !! {loc}: cites '{vid}' (in '{raw_m}'), which has no video legend!")
    if phantoms:
        dedup_ph = OrderedDict()
        for ph in phantoms:
            dedup_ph[ph] = dedup_ph.get(ph, 0) + 1
        for ph, cnt in dedup_ph.items():
            print(f"{ph} (x{cnt})" if cnt > 1 else ph)
    else:
        print("  None detected.")

    print("\n== 4. CALLOUT ORDERING & OVERLAP AUDIT ==")
    seen_main_figs = []
    seen_supp_figs = []
    seen_panels_per_fig = {fid: [] for fid in defined_figs}

    for loc, fid, pan, raw_m in fig_callouts:
        num_str = fid.split()[1]
        if num_str.startswith(('S', 'EV', 'ED')):
            if fid not in [f for f, _ in seen_supp_figs]:
                seen_supp_figs.append((fid, loc))
        else:
            if fid not in [f for f, _ in seen_main_figs]:
                seen_main_figs.append((fid, loc))
        if fid in seen_panels_per_fig and pan:
            if pan not in [p for p, _ in seen_panels_per_fig[fid]]:
                seen_panels_per_fig[fid].append((pan, loc))

    order_issues = False
    main_nums = [int(f.split()[1]) for f, _ in seen_main_figs if f.split()[1].isdigit()]
    if main_nums != sorted(main_nums):
        print(f"  !! Main figures cited out of numerical order: {[f'{f} ({l})' for f, l in seen_main_figs]}")
        order_issues = True
    supp_nums = [int(re.sub(r'^(?:EV|ED|S)', '', f.split()[1])) for f, _ in seen_supp_figs if re.sub(r'^(?:EV|ED|S)', '', f.split()[1]).isdigit()]
    if supp_nums != sorted(supp_nums):
        print(f"  !! Supplementary/Expanded View figures cited out of numerical order: {[f'{f} ({l})' for f, l in seen_supp_figs]}")
        order_issues = True
    for fid, p_list in seen_panels_per_fig.items():
        letters = [p for p, _ in p_list]
        if letters != sorted(letters):
            formatted = ", ".join(f"{fid}{p} (at {l})" for p, l in p_list)
            print(f"  !! Out-of-order panel citations for {fid}: {formatted}")
            order_issues = True
    if overlapping_callouts:
        for oc in overlapping_callouts:
            print(oc)
        order_issues = True
    if not order_issues:
        print("  All figures and panels are first cited in sequential order with no redundant overlaps.")

    print("\n== 5. WHOLE-FIGURE CALLOUTS TO MULTI-PANEL FIGURES ==")
    whole_calls = OrderedDict()
    for loc, fid, pan, raw_m in fig_callouts:
        if pan is None and fid in defined_figs and len(defined_figs[fid]['panels']) > 1:
            msg = f"  - {loc}: cites '{fid}' without panel letter (in '{raw_m}'); {fid} has panels {defined_figs[fid]['panels']}"
            whole_calls[msg] = whole_calls.get(msg, 0) + 1
    if whole_calls:
        for wc, cnt in whole_calls.items():
            print(f"{wc} (x{cnt})" if cnt > 1 else wc)
    else:
        print("  None.")

    print("\n== 6. BIDIRECTIONAL MATERIALS & METHODS COVERAGE CHECK ==")
    mm_start, mm_end = None, None
    for i, raw in enumerate(lines[:body_line_count]):
        _, clean = strip_ptag(raw)
        if re.match(r'^\s*(?:\d+\.?\s+)?(?:materials?\s+and\s+methods?|methods?|experimental\s+procedures?|experimental\s+section)\s*$', clean, re.I):
            mm_start = i + 1
            continue
        if mm_start is not None and re.match(
            r'^\s*(?:\d+\.?\s+)?(?:references|bibliography|acknowledg(?:e)?ments?|author\s+contributions?|'
            r'funding|competing\s+(?:financial\s+)?interests?|conflicts?\s+of\s+interest|declarations?|'
            r'data\s+(?:and\s+code\s+)?availability|code\s+availability|results|discussion|'
            r'supplementary\s+(?:figures?|tables?|materials?|information)|expanded\s+view|extended\s+data|figure\s+legends?)\s*:?\s*$',
            clean,
            re.I,
        ):
            mm_end = i
            break
    if mm_start is not None:
        mm_end = mm_end or body_line_count
        mm_text = '\n'.join(lines[mm_start:mm_end]).lower()
        non_mm_text = '\n'.join(lines[:mm_start - 1] + lines[mm_end:]).lower()

        cur_head = None
        cur_head_loc = None
        cur_body = []
        mm_sections = []
        for i in range(mm_start, mm_end):
            ptag, clean = strip_ptag(lines[i])
            if not clean.strip() or ptag.startswith(('[T', '[TABLE', '[Row')):
                continue
            if len(clean) < 85 and not clean.endswith('.'):
                if cur_head:
                    mm_sections.append((cur_head_loc, cur_head, ' '.join(cur_body)))
                cur_head = clean
                cur_head_loc = ptag or f"L{i+1}"
                cur_body = []
            else:
                cur_body.append(clean)
        if cur_head:
            mm_sections.append((cur_head_loc, cur_head, ' '.join(cur_body)))

        orphan_found = False
        admin_head_pat = re.compile(
            r'\b(?:statistic|reproducib|data\s+availability|code\s+availability|ethics|ethical|'
            r'strains?\s+and\s+(?:plasmids?|growth)|reagents?\s+and\s+antibodies|general\s+methods|'
            r'media\s+and\s+buffers|competing\s+interests?|author\s+contributions?|funding|'
            r'acknowledg(?:e)?ments?|conflicts?\s+of\s+interest|declarations?|cell\s+lines?)\b',
            re.I,
        )
        generic_mm_words = {
            'analysis', 'analyses', 'assay', 'assays', 'cells', 'culture', 'cultures', 'method',
            'methods', 'staining', 'quantification', 'microscopy', 'bacterial', 'strains', 'strain',
            'infection', 'construction', 'mutant', 'mutants', 'growth', 'conditions', 'treatment',
            'statistical', 'statistics', 'mammalian', 'lines', 'line', 'cell', 'infected', 'preparation', 'sample',
            'samples', 'reproducibility', 'availability', 'ethics', 'ethical', 'reagents', 'antibodies',
            'plasmids', 'primers', 'software', 'image', 'images', 'processing', 'general', 'materials',
            'media', 'buffers', 'buffer', 'experiments', 'experiment', 'experimental', 'procedures',
            'isolation', 'extraction', 'purification', 'measurement', 'measurements', 'recording',
            'author', 'authors', 'contributions', 'contribution', 'funding', 'competing', 'interests',
            'conflict', 'conflicts', 'acknowledgments', 'acknowledgements', 'declarations', 'declaration',
        } | ignore_set
        for loc, head, body in mm_sections:
            if admin_head_pat.search(head) or head.lower() in ignore_set:
                continue
            head_words = [w.lower() for w in re.findall(r'[A-Za-z]{5,}', head) if w.lower() not in generic_mm_words]
            all_head_missing = bool(head_words) and all(w not in non_mm_text for w in head_words)
            special_reagents = [
                w.lower() for w in re.findall(r'\b(?:phalloidin|immunofluorescence|immunoblot|western\s+blot|elisa|tunel|annexin|propidium)\b', head + ' ' + body, re.I)
                if w.lower() not in ignore_set
            ]
            missing_reagents = sorted(set(w for w in special_reagents if w not in non_mm_text))
            if all_head_missing or missing_reagents:
                orphan_found = True
                print(f"  !! POTENTIAL ORPHAN METHODS SUBSECTION: '{head}' ({loc})")
                if all_head_missing:
                    print(f"     Heading term(s) never mentioned outside Methods: {head_words}")
                if missing_reagents:
                    print(f"     Assay/reagent(s) never mentioned in Results or Legends: {missing_reagents}")

        # Reverse check: Reagents, stains, enzymes, or tools mentioned in Figure Legends (e.g. "stained with X",
        # "labeled with X", "treated with X", "digested with X", "segmented with/using X") but missing from Methods
        fig_reagent_pat = re.compile(
            r'\b(?:stained|labeled|labelled|treated|incubated|inhibited|digested|fixed|permeabilized|segmented|analyzed|quantified)\s+'
            r'(?:with|using|by)\s+(?:the\s+)?([A-Z0-9][A-Za-z0-9_-]{2,}|\b[a-z]{4,}ase\b(?:\s+[A-Z0-9])?)',
        )
        stop_reagents = {
            'the', 'various', 'different', 'either', 'both', 'each', 'all', 'two', 'three', 'four',
            'unpaired', 'paired', 'one-way', 'two-way', 'student', 'anova', 'tukey', 'welch',
        }
        missing_in_mm = defaultdict(list)
        for fid, info in defined_figs.items():
            for m_at in fig_reagent_pat.finditer(info['text']):
                term = m_at.group(1).strip('.,;:()')
                if term.lower() in stop_reagents or len(term) < 3:
                    continue
                if term.lower() not in mm_text:
                    missing_in_mm[term].append(f"{fid} ({info['ptag']})")
        if missing_in_mm:
            orphan_found = True
            for term, locs in sorted(missing_in_mm.items()):
                print(f"  !! FIGURE REAGENT/TOOL MISSING FROM METHODS: '{term}' appears in {sorted(set(locs))} but is never mentioned in Materials & Methods!")

        if not orphan_found:
            print("  All Methods subsections and Figure reagents/tools have bidirectional coverage.")
    else:
        print("  (Materials & Methods heading not detected)")

    print("\n== 7. SYSTEMATIC FIGURE LEGEND COMPLETENESS MATRIX ==")
    if not defined_figs:
        print("  (No figure legends detected)")
    else:
        print(f"  {'Figure':<12} | {'Loc':<8} | {'ScaleBar':<10} | {'SampleSize(n)':<14} | {'Center/ErrBar':<14} | {'Stats/P-val':<12}")
        print("  " + "-" * 82)
        missing_scale_figs = []
        missing_n_figs = []
        missing_err_figs = []
        for fid, info in defined_figs.items():
            txt = info['text']
            has_scale = bool(re.search(r'\b(?:scale\s+bars?|\d+(?:\.\d+)?\s*(?:\u00b5m|\u03bcm|um|nm|mm))\b', txt, re.I))
            has_img_cue = bool(re.search(r'\b(?:micrograph|microscopy|image|images|confocal|fluorescence|fluorescent|stain|stained|channel|merge|merged|inset|magnif|bar|field|dapi|hoechst|phalloidin|gfp|mcherry|brightfield)\b', txt, re.I))
            has_n = bool(re.search(r'\bn\s*=\s*\d+|\b\d+\s+(?:independent|biological|technical)\s+(?:experiments?|replicates?|cultures?|samples?)\b|\brepresentative\s+of\s+\d+', txt, re.I))
            has_err = bool(re.search(r'\b(?:mean|median|s\.?d\.?|s\.?e\.?m\.?|iqr|error\s+bars?|standard\s+deviation|standard\s+error|confidence\s+interval|box\s*plot)\b', txt, re.I))
            has_stat = bool(re.search(r'(?:\bp\s*[<=>]\s*0?\.\d+|\b(?:anova|t-test|student|mann-whitney|wilcoxon|kruskal|tukey|bonferroni|sidak|\u0161id\u00e1k|dunn|fdr|benjamini|two-tailed|one-tailed|not\s+significant)\b|\*\s*=)', txt, re.I))

            sc_str = "YES" if has_scale else ("MISSING?" if has_img_cue else "n/a")
            n_str = "YES" if has_n else "MISSING"
            err_str = "YES" if has_err else "MISSING"
            st_str = "YES" if has_stat else "-"
            print(f"  {fid:<12} | {info['ptag']:<8} | {sc_str:<10} | {n_str:<14} | {err_str:<14} | {st_str:<12}")
            if sc_str == "MISSING?":
                missing_scale_figs.append(fid)
            if not has_n:
                missing_n_figs.append(fid)
            if not has_err:
                missing_err_figs.append(fid)
        if missing_scale_figs:
            print(f"  !! Legends with microscopy/image cues but no scale-bar dimension stated: {missing_scale_figs}")
        if len(missing_n_figs) == len(defined_figs) and len(defined_figs) >= 3:
            print(f"  !! SYSTEMATIC OMISSION: 0 of {len(defined_figs)} figure legends state sample size / replicate count (n)!")
        if len(missing_err_figs) == len(defined_figs) and len(defined_figs) >= 3:
            print(f"  !! SYSTEMATIC OMISSION: 0 of {len(defined_figs)} figure legends define central tendency or error bars (mean/median ± SD/SEM)!")


if __name__ == '__main__':
    main()
