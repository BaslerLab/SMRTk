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
"""
import sys
import os
import re
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


def extract_table_rows(tbl, rid2target):
    """Extract rows from a <w:tbl> element as lists of cell strings."""
    rows = []
    for tr in tbl.findall(f"{{{NS['w']}}}tr"):
        cells = []
        for tc in tr.findall(f"{{{NS['w']}}}tc"):
            cell_paras = [
                para_text(p, rid2target).strip()
                for p in iter_body_blocks(tc)
                if p.tag == f"{{{NS['w']}}}p"
            ]
            cells.append(' / '.join(cp for cp in cell_paras if cp))
        if any(cells):
            rows.append(cells)
    return rows


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
    instr_texts = [n.text or "" for n in body.findall(f".//{{{NS['w']}}}instrText")]
    zotero_fields = sum(1 for t in instr_texts if "ZOTERO" in t.upper() or "CSL_CITATION" in t.upper())
    endnote_fields = sum(1 for t in instr_texts if "EN.CITE" in t.upper() or "ENDNOTE" in t.upper())
    mendeley_fields = sum(1 for t in instr_texts if "MENDELEY" in t.upper())
    if sdt_nodes or zotero_fields or endnote_fields or mendeley_fields:
        print(
            f"Structured Document Tags (<w:sdt>): {len(sdt_nodes)} control(s) wrapping {sdt_paras} paragraph(s) "
            f"(extracted automatically by docx2txt.py)."
        )
        if zotero_fields or endnote_fields or mendeley_fields:
            print(
                f"  !! PRODUCTION NOTE: Active citation-manager fields detected "
                f"(Zotero={zotero_fields}, EndNote={endnote_fields}, Mendeley={mendeley_fields}). "
                f"Consider unformatting citations to plain text before final journal submission if required by the journal pipeline."
            )
        elif sdt_paras > 0:
            print(
                f"  !! PRODUCTION NOTE: {sdt_paras} paragraph(s) live inside <w:sdt> content controls "
                f"(e.g., Zotero/Word bibliography controls). Consider converting content controls to static text before submission."
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
            if not txt and not sup:
                empty_unsuppressed.append(p_idx)
            para_records.append((p_idx, txt, sup, sizes))

    dominant_size = all_sizes.most_common(1)[0][0] if all_sizes else default_sz
    print(f"Total top-level paragraphs: {p_idx}")
    print(f"Empty spacer paragraphs without <w:suppressLineNumbers/>: {len(empty_unsuppressed)} "
          f"(indices: {empty_unsuppressed[:25]}{'...' if len(empty_unsuppressed) > 25 else ''})")
    if empty_unsuppressed and ln_settings:
        print("  !! NOTE: Empty paragraphs without line-number suppression are counted by Word's continuous")
        print("     line numbering, shifting downstream line numbers by up to +"
              f"{len(empty_unsuppressed)} lines depending on where they appear.")

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
    """Locate the References/Bibliography section and return clean numbered reference entries."""
    ref_hdr_re = re.compile(
        r'^\s*(?:references|references\s+and\s+notes|literature\s+cited|bibliography|works\s+cited)\s*:?\s*$',
        re.I
    )
    end_hdr_re = re.compile(
        r'^\s*(?:acknowledg(?:e)?ments?|author\s+contributions?|competing\s+(?:financial\s+)?interests?|'
        r'declaration\s+of\s+interests?|conflicts?\s+of\s+interest|funding|data\s+(?:and\s+code\s+)?availability|'
        r'figure\s+legends?|main\s+figure\s+legends?|tables?|supplementary\s+(?:information|materials?|figures?|tables?)|'
        r'extended\s+data|star\s*[\s★*]?\s*methods|materials?\s+and\s+methods|experimental\s+procedures)\s*:?\s*$',
        re.I
    )
    num_lead_re = re.compile(r'^\s*(?:\[(\d+)\]|(\d+)[\.\)]|\((\d+)\))\s+(.*\S)\s*$')

    in_refs = False
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

        if not in_refs:
            if ref_hdr_re.match(t):
                in_refs = True
            continue

        # Stop if we hit a post-References section heading
        if end_hdr_re.match(t) and len(extracted) >= 3:
            break
        if len(t) < 65 and not num_lead_re.match(t) and extracted and not re.search(r'\b(?:19|20)\d{2}\b|doi|et\s+al', t, re.I):
            # Likely a new section header after references
            if re.match(r'^[A-Z][A-Za-z0-9\s,\-–—/()]{2,55}$', t):
                break

        m = num_lead_re.match(t)
        if m:
            num = int(m.group(1) or m.group(2) or m.group(3))
            rest = m.group(4).strip()
            auto_num = num
            extracted.append((p_idx, num, rest))
        else:
            # Check if Word auto-numbering (<w:numPr>) is present or if it looks like an unnumbered bibliography entry
            has_numpr = child.find(f".//{{{NS['w']}}}numPr") is not None
            if has_numpr or re.search(r'\b(?:19|20)\d{2}[a-z]?\b', t):
                auto_num += 1
                extracted.append((p_idx, auto_num, t))
            elif extracted:
                # Continuation paragraph of previous reference
                prev_pid, prev_num, prev_txt = extracted[-1]
                extracted[-1] = (prev_pid, prev_num, prev_txt + " " + t)

    return extracted


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        sys.exit(__doc__)

    path = sys.argv[1]
    indexed = '--indexed' in sys.argv
    want_media = '--media' in sys.argv
    want_audit = '--audit-format' in sys.argv
    want_refs = '--extract-refs' in sys.argv
    refs_out = None
    if want_refs:
        r_idx = sys.argv.index('--extract-refs')
        if r_idx + 1 < len(sys.argv) and not sys.argv[r_idx + 1].startswith('-'):
            refs_out = sys.argv[r_idx + 1]
    outdir = 'docx_media'
    if '--outdir' in sys.argv:
        outdir = sys.argv[sys.argv.index('--outdir') + 1]
    elif want_media:
        m_idx = sys.argv.index('--media')
        if m_idx + 1 < len(sys.argv) and not sys.argv[m_idx + 1].startswith('-'):
            outdir = sys.argv[m_idx + 1]

    z = zipfile.ZipFile(path)
    rid2target = {}
    try:
        rt = ET.fromstring(z.read('word/_rels/document.xml.rels'))
        for rel in rt:
            rid2target[rel.get('Id')] = rel.get('Target', '')
    except KeyError:
        pass

    body = ET.fromstring(z.read('word/document.xml')).find(f"{{{NS['w']}}}body")

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
            print(f"# MANUSCRIPT-AUDIT EXTRACT | file={os.path.basename(path)} | sha256={sha_prefix} | bytes={fsize}")
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


if __name__ == '__main__':
    main()
