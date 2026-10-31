#!/usr/bin/env python3
"""Audit in-text callouts to Figures, Panels, Supplementary Figures, Tables, Videos, and Methods subsections.

Usage:
  callout_audit.py TEXTFILE [--supp-files FILE1,FILE2 ...]

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
  7. Systematic Figure Legend Completeness Matrix (scale bars, sample size n, central tendency/error bars, stats)
"""
import sys
import os
import re
from collections import OrderedDict, defaultdict


def strip_ptag(line):
    """Strip one or more leading bracketed paragraph/line tags (e.g. '[supp.txt:L1] [P1] text')."""
    ptags = []
    cur = line
    pat = re.compile(r'^\s*(\[(?:P\d+(?:\.\d+)?|T\d+|TABLE\s+\d+|Row\s+\d+|[^\]:]+:(?:P|L)\d+(?:\.\d+)?)\])\s*(.*)')
    while True:
        m = pat.match(cur)
        if not m:
            break
        ptags.append(m.group(1))
        cur = m.group(2)
    return (ptags[0] if ptags else ''), cur


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
        if ord(ch) - ord(kept[-1]) <= 2:
            kept.append(ch)
        else:
            break
    return [c for c in uniq if c in kept]


def parse_panel_letters(legend_text, return_anomalies=False):
    """Extract panel letters defined via (A), (B,C), (A-D), A), B), bare sentence/line-initial A ..., or lowercase (a), (b,c).
    Excludes taxonomic genus initials like 'C. trachomatis' or 'E. coli'.
    """
    anomalies = []
    # Check for missing sentence-ending period before parenthesized or bare panel labels mid-line (e.g. "word (B) Graph" or "(B) G")
    for m_miss in re.finditer(r'([a-z0-9\])])\s+(\([A-H]\)\s+[A-Z])', legend_text):
        anomalies.append(f"Possible missing period before panel label '{m_miss.group(2)}' (after '...{m_miss.group(1)}')")
    for m_trunc_p in re.finditer(r'(\([A-H]\)\s+[A-Z])\s*(?=\([A-H]\)|$)', legend_text):
        anomalies.append(f"Truncated 1-letter panel description '{m_trunc_p.group(1).strip()}'")

    upper_raw = []
    for m in re.finditer(
        r'(?:\(([A-Z](?:\s*[,;\u2013\u2014-]\s*[A-Z])*)\)|(?:^|(?<=[.?!:;)\n]\s)|(?<=\n))([A-N])[.):]?(?!\.\s*[a-z]{3,})(?=\s+[A-Z0-9]))',
        legend_text,
        re.M,
    ):
        # Exclude abbreviated Latin binomials like "C. trachomatis", "E. coli", "S. aureus"
        if m.group(2):
            after_slice = legend_text[m.end(2):m.end(2) + 20]
            if re.match(r'^\.\s+[a-z]{3,}\b', after_slice):
                continue
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
    for m in re.finditer(
        r'(?:\(([a-n](?:\s*[,;\u2013\u2014-]\s*[a-n])*)\)|(?:^|(?<=[.?!:;)\n]\s)|(?<=\n))([a-h])\)(?=\s+[A-Za-z0-9]))',
        legend_text,
        re.M,
    ):
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
    res = lower_filtered if len(lower_filtered) > len(upper_filtered) else upper_filtered
    if return_anomalies:
        return res, anomalies
    return res


