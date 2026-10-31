#!/usr/bin/env python3
"""Extract structured plain text, tables, media, and formatting diagnostics from a .docx file.

Zero external dependencies (uses Python standard library only).

Usage:
  docx2txt.py FILE.docx                           # extract paragraphs and tables in body order
  docx2txt.py FILE.docx --indexed                 # prefix paragraphs with [P1], [P2]... and [TABLE 1]
  docx2txt.py FILE.docx --media [--outdir DIR]    # also extract embedded images (default: ./docx_media/)
  docx2txt.py FILE.docx --audit-format            # report Word line-numbering settings, empty-line offsets,
                                                  # font-size anomalies, and estimated line numbers per paragraph
  docx2txt.py FILE.docx --extract-refs [OUT.txt]  # extract the numbered References section to OUT.txt (or stdout)
  docx2txt.py FILE.docx --extract-csl [OUT.json]  # extract embedded Zotero/Mendeley/EndNote CSL-JSON metadata
"""
import sys
import os
import re
import json
import shutil
import zipfile
import hashlib
from collections import Counter
import xml.etree.ElementTree as ET

NS = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'm': 'http://schemas.openxmlformats.org/officeDocument/2006/math',
}


def omml_to_text(node):
    """Linearize an Office MathML (<m:oMath>) subtree into readable plain text."""
    if node is None:
        return ''
    tag = node.tag
    if tag == f"{{{NS['m']}}}t":
        return node.text or ''
    if tag == f"{{{NS['m']}}}d":
        d_pr = node.find(f"{{{NS['m']}}}dPr")
        beg_chr = '('
        end_chr = ')'
        if d_pr is not None:
            bc = d_pr.find(f"{{{NS['m']}}}begChr")
            ec = d_pr.find(f"{{{NS['m']}}}endChr")
            if bc is not None and bc.get(f"{{{NS['m']}}}val") is not None:
                beg_chr = bc.get(f"{{{NS['m']}}}val")
            if ec is not None and ec.get(f"{{{NS['m']}}}val") is not None:
                end_chr = ec.get(f"{{{NS['m']}}}val")
        e_nodes = node.findall(f"{{{NS['m']}}}e")
        inner = ', '.join(omml_to_text(e) for e in e_nodes)
        return f"{beg_chr}{inner}{end_chr}"
    if tag == f"{{{NS['m']}}}f":
        num = node.find(f"{{{NS['m']}}}num")
        den = node.find(f"{{{NS['m']}}}den")
        return f"({omml_to_text(num)})/({omml_to_text(den)})"
    if tag == f"{{{NS['m']}}}sSup":
        e = node.find(f"{{{NS['m']}}}e")
        sup = node.find(f"{{{NS['m']}}}sup")
        return f"{omml_to_text(e)}^{{{omml_to_text(sup)}}}"
    if tag == f"{{{NS['m']}}}sSub":
        e = node.find(f"{{{NS['m']}}}e")
        sub = node.find(f"{{{NS['m']}}}sub")
        return f"{omml_to_text(e)}_{{{omml_to_text(sub)}}}"
    if tag == f"{{{NS['m']}}}sSubSup":
        e = node.find(f"{{{NS['m']}}}e")
        sub = node.find(f"{{{NS['m']}}}sub")
        sup = node.find(f"{{{NS['m']}}}sup")
        return f"{omml_to_text(e)}_{{{omml_to_text(sub)}}}^{{{omml_to_text(sup)}}}"
    if tag == f"{{{NS['m']}}}rad":
        e = node.find(f"{{{NS['m']}}}e")
        return f"sqrt({omml_to_text(e)})"
    if tag.endswith('Pr'):
        return ''
    return ''.join(omml_to_text(c) for c in node)


def para_text(p, rid2target, split_br=False):
    """Extract text, inline [[IMAGE:...]] markers, and linearized <m:oMath> equations from a <w:p> element."""
    out = []

    def _walk(elem):
        for node in elem:
            tag = node.tag
            if tag == f"{{{NS['w']}}}del":
                continue
            if tag == f"{{{NS['m']}}}oMath":
                eq_txt = omml_to_text(node).strip()
                if eq_txt:
                    out.append(eq_txt)
                continue
            if tag == f"{{{NS['w']}}}t":
                out.append(node.text or '')
            elif tag == f"{{{NS['w']}}}tab":
                out.append('\t')
            elif tag in (f"{{{NS['w']}}}br", f"{{{NS['w']}}}cr"):
                out.append('\n' if split_br else ' ')
            elif tag == f"{{{NS['a']}}}blip":
                rid = node.get(f"{{{NS['r']}}}embed")
                target = os.path.basename(rid2target.get(rid, rid or 'unknown'))
                out.append(f"[[IMAGE:{target}]]")
            _walk(node)

    _walk(p)
    return ''.join(out)


def para_font_sizes(p):
    """Return list of explicit half-point font sizes (<w:sz w:val='...'/>) in a paragraph."""
    sizes = []
    for r in p.findall(f".//{{{NS['w']}}}r"):
        rpr = r.find(f"{{{NS['w']}}}rPr")
        if rpr is not None:
            sz = rpr.find(f"{{{NS['w']}}}sz")
            if sz is not None and sz.get(f"{{{NS['w']}}}val"):
                try:
                    sizes.append(int(sz.get(f"{{{NS['w']}}}val")) / 2.0)
                except ValueError:
                    pass
    return sizes


def para_suppress_line_numbers(p):
    """Check if <w:suppressLineNumbers/> is set on the paragraph."""
    ppr = p.find(f"{{{NS['w']}}}pPr")
    if ppr is None:
        return False
    sup = ppr.find(f"{{{NS['w']}}}suppressLineNumbers")
    if sup is None:
        return False
    val = sup.get(f"{{{NS['w']}}}val", "true").lower()
    return val not in ("0", "false", "off")


def iter_body_blocks(container):
    """Yield <w:p> and <w:tbl> elements in document order, unwrapping <w:sdt>/<w:sdtContent> and <w:customXml>."""
    p_tag = f"{{{NS['w']}}}p"
    tbl_tag = f"{{{NS['w']}}}tbl"
    sdt_tag = f"{{{NS['w']}}}sdt"
    sdt_content_tag = f"{{{NS['w']}}}sdtContent"
    custom_xml_tag = f"{{{NS['w']}}}customXml"
    for child in container:
        if child.tag in (p_tag, tbl_tag):
            yield child
        elif child.tag == sdt_tag:
            sc = child.find(sdt_content_tag)
            if sc is not None:
                yield from iter_body_blocks(sc)
        elif child.tag in (sdt_content_tag, custom_xml_tag):
            yield from iter_body_blocks(child)


def iter_table_rows(container):
    """Yield <w:tr> elements in document order, unwrapping <w:sdt>/<w:sdtContent> and <w:customXml>."""
    tr_tag = f"{{{NS['w']}}}tr"
    sdt_tag = f"{{{NS['w']}}}sdt"
    sdt_content_tag = f"{{{NS['w']}}}sdtContent"
    custom_xml_tag = f"{{{NS['w']}}}customXml"
    for child in container:
        if child.tag == tr_tag:
            yield child
        elif child.tag == sdt_tag:
            sc = child.find(sdt_content_tag)
            if sc is not None:
                yield from iter_table_rows(sc)
        elif child.tag in (sdt_content_tag, custom_xml_tag):
            yield from iter_table_rows(child)


def iter_row_cells(container):
    """Yield <w:tc> elements in document order, unwrapping <w:sdt>/<w:sdtContent> and <w:customXml>."""
    tc_tag = f"{{{NS['w']}}}tc"
    sdt_tag = f"{{{NS['w']}}}sdt"
    sdt_content_tag = f"{{{NS['w']}}}sdtContent"
    custom_xml_tag = f"{{{NS['w']}}}customXml"
    for child in container:
        if child.tag == tc_tag:
            yield child
        elif child.tag == sdt_tag:
            sc = child.find(sdt_content_tag)
            if sc is not None:
                yield from iter_row_cells(sc)
        elif child.tag in (sdt_content_tag, custom_xml_tag):
            yield from iter_row_cells(child)


_IMG_ONLY_RE = re.compile(r'^(?:\[\[IMAGE:[^\]]+\]\]\s*)+$')


def extract_table_rows(tbl, rid2target):
    """Extract rows from a <w:tbl> element as lists of cell strings (unwrapping <w:sdt>-wrapped rows/cells).
    Also inspects <w:tcPr> for <w:gridSpan> and <w:vMerge> so merged cells are explicitly annotated
    (e.g. '[colspan=2] Header' or '[vmerge-cont]'), preventing false 'shifted column header' accusations.
    """
    w_ns = NS['w']
    rows = []
    for tr in iter_table_rows(tbl):
        cells = []
        for tc in iter_row_cells(tr):
            span_prefix = ""
            tcPr = tc.find(f"{{{w_ns}}}tcPr")
            if tcPr is not None:
                gs = tcPr.find(f"{{{w_ns}}}gridSpan")
                if gs is not None:
                    val = gs.get(f"{{{w_ns}}}val", "1")
                    try:
                        if int(val) > 1:
                            span_prefix += f"[colspan={val}] "
                    except ValueError:
                        pass
                vm = tcPr.find(f"{{{w_ns}}}vMerge")
                if vm is not None:
                    vm_val = vm.get(f"{{{w_ns}}}val", "")
                    if vm_val != "restart":
                        span_prefix += "[vmerge-cont] "
            cell_paras = [
                para_text(p, rid2target).strip()
                for p in iter_body_blocks(tc)
                if p.tag == f"{{{w_ns}}}p"
            ]
            non_empty = [cp for cp in cell_paras if cp]
            if not non_empty:
                cells.append(span_prefix.strip())
            else:
                merged = [non_empty[0]]
                for cp in non_empty[1:]:
                    # Join image-only paragraphs to the following caption with a space instead of ' / '
                    if _IMG_ONLY_RE.match(merged[-1]):
                        merged[-1] = merged[-1] + ' ' + cp
                    else:
                        merged.append(cp)
                cells.append(span_prefix + (' / '.join(merged)))
        if any(cells):
            rows.append(cells)
    return rows