def expand_callout_expr(prefix, expr, is_supp=False, supp_prefix='S'):
    """Parse callout expressions like '4C, S4A', '16ABC', 'EV1A-C', '6-9', '1 D-E', '5A,B and 6B,C' into [(fig_id, panel_or_None)]."""
    results = []
    expr = re.sub(r'\b(?:and|or)\s+a\s+[a-z]{2,}\b', '', expr)
    expr = re.sub(r'\b(?:and|or)\b', ',', expr, flags=re.I)
    expr = re.sub(r'\b((?:EV|ED|S)?\d+)\s+([A-Ja-j](?:\s*[\u2013\u2014-]\s*[A-Ja-j])?|[A-J]{2,5})\b(?!\s+[a-z]{2,})', r'\1\2', expr)
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
        # Concatenated ascending uppercase panel letters on a figure: e.g. "16ABC", "4CDE", "5BC"
        m_concat = re.match(r'^((?:EV|ED|S)?\d+)([A-J]{2,6})$', tok_clean, re.I)
        if m_concat:
            p_chars = m_concat.group(2).upper()
            if list(p_chars) == sorted(set(p_chars)):
                fig_num = m_concat.group(1).upper()
                if is_supp and not fig_num.startswith(('S', 'EV', 'ED')):
                    fig_num = supp_prefix + fig_num
                cur_fig = f"{prefix} {fig_num}"
                for ch in p_chars:
                    results.append((cur_fig, ch))
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
        # Bare panel letter(s) continuing previous figure: e.g. "B" in "Figure 7B,C" or "BC" in "Figure 5A,BC"
        m_panel = re.match(r'^([A-Ja-j]{1,4})$', tok_clean)
        if m_panel and cur_fig:
            p_chars = m_panel.group(1).upper()
            if len(p_chars) == 1 or list(p_chars) == sorted(set(p_chars)):
                for ch in p_chars:
                    results.append((cur_fig, ch))
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