def extract_embedded_csl_json(z):
    """Extract embedded CSL-JSON citation records from Mendeley WebExtensions and Zotero/Mendeley w:instrText fields.

    Returns (csl_items_list, stats_dict) where stats_dict has keys:
      zotero_fields, mendeley_webext_citations, mendeley_fields, endnote_fields,
      unique_items, items_with_doi, items_with_abstract
    """
    stats = {
        'zotero_fields': 0,
        'mendeley_webext_citations': 0,
        'mendeley_fields': 0,
        'endnote_fields': 0,
        'unique_items': 0,
        'items_with_doi': 0,
        'items_with_abstract': 0,
    }
    items = []
    seen_keys = set()

    def _add_item(item_data, source_tag):
        if not isinstance(item_data, dict):
            return
        title = re.sub(r'\s+', ' ', str(item_data.get('title', '')).strip())
        doi = str(item_data.get('DOI', '')).strip()
        iid = str(item_data.get('id', '')).strip()
        if not title and not doi:
            return
        dedup_key = (doi.lower() if doi else '', title.lower()[:120] if title else iid)
        if dedup_key in seen_keys:
            return
        seen_keys.add(dedup_key)
        rec = dict(item_data)
        rec['_source'] = source_tag
        items.append(rec)

    # 1. Check Mendeley Cite WebExtension XMLs (word/webextensions/webextension*.xml)
    for name in sorted(z.namelist()):
        if 'webextension' in name.lower() and name.lower().endswith('.xml'):
            try:
                we_root = ET.fromstring(z.read(name))
            except Exception:
                continue
            for elem in we_root.iter():
                prop_name = elem.get('name', '')
                if 'MENDELEY_CITATIONS' in prop_name.upper() and prop_name.upper() == 'MENDELEY_CITATIONS':
                    val = elem.get('value', '')
                    if not val:
                        continue
                    try:
                        parsed = json.loads(val)
                        if isinstance(parsed, str):
                            parsed = json.loads(parsed)
                        if isinstance(parsed, list):
                            stats['mendeley_webext_citations'] += len(parsed)
                            for cite_obj in parsed:
                                if isinstance(cite_obj, dict):
                                    for ci in cite_obj.get('citationItems', []):
                                        if isinstance(ci, dict) and 'itemData' in ci:
                                            _add_item(ci['itemData'], 'mendeley_webextension')
                    except Exception:
                        pass

    # 2. Check word/document.xml field instructions (<w:instrText> grouped by <w:fldChar>)
    try:
        doc_root = ET.fromstring(z.read('word/document.xml'))
    except Exception:
        doc_root = None

    if doc_root is not None:
        fields = []
        cur = []
        in_fld = False
        for elem in doc_root.iter():
            if elem.tag == f"{{{NS['w']}}}fldChar":
                ftype = elem.get(f"{{{NS['w']}}}fldCharType")
                if ftype == 'begin':
                    in_fld = True
                    cur = []
                elif ftype in ('separate', 'end') and in_fld:
                    if cur:
                        fields.append(''.join(cur))
                    cur = []
                    if ftype == 'end':
                        in_fld = False
            elif elem.tag == f"{{{NS['w']}}}instrText":
                if in_fld:
                    cur.append(elem.text or '')
                else:
                    fields.append(elem.text or '')

        for f_str in fields:
            fu = f_str.upper()
            if 'ZOTERO' in fu or 'CSL_CITATION' in fu:
                stats['zotero_fields'] += 1
            if 'EN.CITE' in fu or 'ENDNOTE' in fu:
                stats['endnote_fields'] += 1
            if 'MENDELEY' in fu:
                stats['mendeley_fields'] += 1
            idx = f_str.find('CSL_CITATION')
            if idx != -1:
                j_str = f_str[idx + len('CSL_CITATION'):].strip()
                try:
                    d = json.loads(j_str)
                    for ci in d.get('citationItems', []):
                        if isinstance(ci, dict) and 'itemData' in ci:
                            _add_item(ci['itemData'], 'csl_citation_field')
                except Exception:
                    pass

    stats['unique_items'] = len(items)
    stats['items_with_doi'] = sum(1 for it in items if str(it.get('DOI', '')).strip())
    stats['items_with_abstract'] = sum(1 for it in items if str(it.get('abstract', '')).strip())
    return items, stats


def audit_formatting(z, body, rid2target):
    """Inspect Word line numbering, SDT/citation controls, empty spacer paragraphs, font-size overrides, and estimate line numbers."""
    ln_settings = []
    for sect in body.findall(f".//{{{NS['w']}}}lnNumType"):
        ln_settings.append({
            'countBy': sect.get(f"{{{NS['w']}}}countBy", '1'),
            'restart': sect.get(f"{{{NS['w']}}}restart", 'newPage'),
            'start': sect.get(f"{{{NS['w']}}}start", '1'),
        })

    print("== WORD FORMATTING & LINE-NUMBERING DIAGNOSTICS ==")
    if ln_settings:
        print(f"Word line numbering enabled in document: {ln_settings}")
    else:
        print("Word line numbering (<w:lnNumType>): not explicitly enabled in sectPr.")

    sdt_nodes = body.findall(f".//{{{NS['w']}}}sdt")
    sdt_paras = sum(len(s.findall(f".//{{{NS['w']}}}p")) for s in sdt_nodes)
    csl_items, cite_stats = extract_embedded_csl_json(z)
    zotero_fields = cite_stats['zotero_fields']
    endnote_fields = cite_stats['endnote_fields']
    mendeley_fields = cite_stats['mendeley_fields']
    mendeley_webext = cite_stats['mendeley_webext_citations']
    if sdt_nodes or zotero_fields or endnote_fields or mendeley_fields or mendeley_webext:
        print(
            f"Structured Document Tags (<w:sdt>): {len(sdt_nodes)} control(s) wrapping {sdt_paras} paragraph(s) "
            f"(extracted automatically by docx2txt.py)."
        )
        if zotero_fields or endnote_fields or mendeley_fields or mendeley_webext:
            print(
                f"  !! PRODUCTION NOTE: Active citation-manager metadata detected "
                f"(Zotero/CSL fields={zotero_fields}, Mendeley WebExtension citations={mendeley_webext}, "
                f"Mendeley fields={mendeley_fields}, EndNote={endnote_fields}). "
                f"Embedded CSL-JSON records: {cite_stats['unique_items']} unique items "
                f"({cite_stats['items_with_doi']} with DOI, {cite_stats['items_with_abstract']} with full abstract). "
                f"Consider unformatting citations to plain text before final journal submission if required by the journal pipeline."
            )
        elif sdt_paras > 0:
            print(
                f"  !! PRODUCTION NOTE: {sdt_paras} paragraph(s) live inside <w:sdt> content controls "
                f"(e.g., Zotero/Mendeley/Word bibliography controls). Consider converting content controls to static text before submission."
            )

    br_nodes = body.findall(f".//{{{NS['w']}}}br")
    if br_nodes:
        print(
            f"Soft line breaks (<w:br/>): {len(br_nodes)} break(s) detected "
            f"(split into [P#.1], [P#.2]... sub-anchors by --indexed)."
        )

    all_sizes = Counter()
    para_records = []
    p_idx = 0
    empty_unsuppressed = []
    merged_headings = []
    split_paragraphs = []

    merged_hdr_re = re.compile(
        r'([a-z\)\]]\s*(?:\[\d+(?:[,–\-]\d+)*\])?\s*\.+\s*(?:\[\d+(?:[,–\-]\d+)*\])?\s*\.*)'
        r'\s*(\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z0-9\s,\-–/()]{2,60})$'
    )
    stray_dot_hdr_re = re.compile(
        r'^\.+(\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z0-9\s,\-–/()]{2,60})$'
    )

    default_sz = "document-default"
    try:
        styles = ET.fromstring(z.read('word/styles.xml'))
        dd = styles.find(f".//{{{NS['w']}}}docDefaults//{{{NS['w']}}}sz")
        if dd is not None and dd.get(f"{{{NS['w']}}}val"):
            default_sz = f"{int(dd.get(f'{{{NS['w']}}}val')) / 2.0} pt"
    except KeyError:
        pass

    for child in iter_body_blocks(body):
        if child.tag == f"{{{NS['w']}}}p":
            p_idx += 1
            txt = para_text(child, rid2target).strip()
            sup = para_suppress_line_numbers(child)
            sizes = para_font_sizes(child)
            if txt:
                if sizes:
                    for s in set(sizes):
                        all_sizes[f"{s} pt"] += 1
                else:
                    all_sizes[default_sz] += 1
                m_mh = merged_hdr_re.search(txt)
                if m_mh:
                    merged_headings.append((p_idx, m_mh.group(1), m_mh.group(2)))
                elif stray_dot_hdr_re.match(txt) and len(txt) < 95:
                    merged_headings.append((p_idx, '.', stray_dot_hdr_re.match(txt).group(1)))
            if not txt and not sup:
                empty_unsuppressed.append(p_idx)
            para_records.append((p_idx, txt, sup, sizes))

    # Check for paragraphs accidentally split mid-sentence by a hard return
    non_empty_paras = [(pid, txt) for pid, txt, _, _ in para_records if txt]
    for i in range(len(non_empty_paras) - 1):
        pid1, txt1 = non_empty_paras[i]
        pid2, txt2 = non_empty_paras[i + 1]
        if len(txt1) >= 40 and len(txt2) >= 25:
            if (
                re.search(r'\b(?:of|in|to|for|with|and|or|by|from|that|which|the|a|an|as|at|on|into|between|within|during|under|over|through|[a-z]{4,})\s*$', txt1)
                and not re.search(r'[.!?:;)\]]\s*$', txt1)
                and re.match(r'^[a-z]{2,}\b', txt2)
            ):
                split_paragraphs.append((pid1, pid2, txt1[-45:], txt2[:45]))

    dominant_size = all_sizes.most_common(1)[0][0] if all_sizes else default_sz
    print(f"Total top-level paragraphs: {p_idx}")
    print(f"Empty spacer paragraphs without <w:suppressLineNumbers/>: {len(empty_unsuppressed)} "
          f"(indices: {empty_unsuppressed[:25]}{'...' if len(empty_unsuppressed) > 25 else ''})")
    if empty_unsuppressed and ln_settings:
        print("  !! NOTE: Empty paragraphs without line-number suppression are counted by Word's continuous")
        print("     line numbering, shifting downstream line numbers by up to +"
              f"{len(empty_unsuppressed)} lines depending on where they appear.")

    if merged_headings:
        for pid, pre_end, hdr_txt in merged_headings:
            print(f"  !! MERGED SECTION HEADING in [P{pid}]: missing paragraph break before '{hdr_txt}' (after '...{pre_end}')")

    if split_paragraphs:
        for pid1, pid2, tail1, head2 in split_paragraphs:
            print(f"  !! ACCIDENTAL MID-SENTENCE PARAGRAPH SPLIT between [P{pid1}] and [P{pid2}]: '...{tail1}' -> '{head2}...'")

    # Non-standard Greek / mathematical look-alike symbol scan (e.g. U+03D0 ϐ for β, U+03D2 ϒ for γ, U+2206 ∆ for Δ)
    lookalike_map = {
        '\u03d0': ('ϐ', 'U+03D0 GREEK BETA SYMBOL', 'β (U+03B2)'),
        '\u03d1': ('ϑ', 'U+03D1 GREEK THETA SYMBOL', 'θ (U+03B8)'),
        '\u03d2': ('ϒ', 'U+03D2 GREEK UPSILON WITH HOOK SYMBOL', 'γ (U+03B3) or υ (U+03C5)'),
        '\u03f0': ('ϰ', 'U+03F0 GREEK KAPPA SYMBOL', 'κ (U+03BA)'),
        '\u03f1': ('ϱ', 'U+03F1 GREEK RHO SYMBOL', 'ρ (U+03C1)'),
        '\u03f5': ('ϵ', 'U+03F5 GREEK LUNATE EPSILON SYMBOL', 'ε (U+03B5)'),
        '\u2206': ('∆', 'U+2206 INCREMENT OPERATOR', 'Δ (U+0394 GREEK CAPITAL LETTER DELTA)'),
        '\ufb01': ('ﬁ', 'U+FB01 LATIN SMALL LIGATURE FI', 'fi'),
        '\ufb02': ('ﬂ', 'U+FB02 LATIN SMALL LIGATURE FL', 'fl'),
    }
    lookalike_hits = {}
    for pid, txt, _, _ in para_records:
        if not txt:
            continue
        for ch, (glyph, name, std_repl) in lookalike_map.items():
            if ch in txt:
                # Extract up to 3 example words containing the character
                words = [w.strip('.,;:()[]') for w in txt.split() if ch in w][:3]
                lookalike_hits.setdefault(ch, []).append((pid, words))
    if lookalike_hits:
        print("\n  !! NON-STANDARD UNICODE / GREEK LOOK-ALIKE CHARACTERS DETECTED:")
        for ch, occs in lookalike_hits.items():
            glyph, name, std_repl = lookalike_map[ch]
            pids = [f"[P{p}]" for p, _ in occs[:12]]
            ex_words = []
            for _, ws in occs:
                for w in ws:
                    if w not in ex_words:
                        ex_words.append(w)
            print(f"     - '{glyph}' ({name}) instead of standard {std_repl} in {len(occs)} paragraph(s) "
                  f"({', '.join(pids)}{'...' if len(occs) > 12 else ''}); examples: {', '.join(ex_words[:5])}")

    if len(all_sizes) > 1:
        print(f"Paragraph font-size distribution: {dict(all_sizes)}")
        for pid, txt, _, sizes in para_records:
            if txt and sizes:
                uniq = [f"{s} pt" for s in sorted(set(sizes))]
                if any(u != dominant_size for u in uniq):
                    print(f"  !! Font-size override in [P{pid}] ({', '.join(uniq)} vs dominant {dominant_size}): {txt[:75]}...")

    print("\n== ESTIMATED CONTINUOUS LINE NUMBERS PER PARAGRAPH ==")
    print("  Columns: [ParaID] | NonEmptyLines (10pt) | WordWithEmpty (10pt..11pt) | Text preview")
    line_nonempty_10 = 0
    line_word_10 = 0
    line_word_11 = 0
    for pid, txt, sup, sizes in para_records:
        if not txt:
            if not sup:
                line_word_10 += 1
                line_word_11 += 1
            continue
        # Approximate characters per line on standard letter/A4 page with 1-inch margins (~6.5 in printable width)
        # 10pt ~ 92 chars/line; 11pt ~ 83 chars/line; 12pt ~ 75 chars/line
        eff_pt_10 = max(sizes) if sizes else 10.0
        eff_pt_11 = max(sizes) if sizes else 11.0
        cpl_10 = max(55, int(92 * (10.0 / eff_pt_10)))
        cpl_11 = max(55, int(92 * (10.0 / eff_pt_11)))

        # Greedy word wrap line count
        def count_lines(text, cpl):
            words = text.split()
            if not words:
                return 1
            lines, cur = 1, len(words[0])
            for w in words[1:]:
                if cur + 1 + len(w) <= cpl:
                    cur += 1 + len(w)
                else:
                    lines += 1
                    cur = len(w)
            return lines

        n10 = count_lines(txt, cpl_10)
        n11 = count_lines(txt, cpl_11)

        s_ne10, e_ne10 = line_nonempty_10 + 1, line_nonempty_10 + n10
        line_nonempty_10 = e_ne10

        if not sup:
            s_w10, e_w10 = line_word_10 + 1, line_word_10 + n10
            s_w11, e_w11 = line_word_11 + 1, line_word_11 + n11
            line_word_10 = e_w10
            line_word_11 = e_w11
            print(f"  [P{pid:<3d}] | L{s_ne10:<3d}-{e_ne10:<3d} | Word L{s_w10:<3d}-{e_w11:<3d} | {txt[:75]}")
        else:
            print(f"  [P{pid:<3d}] | L{s_ne10:<3d}-{e_ne10:<3d} | (suppressed)   | {txt[:75]}")