def _load_external_legend_lines(main_txt_path, first_header_line=""):
    """Discover and load lines from external movie/figure/table legend files and .zip movie archives."""
    cand_dirs = []
    if "--legend-dir" in sys.argv:
        idx = sys.argv.index("--legend-dir")
        if idx + 1 < len(sys.argv):
            cand_dirs.append(sys.argv[idx + 1])
    if first_header_line.startswith("# MANUSCRIPT-AUDIT EXTRACT"):
        m_p = re.search(r'\bpath=([^|]+)', first_header_line)
        if m_p and os.path.isfile(m_p.group(1).strip()):
            cand_dirs.append(os.path.dirname(os.path.abspath(m_p.group(1).strip())))
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
            if low.endswith(".zip") and ("movie" in low or "video" in low):
                import zipfile
                try:
                    with zipfile.ZipFile(full, "r") as zf:
                        for zname in sorted(zf.namelist()):
                            if zname.lower().endswith(".docx") and not os.path.basename(zname).startswith("~$"):
                                import xml.etree.ElementTree as ET
                                root = ET.fromstring(zf.read(zname))
                                w_p = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
                                w_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
                                for idx, p in enumerate(root.iter(w_p), 1):
                                    txt = "".join(t.text or "" for t in p.iter(w_t)).strip()
                                    if txt:
                                        extra.append(f"[{os.path.basename(zname)}:P{idx}] {txt}")
                except Exception:
                    pass
                seen_files.add(os.path.abspath(full))
                continue
            if not is_dedicated_legend_dir and not (
                ("legend" in low or "movie" in low or "video" in low or "supplement" in low) and low.endswith((".docx", ".txt"))
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
    all_raw = open(sys.argv[1], encoding='utf-8').read().splitlines()
    first_hdr = all_raw[0] if all_raw and all_raw[0].startswith('# MANUSCRIPT-AUDIT EXTRACT') else ''
    lines = [l for l in all_raw if not l.startswith('# MANUSCRIPT-AUDIT EXTRACT')]

    # Collect companion / supplementary files passed via positional args (sys.argv[2:]) or --supp-files
    supp_paths = []
    skip_next = False
    for idx_a in range(2, len(sys.argv)):
        if skip_next:
            skip_next = False
            continue
        arg = sys.argv[idx_a]
        if arg in ('--supp-files', '--ignore', '--legend-dir'):
            skip_next = True
            if arg == '--supp-files' and idx_a + 1 < len(sys.argv):
                for sp in re.split(r'[,;]', sys.argv[idx_a + 1]):
                    if sp.strip() and os.path.isfile(sp.strip()):
                        supp_paths.append(sp.strip())
            continue
        if not arg.startswith('-') and os.path.isfile(arg):
            supp_paths.append(arg)

    defined_figs = OrderedDict()
    defined_tables = OrderedDict()
    defined_videos = OrderedDict()

    ext_legend_lines = _load_external_legend_lines(sys.argv[1], first_header_line=first_hdr)
    for sp in supp_paths:
        sp_base = os.path.basename(sp)
        # Check if the companion filename or its extract header defines a standalone Table / Figure / Video (e.g. Table S1_ver2.docx)
        if sp.lower().endswith('.docx'):
            sp_lines = _extract_docx_lines_simple(sp)
            hdr_name = sp_base
        else:
            raw_sp = open(sp, encoding='utf-8', errors='replace').read().splitlines()
            hdr_name = sp_base
            if raw_sp and raw_sp[0].startswith('# MANUSCRIPT-AUDIT EXTRACT'):
                m_f = re.search(r'\bfile=([^|\n]+)', raw_sp[0])
                if m_f:
                    hdr_name = m_f.group(1).strip()
            sp_lines = [f"[{sp_base}:L{idx_l}] {ln.strip()}" for idx_l, ln in enumerate(raw_sp, 1) if ln.strip() and not ln.startswith('#')]
        ext_legend_lines.extend(sp_lines)
        m_fn_tbl = re.search(r'(?:^|[_\s])(?:(Supplementary|Supplemental|Expanded[_\s]+View|Extended[_\s]+Data)[_\s]+)?Table[_\s]+((?:EV|ED|S)?\d+)\b', hdr_name, re.I)
        if m_fn_tbl:
            tnum = m_fn_tbl.group(2).upper()
            mod = (m_fn_tbl.group(1) or '').lower()
            if mod and not tnum.startswith(('S', 'EV', 'ED')):
                tnum = ('EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')) + tnum
            tid = f"Table {tnum}"
            if tid not in defined_tables:
                first_txt = strip_ptag(sp_lines[0])[1][:120] if sp_lines else hdr_name
                defined_tables[tid] = {'ptag': f"[{hdr_name}]", 'title': f"{tid} ({hdr_name}): {first_txt}"}

    body_line_count = len(lines)
    if ext_legend_lines:
        lines.extend(ext_legend_lines)

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
                full_vleg = seg
                k_next = i + 1
                while k_next < len(lines) and not ptag.startswith('[T'):
                    _, next_clean = strip_ptag(lines[k_next])
                    next_no_img = strip_image_tags(next_clean)
                    if not next_no_img:
                        k_next += 1
                        continue
                    if re.match(r'^(?:Supplementary\s+|Supplemental\s+|Supp\.?\s+|Expanded\s+View\s+|Extended\s+Data\s+)?(?:Figure|Fig\.?|Video|Movie|Table)\s+(?:EV|ED|S)?\d+\s*[.:–—-]?', next_no_img, re.I):
                        break
                    if re.match(r'^\s*(?:references|bibliography|acknowledg(?:e)?ments?|materials?\s+and\s+methods)\s*:?\s*$', next_no_img, re.I):
                        break
                    full_vleg += " " + next_no_img
                    legend_line_indices.add(k_next)
                    k_next += 1
                defined_videos[vid] = {'ptag': ptag or f"L{i+1}", 'title': full_vleg}
                legend_line_indices.add(i)
                continue

            # Check Figure legend definition
            m_fig = re.match(r'^(?:(Supplementary|Supplemental|Supp\.?|Expanded\s+View|Extended\s+Data)\s+)?(?:Figure|Fig\.?)\s+((?:EV|ED|S)?\d+)\s*[.:–—-]\s*(.+)', seg, re.I)
            if m_fig:
                fnum = m_fig.group(2).upper()
                mod = (m_fig.group(1) or '').lower()
                if mod and not fnum.startswith(('S', 'EV', 'ED')):
                    fnum = ('EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')) + fnum
                fid = f"Figure {fnum}"
                legend_line_indices.add(i)
                full_leg = seg
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
                    if re.match(r'^(?:\([A-Za-z]|[A-Ha-h][.):]?\s+[A-Z])', next_no_img) or in_legends_section:
                        # Join multi-paragraph figure legends with '\n' so line-initial panel labels ('A Schematic...') are preserved even if the title lacks a period
                        full_leg += "\n" + next_no_img
                        legend_line_indices.add(k_next)
                        k_next += 1
                    else:
                        break
                panels, leg_anoms = parse_panel_letters(full_leg, return_anomalies=True)
                defined_figs[fid] = {
                    'ptag': ptag or f"L{i+1}",
                    'title': seg,
                    'panels': panels,
                    'text': full_leg,
                    'anomalies': leg_anoms,
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
    fig_callouts = []
    table_callouts = []
    video_callouts = []
    overlapping_callouts = []

    fig_pat = re.compile(
        r'\b(?:(Supplementary|Supplemental|Supp\.?|Expanded\s+View|Extended\s+Data)\s+)?(?:Figures?|Figs?\.?)\s+'
        r'((?:EV|ED|S)?\d+(?:\s*(?:[A-J]{2,6}|[A-Ja-j])(?![a-z]))?(?:\s*(?:[,;\u2013\u2014-]|\band\b|\bor\b)\s*(?!\d+\s*-\s*way\b)(?:(?:EV|ED|S)?\d+(?:\s*(?:[A-J]{2,6}|[A-Ja-j])(?![a-z]))?|(?:[A-J]{2,6}|[A-Ja-j])(?![a-z])|\b(?:left|right|top|bottom|inset)\b))*)',
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
        elif pan and not defined_figs[fid]['panels']:
            phantoms.append(f"  !! {loc}: cites panel '{fid}{pan}' (in '{raw_m}'), but {fid} legend defines NO panel letters at all (single-panel legend or missing panel labels in legend)!")
        elif pan and pan not in defined_figs[fid]['panels']:
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
        # Include companion supplementary text/tables in mm_plus_supp_text when checking whether strains/reagents are defined
        supp_extra_text = '\n'.join(lines[body_line_count:]).lower()
        mm_text = ('\n'.join(lines[mm_start:mm_end]) + '\n' + supp_extra_text).lower()
        non_mm_lines = lines[:mm_start - 1] + lines[mm_end:body_line_count]
        non_mm_text = '\n'.join(non_mm_lines).lower()

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

        fig_reagent_pat = re.compile(
            r'\b(?:stained|labeled|labelled|treated|incubated|inhibited|digested|fixed|permeabilized|segmented|analyzed|quantified)\s+'
            r'(?:with|using|by)\s+(?:the\s+)?([A-Z0-9][A-Za-z0-9_-]{2,}|\b[a-z]{4,}ase\b(?:\s+[A-Z0-9])?)',
        )
        # Section 6B: Reverse Methods Coverage — scan Results & Figure Legends for fluorescent proteins, tags, stains, and named techniques
        reverse_assay_pat = re.compile(
            r'\b(mNeonGreen|mGreenLantern|mCherry|tdTomato|mScarlet|mRFP|mTurquoise|mVenus|mCerulean|sfGFP|eGFP|EGFP|EYFP|ECFP|'
            r'HaloTag|SNAP-tag|CLIP-tag|NanoLuc|Luciferase|VSV-G|FLAG-tag|His6|Strep-tag|Hoechst(?:\s*\d+)?|DAPI|Sytox|'
            r'propidium\s+iodide|FM\s*4-64|phalloidin|MitoTracker|LysoTracker|Calcofluor| Nile\s+Red|annexin\s+V|TUNEL|'
            r'RT-qPCR|qRT-PCR|qPCR|RNA-seq|ChIP-seq|ATAC-seq|LC-MS/MS|mass\s+spectrometry|co-immunoprecipitation|'
            r'surface\s+plasmon\s+resonance|isothermal\s+titration\s+calorimetry|analytical\s+ultracentrifugation|'
            r'size-exclusion\s+chromatography|dynamic\s+light\s+scattering|atomic\s+force\s+microscopy|cryo-EM|FRAP|FRET|TIRF)\b',
            re.I,
        )
        stop_reagents = {
            'the', 'various', 'different', 'either', 'both', 'each', 'all', 'two', 'three', 'four',
            'unpaired', 'paired', 'one-way', 'two-way', 'student', 'anova', 'tukey', 'welch',
        }
        missing_in_mm = defaultdict(list)
        for fid, info in defined_figs.items():
            for m_at in fig_reagent_pat.finditer(info['text']):
                term = m_at.group(1).strip('.,;:()')
                if term.lower() in stop_reagents or len(term) < 3 or term.lower() in ignore_set:
                    continue
                if term.lower() not in mm_text:
                    missing_in_mm[term].append(f"{fid} ({info['ptag']})")
            for m_ra in reverse_assay_pat.finditer(info['text']):
                term = m_ra.group(1).strip()
                if term.lower() not in mm_text and term.lower() not in ignore_set:
                    missing_in_mm[term].append(f"{fid} ({info['ptag']})")

        # Also scan Results paragraphs for reverse_assay_pat terms missing from Materials & Methods
        for i_ln in range(0, mm_start - 1):
            ptag, clean = strip_ptag(lines[i_ln])
            if not clean.strip() or i_ln in legend_line_indices:
                continue
            for m_ra in reverse_assay_pat.finditer(clean):
                term = m_ra.group(1).strip()
                if term.lower() not in mm_text and term.lower() not in ignore_set:
                    missing_in_mm[term].append(ptag or f"L{i_ln+1}")

        if missing_in_mm:
            orphan_found = True
            for term, locs in sorted(missing_in_mm.items()):
                print(f"  !! RESULTS/FIGURE ASSAY, REPORTER, OR REAGENT MISSING FROM METHODS: '{term}' appears in {sorted(set(locs))[:6]} but is never mentioned in Materials & Methods!")

        if not orphan_found:
            print("  All Methods subsections and Results/Figure reagents/tools have bidirectional coverage.")
    else:
        print("  (Materials & Methods heading not detected)")

    print("\n== 7. SYSTEMATIC FIGURE LEGEND COMPLETENESS MATRIX ==")
    if not defined_figs:
        print("  (No figure legends detected)")
    else:
        print(f"  {'Figure':<12} | {'Loc':<8} | {'ScaleBar':<18} | {'SampleSize(n)':<14} | {'Center/ErrBar':<14} | {'Stats/P-val':<12}")
        print("  " + "-" * 90)
        missing_scale_figs = []
        partial_scale_figs = []
        missing_n_figs = []
        missing_err_figs = []
        for fid, info in defined_figs.items():
            txt = info['text']
            # Exclude non-scale-bar spatial measurements like "10 µm radius", "0.22 µm filter", "100 µm diameter", "µm^2", "µm/s"
            txt_for_scale = re.sub(
                r'\b\d+(?:\.\d+)?\s*(?:\u00b5m|\u03bcm|um|nm|mm)\s*(?:radius|diameter|filter|pore|sieve|bead|thick|section|cuvette|capillary|channel\s+width|channel\s+height|2|3|\u00b2|\u00b3|/s|/min|/h)\b',
                ' ',
                txt,
                flags=re.I,
            )
            sb_mentions = re.findall(r'\bscale\s+bars?\b|\bbar\s*[=:]\s*\d+(?:\.\d+)?\s*(?:\u00b5m|\u03bcm|um|nm|mm)\b', txt_for_scale, re.I)
            dim_mentions = re.findall(r'\b\d+(?:\.\d+)?\s*(?:\u00b5m|\u03bcm|um|nm|mm)\b', txt_for_scale, re.I)
            has_scale = bool(sb_mentions or dim_mentions)
            has_all_panels_phrase = bool(re.search(r'\b(?:scale\s+bars?\s+(?:in\s+all|for\s+all|represent)|all\s+scale\s+bars?|each\s+panel|unless\s+otherwise)\b', txt, re.I))

            # Distinguish true micrograph cues from pure flow-cytometry / plate-reader fluorescence
            is_flow_only = bool(re.search(r'\bflow\s+cytometr|facs\b|gating\s+strateg|plate\s+reader|spectrofluor|od\s*600\b', txt, re.I)) and not bool(
                re.search(r'\b(?:micrograph|microscopy|confocal|widefield|epifluorescen|phase\s+contrast|brightfield|dapi|hoechst|phalloidin|merge|inset|overlay|kymograph|time-lapse)\b', txt, re.I)
            )
            has_img_cue = (not is_flow_only) and bool(
                re.search(r'\b(?:micrograph|microscopy|image|images|confocal|widefield|phase\s+contrast|brightfield|stain|stained|channel|merge|merged|inset|magnif|field|dapi|hoechst|phalloidin|gfp|mcherry|mng|mgl|time-lapse|kymograph)\b', txt, re.I)
            )
            has_n = bool(re.search(r'\bn\s*=\s*\d+|\b\d+\s+(?:independent|biological|technical)\s+(?:experiments?|replicates?|cultures?|samples?)\b|\brepresentative\s+of\s+\d+', txt, re.I))
            has_err = bool(re.search(r'\b(?:mean|median|s\.?d\.?|s\.?e\.?m\.?|iqr|error\s+bars?|standard\s+deviation|standard\s+error|confidence\s+interval|box\s*plot)\b', txt, re.I))
            has_stat = bool(re.search(r'(?:\bp\s*[<=>]\s*0?\.\d+|\b(?:anova|t-test|student|mann-whitney|wilcoxon|kruskal|tukey|bonferroni|sidak|\u0161id\u00e1k|dunn|fdr|benjamini|two-tailed|one-tailed|not\s+significant)\b|\*\s*=)', txt, re.I))

            n_panels = len(info['panels'])
            n_scale_vals = max(len(sb_mentions), len(dim_mentions))
            if has_scale:
                if n_panels >= 2 and n_scale_vals == 1 and not has_all_panels_phrase:
                    sc_str = f"1x/{n_panels}p (CHECK!)"
                    partial_scale_figs.append(f"{fid} ({n_panels} panels, 1 scale mention)")
                else:
                    sc_str = f"YES ({n_scale_vals}x)"
            else:
                sc_str = "MISSING?" if has_img_cue else "n/a"

            n_str = "YES" if has_n else "MISSING"
            err_str = "YES" if has_err else "MISSING"
            st_str = "YES" if has_stat else "-"
            print(f"  {fid:<12} | {info['ptag']:<8} | {sc_str:<18} | {n_str:<14} | {err_str:<14} | {st_str:<12}")
            if sc_str == "MISSING?":
                missing_scale_figs.append(fid)
            if not has_n:
                missing_n_figs.append(fid)
            if not has_err:
                missing_err_figs.append(fid)
            for anom in info.get('anomalies', []):
                print(f"    !! {fid} ({info['ptag']}) LEGEND SYNTAX ANOMALY: {anom}")
        if missing_scale_figs:
            print(f"  !! Legends with microscopy/image cues but NO scale-bar dimension stated: {missing_scale_figs}")
        if partial_scale_figs:
            print(f"  !! Multi-panel legends with only 1 scale-bar value stated (verify whether other micrograph panels in the same figure are missing scale-bar definitions!): {partial_scale_figs}")
        if len(missing_n_figs) == len(defined_figs) and len(defined_figs) >= 3:
            print(f"  !! SYSTEMATIC OMISSION: 0 of {len(defined_figs)} figure legends state sample size / replicate count (n)!")
        if len(missing_err_figs) == len(defined_figs) and len(defined_figs) >= 3:
            print(f"  !! SYSTEMATIC OMISSION: 0 of {len(defined_figs)} figure legends define central tendency or error bars (mean/median ± SD/SEM)!")

    print("\n== 8. INTER-LEGEND COPY-PASTE, TABLE-PAIRING & CALLOUT SEMANTIC CHECKS ==")
    sec8_issues = 0
    # 8a. Inter-legend copy-paste shared >=7-word descriptive clause detector
    boiler_pat = re.compile(
        r'\b(?:error\s+bars?\s+represent|data\s+are\s+(?:presented|shown|expressed)|statistical\s+significance|'
        r'independent\s+experiments?|biological\s+replicates?|one-way\s+anova|two-way\s+anova|student\s*[\'\u2019]?s?\s+t-test|'
        r'scale\s+bars?\s*,?\s*\d+|mean\s*\u00b1\s*s\.?(?:d|e\.?m)|p\s*[<=>]\s*0\.\d+|not\s+significant|representative\s+of)\b',
        re.I,
    )
    fig_list = list(defined_figs.items())
    for i_f in range(len(fig_list)):
        fid1, info1 = fig_list[i_f]
        words1 = re.findall(r"[A-Za-z0-9\u0391-\u03c9'\-]+", info1['text'])
        if len(words1) < 8:
            continue
        ngrams1 = {}
        for k in range(len(words1) - 7):
            phrase = " ".join(words1[k:k + 8])
            if not boiler_pat.search(phrase):
                ngrams1.setdefault(phrase.lower(), phrase)
        for j_f in range(i_f + 1, len(fig_list)):
            fid2, info2 = fig_list[j_f]
            txt2_low = " ".join(re.findall(r"[A-Za-z0-9\u0391-\u03c9'\-]+", info2['text'])).lower()
            shared_phrases = [orig for low_p, orig in ngrams1.items() if low_p in txt2_low]
            if shared_phrases:
                # Pick longest or first distinct shared phrase
                sec8_issues += 1
                print(
                    f"  !! SHARED VERBATIM CLAUSE BETWEEN {fid1} ({info1['ptag']}) AND {fid2} ({info2['ptag']}): "
                    f"\"{shared_phrases[0]}...\" — verify whether a copy-pasted legend condition/timepoint/title was left unedited!"
                )

    # 8b. Legend-to-Table vs Body-to-Table pairing check (e.g. Supp Fig 8 legend cites Table 1 while body pairs Supp Fig 8 with Table 2)
    fig_body_tables = defaultdict(set)
    for i_ln, raw in enumerate(lines[:body_line_count]):
        if i_ln in legend_line_indices:
            continue
        _, clean = strip_ptag(raw)
        f_in_line = set()
        for m in fig_pat.finditer(clean):
            mod = (m.group(1) or '').lower()
            s_pref = 'EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')
            for fid, _ in expand_callout_expr("Figure", m.group(2), is_supp=bool(m.group(1)), supp_prefix=s_pref):
                f_in_line.add(fid)
        t_in_line = set()
        for m in tbl_pat.finditer(clean):
            mod = (m.group(1) or '').lower()
            s_pref = 'EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')
            for tid, _ in expand_callout_expr("Table", m.group(2), is_supp=bool(m.group(1)), supp_prefix=s_pref):
                t_in_line.add(tid)
        if f_in_line and t_in_line:
            for fid in f_in_line:
                fig_body_tables[fid].update(t_in_line)

    for fid, info in defined_figs.items():
        leg_tables = set()
        for m in tbl_pat.finditer(info['text']):
            mod = (m.group(1) or '').lower()
            s_pref = 'EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')
            for tid, _ in expand_callout_expr("Table", m.group(2), is_supp=bool(m.group(1)), supp_prefix=s_pref):
                leg_tables.add(tid)
        if leg_tables and fid in fig_body_tables and not (leg_tables & fig_body_tables[fid]):
            sec8_issues += 1
            print(
                f"  !! FIGURE-TO-TABLE PAIRING MISMATCH on {fid} ({info['ptag']}): "
                f"legend references {sorted(leg_tables)}, whereas main text pairs {fid} with {sorted(fig_body_tables[fid])}!"
            )
        # 8c. Check whether a Table cited in a Figure Legend has a caption/title whose condition tokens conflict with the Figure Legend
        for tid in sorted(leg_tables):
            if tid in defined_tables:
                t_title = defined_tables[tid]['title']
                t_toks = set(w.lower() for w in re.findall(r'[A-Za-z0-9][A-Za-z0-9_-]{1,}', t_title) if len(w) >= 2)
                # Extract specific strain/genotype/condition tokens (containing digits, hyphens, or uppercase interior) from the Figure Legend title sentence
                leg_first_sent = info['text'].split('.')[0]
                leg_conds = set(
                    w for w in re.findall(r'\b(?:[A-Za-z0-9]+-[A-Za-z0-9_-]+|[A-Z]{2,}[0-9]*|[A-Za-z]+[0-9]+[A-Za-z0-9]*)\b', leg_first_sent)
                    if w.lower() not in ('fig', 'figure', 'supplementary', 'table', 'tables', 'extended', 'expanded', 'data', 'view')
                )
                missing_conds = [c for c in leg_conds if c.lower() not in t_toks]
                if missing_conds:
                    # Check if another defined table's title DOES contain those condition tokens
                    better_tables = []
                    for other_tid, other_info in defined_tables.items():
                        if other_tid == tid:
                            continue
                        ot_low = other_info['title'].lower()
                        if all(c.lower() in ot_low for c in missing_conds):
                            better_tables.append(f"{other_tid} ({other_info['ptag']}: \"{other_info['title'][:80]}\")")
                    if better_tables:
                        sec8_issues += 1
                        print(
                            f"  !! LEGEND-TO-TABLE CONDITION MISMATCH on {fid} ({info['ptag']}): "
                            f"legend cites {tid} (\"{t_title[:80]}\"), which lacks legend condition(s) {missing_conds} "
                            f"— whereas {', '.join(better_tables[:2])} matches!"
                        )
        # 8d. Intra-legend duplicate panel sequence detector (e.g., A..B..C followed by another A..B..C pasted from another figure)
        if '\n' in info['text']:
            sub_paras = [sp.strip() for sp in info['text'].split('\n') if sp.strip()]
            if len(sub_paras) >= 2:
                para_panel_sets = []
                for sp in sub_paras:
                    p_lets = extract_legend_panels(sp)
                    if len(p_lets) >= 2:
                        para_panel_sets.append((p_lets, sp[:75]))
                if len(para_panel_sets) >= 2:
                    seen_p = set()
                    for p_lets, sp_preview in para_panel_sets:
                        overlap_p = seen_p & p_lets
                        if len(overlap_p) >= 2:
                            sec8_issues += 1
                            print(
                                f"  !! INTRA-LEGEND DUPLICATE PANEL LABELS in {fid} ({info['ptag']}): "
                                f"panel letters {sorted(overlap_p)} are defined twice across consecutive paragraphs "
                                f"(second block starts: \"{sp_preview}...\") — check for an accidentally pasted legend paragraph!"
                            )
                        seen_p.update(p_lets)

    if sec8_issues == 0:
        print("  None detected.")


if __name__ == '__main__':
    main()