def extract_references_section(body, rid2target):
    """Locate the References/Bibliography section(s) (including Supplementary References) and return numbered entries."""
    ref_hdr_re = re.compile(
        r'^\s*(?:(?:\d+(?:\.\d+)*\.?\s+)?(?:supplementary\s+|supporting\s+|si\s+)?'
        r'(?:references|references\s+and\s+notes|literature\s+cited|bibliography|works\s+cited))\s*:?\s*$',
        re.I
    )
    end_hdr_re = re.compile(
        r'^\s*(?:acknowledg(?:e)?ments?|author\s+contributions?|competing\s+(?:financial\s+)?interests?|'
        r'declaration\s+of\s+interests?|conflicts?\s+of\s+interest|funding|data\s+(?:and\s+code\s+)?availability|'
        r'figure\s+legends?|main\s+figure\s+legends?|tables?|supplementary\s+(?:information|materials?|figures?|tables?|notes?|methods?|text)|'
        r'extended\s+data|star\s*[\s★*]?\s*methods|materials?\s+and\s+methods|experimental\s+procedures)\s*:?\s*$',
        re.I
    )
    num_lead_re = re.compile(r'^\s*(?:\[(S?\d+)\]|(S?\d+)[\.\)]|\((S?\d+)\))\s+(.*\S)\s*$', re.I)

    in_refs = False
    is_supp_section = False
    extracted = []
    auto_num = 0
    p_idx = 0

    for child in iter_body_blocks(body):
        if child.tag != f"{{{NS['w']}}}p":
            continue
        p_idx += 1
        t = re.sub(r'\s+', ' ', para_text(child, rid2target)).strip()
        if not t:
            continue

        if ref_hdr_re.match(t):
            in_refs = True
            is_supp_section = bool(re.search(r'\b(?:supplementary|supporting|si)\b', t, re.I))
            auto_num = 0
            continue

        if not in_refs:
            continue

        # Stop current references block if we hit a post-References section heading (but keep scanning for Supplementary References)
        if end_hdr_re.match(t) and len(extracted) >= 1:
            in_refs = False
            continue
        if len(t) < 65 and not num_lead_re.match(t) and extracted and not re.search(r'\b(?:19|20)\d{2}\b|doi|et\s+al', t, re.I):
            if re.match(r'^[A-Z][A-Za-z0-9\s,\-–—/()]{2,55}$', t):
                in_refs = False
                continue

        m = num_lead_re.match(t)
        if m:
            raw_id = (m.group(1) or m.group(2) or m.group(3)).upper()
            rest = m.group(4).strip()
            if raw_id.startswith('S'):
                num = raw_id
                try:
                    auto_num = int(raw_id[1:])
                except ValueError:
                    pass
            else:
                num = int(raw_id)
                auto_num = num
                if is_supp_section and any(str(x[1]) == str(num) for x in extracted):
                    num = f"S{num}"
            extracted.append((p_idx, num, rest))
        else:
            # Check if Word auto-numbering (<w:numPr>) is present or if it looks like an unnumbered bibliography entry
            has_numpr = child.find(f".//{{{NS['w']}}}numPr") is not None
            if has_numpr or re.search(r'\b(?:19|20)\d{2}[a-z]?\b', t):
                auto_num += 1
                num_label = f"S{auto_num}" if (is_supp_section and any(x[1] == auto_num for x in extracted)) else auto_num
                extracted.append((p_idx, num_label, t))
            elif extracted:
                # Continuation paragraph of previous reference
                prev_pid, prev_num, prev_txt = extracted[-1]
                extracted[-1] = (prev_pid, prev_num, prev_txt + " " + t)

    return extracted


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help') or '--help' in sys.argv:
        sys.exit(__doc__)

    indexed = '--indexed' in sys.argv
    want_media = '--media' in sys.argv
    want_audit = '--audit-format' in sys.argv
    want_refs = '--extract-refs' in sys.argv
    want_csl = '--extract-csl' in sys.argv

    consumed_indices = {0}
    for flag in ('--indexed', '--media', '--audit-format', '--extract-refs', '--extract-csl', '--outdir'):
        if flag in sys.argv:
            f_idx = sys.argv.index(flag)
            consumed_indices.add(f_idx)
            if flag in ('--extract-refs', '--extract-csl', '--outdir', '--media'):
                if f_idx + 1 < len(sys.argv) and not sys.argv[f_idx + 1].startswith('-'):
                    # Only consume next arg as flag value if it is not the input .docx file
                    if not sys.argv[f_idx + 1].lower().endswith('.docx'):
                        consumed_indices.add(f_idx + 1)

    pos_args = [arg for idx_a, arg in enumerate(sys.argv) if idx_a not in consumed_indices and not arg.startswith('-')]
    if not pos_args:
        sys.exit(__doc__)
    path = pos_args[0]
    pos_outfile = pos_args[1] if len(pos_args) >= 2 else None

    refs_out = None
    if want_refs:
        r_idx = sys.argv.index('--extract-refs')
        if r_idx + 1 < len(sys.argv) and not sys.argv[r_idx + 1].startswith('-') and sys.argv[r_idx + 1] != path:
            refs_out = sys.argv[r_idx + 1]
    csl_out = None
    if want_csl:
        c_idx = sys.argv.index('--extract-csl')
        if c_idx + 1 < len(sys.argv) and not sys.argv[c_idx + 1].startswith('-') and sys.argv[c_idx + 1] != path:
            csl_out = sys.argv[c_idx + 1]
    outdir = 'docx_media'
    if '--outdir' in sys.argv:
        outdir = sys.argv[sys.argv.index('--outdir') + 1]
    elif want_media:
        m_idx = sys.argv.index('--media')
        if m_idx + 1 < len(sys.argv) and not sys.argv[m_idx + 1].startswith('-') and sys.argv[m_idx + 1] != path:
            outdir = sys.argv[m_idx + 1]

    out_fh = None
    orig_stdout = sys.stdout
    if pos_outfile and not (want_refs or want_csl):
        os.makedirs(os.path.dirname(os.path.abspath(pos_outfile)), exist_ok=True)
        out_fh = open(pos_outfile, 'w', encoding='utf-8')
        sys.stdout = out_fh

    z = zipfile.ZipFile(path)
    rid2target = {}
    try:
        rt = ET.fromstring(z.read('word/_rels/document.xml.rels'))
        for rel in rt:
            rid2target[rel.get('Id')] = rel.get('Target', '')
    except KeyError:
        pass

    body = ET.fromstring(z.read('word/document.xml')).find(f"{{{NS['w']}}}body")

    if want_csl:
        csl_items, cite_stats = extract_embedded_csl_json(z)
        payload = json.dumps({'stats': cite_stats, 'items': csl_items}, indent=2, ensure_ascii=False)
        if csl_out:
            os.makedirs(os.path.dirname(os.path.abspath(csl_out)), exist_ok=True)
            with open(csl_out, 'w', encoding='utf-8') as f:
                f.write(payload + '\n')
            print(
                f"# extracted {len(csl_items)} embedded CSL items "
                f"({cite_stats['items_with_doi']} DOIs, {cite_stats['items_with_abstract']} abstracts) to {csl_out}",
                file=sys.stderr,
            )
        else:
            print(payload)
            return
        if not (indexed or want_audit or want_refs):
            return

    if want_refs:
        refs = extract_references_section(body, rid2target)
        lines = [f"{num}.\t{txt}" for _, num, txt in refs]
        if refs_out:
            os.makedirs(os.path.dirname(os.path.abspath(refs_out)), exist_ok=True)
            with open(refs_out, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + ('\n' if lines else ''))
            print(f"# extracted {len(refs)} references to {refs_out}", file=sys.stderr)
        else:
            for ln in lines:
                print(ln)
            print(f"# extracted {len(refs)} references", file=sys.stderr)
        return

    if want_audit:
        audit_formatting(z, body, rid2target)
        return

    if want_media and not indexed:
        os.makedirs(outdir, exist_ok=True)
        n = 0
        for name in sorted(z.namelist()):
            if name.startswith('word/media/') and not name.endswith('/'):
                dst = os.path.join(outdir, os.path.basename(name))
                with z.open(name) as src, open(dst, 'wb') as f:
                    shutil.copyfileobj(src, f)
                n += 1
                print(f"# media[{n}]: {dst} ({os.path.getsize(dst)} bytes)", file=sys.stderr)
        print(f"# extracted {n} media files to {outdir}/", file=sys.stderr)
        return

    if indexed:
        try:
            with open(path, 'rb') as bf:
                sha_prefix = hashlib.sha256(bf.read()).hexdigest()[:16]
            fsize = os.path.getsize(path)
            print(
                f"# MANUSCRIPT-AUDIT EXTRACT | file={os.path.basename(path)} "
                f"| path={os.path.abspath(path)} | sha256={sha_prefix} | bytes={fsize}"
            )
        except OSError:
            pass

    p_idx = 0
    t_idx = 0
    for child in iter_body_blocks(body):
        if child.tag == f"{{{NS['w']}}}p":
            p_idx += 1
            if indexed:
                raw_t = para_text(child, rid2target, split_br=True)
                sub_lines = [ln.strip() for ln in raw_t.splitlines() if ln.strip()]
                if len(sub_lines) == 1:
                    print(f"[P{p_idx}] {sub_lines[0]}")
                elif len(sub_lines) > 1:
                    for sub_i, sub_ln in enumerate(sub_lines, 1):
                        print(f"[P{p_idx}.{sub_i}] {sub_ln}")
            else:
                t = para_text(child, rid2target, split_br=True).strip()
                if t:
                    print(t)
        elif child.tag == f"{{{NS['w']}}}tbl":
            t_idx += 1
            rows = extract_table_rows(child, rid2target)
            if rows:
                if indexed:
                    print(f"[TABLE {t_idx}]")
                    for r_i, row in enumerate(rows, 1):
                        print(f"  [Row {r_i}] " + " | ".join(row))
                else:
                    for row in rows:
                        print("\t".join(row))

    if want_media:
        os.makedirs(outdir, exist_ok=True)
        n = 0
        for name in sorted(z.namelist()):
            if name.startswith('word/media/') and not name.endswith('/'):
                dst = os.path.join(outdir, os.path.basename(name))
                with z.open(name) as src, open(dst, 'wb') as f:
                    shutil.copyfileobj(src, f)
                n += 1
                print(f"# media[{n}]: {dst} ({os.path.getsize(dst)} bytes)", file=sys.stderr)
        print(f"# extracted {n} media files to {outdir}/", file=sys.stderr)

    if out_fh is not None:
        sys.stdout = orig_stdout
        out_fh.close()


if __name__ == '__main__':
    main()
