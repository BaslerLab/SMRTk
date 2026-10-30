#!/usr/bin/env python3
"""Insert native Word Track Changes (<w:del>/<w:ins>) and Margin Comments into a copy of a .docx file.

Zero external dependencies (uses Python standard library `zipfile` + `xml.etree.ElementTree`).
Preserves 100% of original document formatting, styles, tables, and embedded media.

Usage:
  annotate_docx.py INPUT.docx OUTPUT.docx EDITS.json [--author "Manuscript Audit"]

JSON specification format (`EDITS.json` is a list of edit/comment objects):
[
  {
    "para": 34,                         // optional: 1-based top-level paragraph index [P34] to restrict search
    "target": "CMP-1006-treated",       // exact substring in paragraph (or table cell) to match
    "replacement": "CMP-1060-treated",  // optional: if present and != target, inserts native <w:del> + <w:ins>
    "severity": "MINOR",                // optional badge prefix in comment: CRITICAL | MAJOR | MODERATE | MINOR
    "category": "Typo",                 // optional category label in comment header
    "comment": "Compound code typo: change 'CMP-1006' to 'CMP-1060'." // optional: if present, attaches a Word margin comment
  }
]
"""
import sys
import os
import re
import copy
import json
import difflib
import zipfile
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

# 1-to-1 character translation table so smart quotes, dashes, NBSP, Δ/∆, and µ/μ match seamlessly
_TYPO_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201b": "'", "\u2032": "'", "\u02bc": "'",
    "\u201c": '"', "\u201d": '"', "\u201f": '"', "\u2033": '"',
    "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u2007": " ", "\u2002": " ", "\u2003": " ",
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2011": "-", "\u2010": "-",
    "\u2206": "\u0394",
    "\u00b5": "\u03bc",
})


def norm_typo(s):
    """Length-preserving (1-char-to-1-char) normalization of quotes, dashes, spaces, Δ, and μ."""
    return s.translate(_TYPO_MAP)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("w", W_NS)
ET.register_namespace("r", R_NS)
ET.register_namespace("wp", "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing")
ET.register_namespace("a", "http://schemas.openxmlformats.org/drawingml/2006/main")
ET.register_namespace("pic", "http://schemas.openxmlformats.org/drawingml/2006/picture")
ET.register_namespace("mc", "http://schemas.openxmlformats.org/markup-compatibility/2006")
ET.register_namespace("w14", "http://schemas.microsoft.com/office/word/2010/wordml")
ET.register_namespace("w15", "http://schemas.microsoft.com/office/word/2012/wordml")


def wtag(local):
    return f"{{{W_NS}}}{local}"


def iter_body_blocks(container):
    """Yield <w:p> and <w:tbl> elements in document order, unwrapping <w:sdt>/<w:sdtContent> and <w:customXml>."""
    p_tag = wtag("p")
    tbl_tag = wtag("tbl")
    sdt_tag = wtag("sdt")
    sdt_content_tag = wtag("sdtContent")
    custom_xml_tag = wtag("customXml")
    for child in container:
        if child.tag in (p_tag, tbl_tag):
            yield child
        elif child.tag == sdt_tag:
            sc = child.find(sdt_content_tag)
            if sc is not None:
                yield from iter_body_blocks(sc)
        elif child.tag in (sdt_content_tag, custom_xml_tag):
            yield from iter_body_blocks(child)


def unwrap_inline_containers_in_para(p_elem):
    """Unwrap inline <w:sdt>, <w:fldSimple>, <w:smartTag>, and <w:hyperlink> inside a <w:p>,
    and split mixed <w:t> + <w:br/> runs so soft line breaks (<w:br/>) are never lost during run splitting."""
    inline_wrappers = {wtag("sdt"), wtag("sdtContent"), wtag("fldSimple"), wtag("smartTag"), wtag("hyperlink"), wtag("customXml")}
    changed = True
    while changed:
        changed = False
        for idx, child in enumerate(list(p_elem)):
            if child.tag in inline_wrappers:
                inner_runs = [el for el in child.iter(wtag("r"))]
                pos = list(p_elem).index(child)
                p_elem.remove(child)
                for offset, r_el in enumerate(inner_runs):
                    p_elem.insert(pos + offset, r_el)
                changed = True
                break

    # Split any <w:r> that mixes <w:br/>/<w:cr/> with <w:t> into separate <w:r> elements
    for r_el in list(p_elem):
        if r_el.tag != wtag("r"):
            continue
        has_br = any(c.tag in (wtag("br"), wtag("cr")) for c in r_el)
        has_t = any(c.tag == wtag("t") for c in r_el)
        if has_br and (has_t or sum(1 for c in r_el if c.tag in (wtag("br"), wtag("cr"))) > 1):
            rpr = r_el.find(wtag("rPr"))
            sub_runs = []
            for c in r_el:
                if c.tag == wtag("rPr"):
                    continue
                nr = ET.Element(wtag("r"))
                if rpr is not None:
                    nr.append(copy.deepcopy(rpr))
                nr.append(copy.deepcopy(c))
                sub_runs.append(nr)
            if sub_runs:
                pos = list(p_elem).index(r_el)
                p_elem.remove(r_el)
                for off, sr in enumerate(sub_runs):
                    p_elem.insert(pos + off, sr)


def get_para_full_text(p_elem):
    """Return full visible text of a paragraph (including runs inside inline <w:sdt>/<w:hyperlink>)."""
    parts = []
    for r_el in p_elem.iter(wtag("r")):
        parts.append(get_run_text(r_el))
    return "".join(parts)


def get_run_text(r_elem):
    """Return concatenated text of <w:t> children inside a single <w:r> element."""
    parts = []
    for child in r_elem:
        if child.tag == wtag("t"):
            parts.append(child.text or "")
        elif child.tag == wtag("tab"):
            parts.append("\t")
        elif child.tag in (wtag("br"), wtag("cr")):
            parts.append(" ")
    return "".join(parts)


def make_text_run(text, rpr_template=None):
    """Create a <w:r> element with optional cloned <w:rPr> and a <w:t xml:space='preserve'> child."""
    r = ET.Element(wtag("r"))
    if rpr_template is not None:
        r.append(copy.deepcopy(rpr_template))
    t = ET.SubElement(r, wtag("t"))
    t.set(f"{{{XML_NS}}}space", "preserve")
    t.text = text
    return r


def make_del_run(text, rpr_template=None):
    """Create a <w:r> element with <w:delText xml:space='preserve'> for use inside <w:del>."""
    r = ET.Element(wtag("r"))
    if rpr_template is not None:
        r.append(copy.deepcopy(rpr_template))
    dt = ET.SubElement(r, wtag("delText"))
    dt.set(f"{{{XML_NS}}}space", "preserve")
    dt.text = text
    return r


def isolate_span_in_paragraph(p_elem, start_char, end_char):
    """Split <w:r> elements in `p_elem` so that the character span [start_char, end_char)
    corresponds to an exact contiguous slice of child elements in `p_elem`.

    Returns (first_child_idx, last_child_idx_exclusive, template_rPr) or None if span cannot be isolated.
    """
    # Build mapping of direct <w:r> children and their character offsets
    runs_info = []  # (child_index_in_p, r_elem, text_str, char_start, char_end)
    cur_pos = 0
    for idx, child in enumerate(list(p_elem)):
        if child.tag == wtag("r"):
            txt = get_run_text(child)
            if txt:
                runs_info.append((idx, child, txt, cur_pos, cur_pos + len(txt)))
                cur_pos += len(txt)

    if not runs_info or start_char < 0 or end_char > cur_pos or start_char >= end_char:
        return None

    # Find which runs overlap [start_char, end_char)
    overlapping = [info for info in runs_info if info[4] > start_char and info[3] < end_char]
    if not overlapping:
        return None

    # Step 1: If the last overlapping run extends past end_char, split it at end_char
    last_idx, last_r, last_txt, last_s, last_e = overlapping[-1]
    if end_char < last_e:
        cut = end_char - last_s
        prefix_txt = last_txt[:cut]
        suffix_txt = last_txt[cut:]
        rpr = last_r.find(wtag("rPr"))
        r_pre = make_text_run(prefix_txt, rpr)
        r_suf = make_text_run(suffix_txt, rpr)
        pos_in_p = list(p_elem).index(last_r)
        p_elem.remove(last_r)
        p_elem.insert(pos_in_p, r_suf)
        p_elem.insert(pos_in_p, r_pre)
        # Update overlapping entry if first == last
        if len(overlapping) == 1:
            overlapping[0] = (pos_in_p, r_pre, prefix_txt, last_s, end_char)
        else:
            overlapping[-1] = (pos_in_p, r_pre, prefix_txt, last_s, end_char)

    # Step 2: If the first overlapping run starts before start_char, split it at start_char
    first_idx, first_r, first_txt, first_s, first_e = overlapping[0]
    if start_char > first_s:
        cut = start_char - first_s
        prefix_txt = first_txt[:cut]
        suffix_txt = first_txt[cut:]
        rpr = first_r.find(wtag("rPr"))
        r_pre = make_text_run(prefix_txt, rpr)
        r_suf = make_text_run(suffix_txt, rpr)
        pos_in_p = list(p_elem).index(first_r)
        p_elem.remove(first_r)
        p_elem.insert(pos_in_p, r_suf)
        p_elem.insert(pos_in_p, r_pre)
        overlapping[0] = (pos_in_p + 1, r_suf, suffix_txt, start_char, first_e)

    children_now = list(p_elem)
    first_child_pos = children_now.index(overlapping[0][1])
    last_child_pos = children_now.index(overlapping[-1][1]) + 1
    template_rpr = overlapping[0][1].find(wtag("rPr"))
    return first_child_pos, last_child_pos, template_rpr


def minimize_diff_span(target, replacement):
    """Trim common prefix/suffix at word boundaries so Track Changes highlights the minimal changed phrase."""
    if not replacement or norm_typo(target) == norm_typo(replacement):
        return 0, len(target), target, replacement

    nt = norm_typo(target)
    nr = norm_typo(replacement)

    # Trim common prefix up to the last whitespace before difference
    pre_len = 0
    max_pre = min(len(nt), len(nr))
    while pre_len < max_pre and nt[pre_len] == nr[pre_len]:
        pre_len += 1
    while pre_len > 0 and nt[pre_len - 1] not in (" ", "\t"):
        pre_len -= 1

    # Trim common suffix back to the first whitespace/punctuation after difference
    suf_len = 0
    max_suf = min(len(nt) - pre_len, len(nr) - pre_len)
    while suf_len < max_suf and nt[len(nt) - 1 - suf_len] == nr[len(nr) - 1 - suf_len]:
        suf_len += 1
    while suf_len > 0 and nt[len(nt) - suf_len] not in (" ", "\t", ".", ",", ";", ":", ")", "("):
        suf_len -= 1

    t_mid = target[pre_len:len(target) - suf_len]
    r_mid = replacement[pre_len:len(replacement) - suf_len]
    if not t_mid and not r_mid:
        return 0, len(target), target, replacement
    return pre_len, len(target) - suf_len, t_mid, r_mid


def _find_target_span(full_text, target):
    """Find (start, end) of `target` in `full_text` using exact, typographic-normalized,
    flexible-whitespace, or punctuation/citation-boundary fallback matching."""
    pos = full_text.find(target)
    if pos != -1:
        return pos, pos + len(target)

    nf = norm_typo(full_text)
    nt = norm_typo(target)
    pos = nf.find(nt)
    if pos != -1:
        return pos, pos + len(target)

    # Fallback 1: flexible whitespace + typographic normalization
    tokens = [re.escape(tok) for tok in nt.split() if tok]
    if tokens:
        pat = re.compile(r"\s+".join(tokens))
        m = pat.search(nf)
        if m:
            return m.start(), m.end()

    # Fallback 2: tolerate optional whitespace around punctuation and optional superscript citation numbers
    # e.g. "technology18 . For" <-> "technology18. For" <-> "technology. For"
    sub_tokens = [t for t in re.split(r"(\s+|[.,;:?!()\[\]])", nt) if t and not t.isspace()]
    if sub_tokens:
        pat_parts = []
        for tok in sub_tokens:
            # Also allow stripping trailing digits if tok is word+digits (e.g. technology18 -> technology(?:\d+)?)
            m_wd = re.match(r"^([A-Za-z]{3,})(\d{1,3})$", tok)
            if m_wd:
                pat_parts.append(re.escape(m_wd.group(1)) + r"(?:\s*\d{1,3}(?:[,-]\d{1,3})*)?")
            else:
                pat_parts.append(re.escape(tok))
        cite_bridge = r"(?:\s*\d{1,3}(?:[,;\s-]\d{1,3})*)?\s*"
        pat2 = re.compile(cite_bridge.join(pat_parts))
        m2 = pat2.search(nf)
        if m2:
            return m2.start(), m2.end()
    return None


def get_para_subline_ranges(p_elem):
    """Return a list of (start_char, end_char) spans in `get_para_full_text(p_elem)` for each
    non-empty line separated by soft line breaks (<w:br/> or <w:cr/>), matching docx2txt.py [P#.sub]."""
    spans = []
    cur_pos = 0
    seg_start = 0
    seg_chars = []
    for r_el in p_elem.iter(wtag("r")):
        for child in r_el:
            if child.tag == wtag("t"):
                t = child.text or ""
                seg_chars.append(t)
                cur_pos += len(t)
            elif child.tag == wtag("tab"):
                seg_chars.append("\t")
                cur_pos += 1
            elif child.tag in (wtag("br"), wtag("cr")):
                if "".join(seg_chars).strip():
                    spans.append((seg_start, cur_pos))
                cur_pos += 1  # get_run_text emits ' ' for <w:br/>
                seg_start = cur_pos
                seg_chars = []
    if "".join(seg_chars).strip():
        spans.append((seg_start, cur_pos))
    return spans


def apply_edit_to_paragraph(p_elem, edit, rev_id_counter, comment_id_counter, author, date_str, comments_list):
    """Attempt to apply `edit` to `p_elem`. Returns True if matched and applied."""
    target = edit["target"]
    para_sub = edit.get("para_sub")

    def _locate(text_str, elem_for_sub):
        if para_sub is not None and para_sub >= 1:
            sub_ranges = get_para_subline_ranges(elem_for_sub)
            if para_sub <= len(sub_ranges):
                s_lo, s_hi = sub_ranges[para_sub - 1]
                sub_span = _find_target_span(text_str[s_lo:s_hi], target)
                if sub_span is not None:
                    return s_lo + sub_span[0], s_lo + sub_span[1]
        return _find_target_span(text_str, target)

    full_text_direct = "".join(get_run_text(c) for c in p_elem if c.tag == wtag("r"))
    span = _locate(full_text_direct, p_elem)
    if span is None:
        # Check if unwrapping inline <w:sdt>/<w:hyperlink>/<w:fldSimple> reveals the target
        full_text_all = get_para_full_text(p_elem)
        if _locate(full_text_all, p_elem) is not None:
            unwrap_inline_containers_in_para(p_elem)
            full_text = "".join(get_run_text(c) for c in p_elem if c.tag == wtag("r"))
            span = _locate(full_text, p_elem)
        else:
            return False
    else:
        # Even if matched directly, if the paragraph has mixed <w:t>+<w:br/> runs and we are targeting a sub-line,
        # unwrap/split <w:br/> runs so isolate_span_in_paragraph never clobbers a <w:br/>
        if any(
            any(c.tag in (wtag("br"), wtag("cr")) for c in r_el) and any(c.tag == wtag("t") for c in r_el)
            for r_el in p_elem if r_el.tag == wtag("r")
        ):
            unwrap_inline_containers_in_para(p_elem)
            full_text = "".join(get_run_text(c) for c in p_elem if c.tag == wtag("r"))
            span = _locate(full_text, p_elem)
        else:
            full_text = full_text_direct

    if span is None:
        return False
    pos, end_pos = span
    matched_doc_target = full_text[pos:end_pos]

    replacement = edit.get("replacement")
    comment_txt = edit.get("comment")
    has_replacement = replacement is not None and norm_typo(replacement) != norm_typo(matched_doc_target)

    if has_replacement:
        sub_s, sub_e, del_str, ins_str = minimize_diff_span(matched_doc_target, replacement)
        span_start = pos + sub_s
        span_end = pos + sub_e
    else:
        span_start = pos
        span_end = end_pos
        del_str, ins_str = matched_doc_target, None

    if has_replacement and len(del_str) == 0:
        span_start = pos
        span_end = end_pos
        del_str = matched_doc_target
        ins_str = replacement

    iso = isolate_span_in_paragraph(p_elem, span_start, span_end)
    if iso is None:
        return False
    first_pos, last_pos, rpr_template = iso

    children_now = list(p_elem)
    target_elems = children_now[first_pos:last_pos]

    # Remove target_elems from p_elem so we can replace/wrap them cleanly
    for el in target_elems:
        p_elem.remove(el)

    insert_cursor = first_pos
    cid = None
    if comment_txt:
        cid = str(comment_id_counter[0])
        comment_id_counter[0] += 1
        crs = ET.Element(wtag("commentRangeStart"))
        crs.set(wtag("id"), cid)
        p_elem.insert(insert_cursor, crs)
        insert_cursor += 1
        comments_list.append({
            "id": cid,
            "severity": edit.get("severity", ""),
            "category": edit.get("category", ""),
            "text": comment_txt,
        })

    if has_replacement:
        # Build <w:del>
        if del_str:
            del_id = str(rev_id_counter[0])
            rev_id_counter[0] += 1
            del_el = ET.Element(wtag("del"))
            del_el.set(wtag("id"), del_id)
            del_el.set(wtag("author"), author)
            del_el.set(wtag("date"), date_str)
            for orig_el in target_elems:
                if orig_el.tag == wtag("r"):
                    txt = get_run_text(orig_el)
                    rpr = orig_el.find(wtag("rPr"))
                    del_el.append(make_del_run(txt, rpr))
            p_elem.insert(insert_cursor, del_el)
            insert_cursor += 1

        # Build <w:ins>
        if ins_str:
            ins_id = str(rev_id_counter[0])
            rev_id_counter[0] += 1
            ins_el = ET.Element(wtag("ins"))
            ins_el.set(wtag("id"), ins_id)
            ins_el.set(wtag("author"), author)
            ins_el.set(wtag("date"), date_str)
            ins_el.append(make_text_run(ins_str, rpr_template))
            p_elem.insert(insert_cursor, ins_el)
            insert_cursor += 1
    else:
        # Keep original runs intact inside the comment range
        for orig_el in target_elems:
            p_elem.insert(insert_cursor, orig_el)
            insert_cursor += 1

    if cid is not None:
        cre = ET.Element(wtag("commentRangeEnd"))
        cre.set(wtag("id"), cid)
        p_elem.insert(insert_cursor, cre)
        insert_cursor += 1

        ref_run = ET.Element(wtag("r"))
        rpr = ET.SubElement(ref_run, wtag("rPr"))
        rs = ET.SubElement(rpr, wtag("rStyle"))
        rs.set(wtag("val"), "CommentReference")
        cr = ET.SubElement(ref_run, wtag("commentReference"))
        cr.set(wtag("id"), cid)
        p_elem.insert(insert_cursor, ref_run)
        insert_cursor += 1

    return True


def build_comments_xml(existing_xml_bytes, comments_list, author, date_str):
    """Create or append to word/comments.xml."""
    if existing_xml_bytes:
        root = ET.fromstring(existing_xml_bytes)
    else:
        root = ET.Element(wtag("comments"))

    for c in comments_list:
        cm = ET.SubElement(root, wtag("comment"))
        cm.set(wtag("id"), c["id"])
        cm.set(wtag("author"), author)
        cm.set(wtag("date"), date_str)
        cm.set(wtag("initials"), "MA")

        p = ET.SubElement(cm, wtag("p"))
        # Bold header prefix if severity or category is given
        header_parts = [x for x in (c.get("severity"), c.get("category")) if x]
        if header_parts:
            r_bold = ET.SubElement(p, wtag("r"))
            rpr = ET.SubElement(r_bold, wtag("rPr"))
            ET.SubElement(rpr, wtag("b"))
            t_b = ET.SubElement(r_bold, wtag("t"))
            t_b.set(f"{{{XML_NS}}}space", "preserve")
            t_b.text = f"[{' — '.join(header_parts)}] "

        r_txt = ET.SubElement(p, wtag("r"))
        t_txt = ET.SubElement(r_txt, wtag("t"))
        t_txt.set(f"{{{XML_NS}}}space", "preserve")
        t_txt.text = c["text"]

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def ensure_comments_relationships(zin_files):
    """Ensure [Content_Types].xml, word/_rels/document.xml.rels, and word/settings.xml support comments & track changes."""
    # 1. [Content_Types].xml
    ct_bytes = zin_files["[Content_Types].xml"]
    ET.register_namespace("", CT_NS)
    ct_root = ET.fromstring(ct_bytes)
    has_comments_ct = any(
        el.get("PartName") == "/word/comments.xml"
        for el in ct_root.findall(f"{{{CT_NS}}}Override")
    )
    if not has_comments_ct:
        ov = ET.SubElement(ct_root, f"{{{CT_NS}}}Override")
        ov.set("PartName", "/word/comments.xml")
        ov.set("ContentType", "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml")
    zin_files["[Content_Types].xml"] = ET.tostring(ct_root, encoding="utf-8", xml_declaration=True)

    # 2. word/_rels/document.xml.rels
    rels_key = "word/_rels/document.xml.rels"
    if rels_key in zin_files:
        ET.register_namespace("", REL_NS)
        rels_root = ET.fromstring(zin_files[rels_key])
        comments_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
        has_rel = any(
            el.get("Type") == comments_rel_type
            for el in rels_root.findall(f"{{{REL_NS}}}Relationship")
        )
        if not has_rel:
            rel = ET.SubElement(rels_root, f"{{{REL_NS}}}Relationship")
            rel.set("Id", "rIdAuditComments")
            rel.set("Type", comments_rel_type)
            rel.set("Target", "comments.xml")
        zin_files[rels_key] = ET.tostring(rels_root, encoding="utf-8", xml_declaration=True)

    # 3. word/settings.xml -> enable <w:trackRevisions/>
    settings_key = "word/settings.xml"
    if settings_key in zin_files:
        ET.register_namespace("w", W_NS)
        s_root = ET.fromstring(zin_files[settings_key])
        if s_root.find(wtag("trackRevisions")) is None:
            ET.SubElement(s_root, wtag("trackRevisions"))
        zin_files[settings_key] = ET.tostring(s_root, encoding="utf-8", xml_declaration=True)


_VALID_EDIT_KEYS = {
    "para", "para_id", "pid",
    "target", "find", "search", "quote",
    "replacement", "replace", "new", "insert",
    "comment", "note", "issue",
    "severity", "sev", "category", "cat",
    "id", "title", "location",
}


def validate_and_normalize_edits(edits, json_path):
    """Validate EDITS.json schema upfront and normalize field aliases."""
    if not isinstance(edits, list):
        sys.exit(f"ERROR in {json_path}: Root JSON value must be a list of edit objects [...]")
    norm_list = []
    for idx, item in enumerate(edits, 1):
        if not isinstance(item, dict):
            sys.exit(f"ERROR in {json_path} item #{idx}: Expected a JSON object {{...}}, got {type(item).__name__}")
        unknown = set(item.keys()) - _VALID_EDIT_KEYS
        if unknown:
            sys.exit(
                f"ERROR in {json_path} item #{idx}: Unrecognized key(s) {sorted(unknown)}.\n"
                f"  Valid keys: 'para', 'target', 'replacement' (or 'replace'), 'comment', 'severity', 'category'."
            )
        target = item.get("target") or item.get("find") or item.get("search") or item.get("quote")
        if not target or not isinstance(target, str) or not target.strip():
            sys.exit(
                f"ERROR in {json_path} item #{idx}: Missing or empty 'target' string.\n"
                f"  Expected schema: {{\"para\": 34, \"target\": \"exact phrase\", \"replacement\": \"...\", \"comment\": \"...\"}}"
            )
        replacement = None
        for r_key in ("replacement", "replace", "new", "insert"):
            if r_key in item and item[r_key] is not None:
                replacement = item[r_key]
                break
        comment = item.get("comment") or item.get("note") or item.get("issue")
        if replacement is None and not comment:
            sys.exit(
                f"ERROR in {json_path} item #{idx} (target='{target[:40]}'): Must specify at least 'replacement' or 'comment'."
            )
        para_raw = item.get("para") if "para" in item else (item.get("para_id") or item.get("pid"))
        para_int = None
        para_sub = None
        para_tbl = None
        if isinstance(para_raw, int):
            para_int = para_raw
        elif isinstance(para_raw, float):
            if para_raw.is_integer():
                para_int = int(para_raw)
            else:
                parts = str(para_raw).split(".", 1)
                para_int = int(parts[0])
                para_sub = int(parts[1])
        elif isinstance(para_raw, str):
            m_t = re.match(r"^\[?(?:T|TABLE\s*)(\d+)", para_raw.strip(), re.I)
            if m_t:
                para_tbl = int(m_t.group(1))
            else:
                m_p = re.search(r"(\d+)(?:\.(\d+))?", para_raw)
                if m_p:
                    para_int = int(m_p.group(1))
                    if m_p.group(2):
                        para_sub = int(m_p.group(2))
        norm_item = dict(item)
        norm_item["target"] = target
        norm_item["replacement"] = replacement
        norm_item["para"] = para_int
        norm_item["para_sub"] = para_sub
        norm_item["para_tbl"] = para_tbl
        if comment:
            norm_item["comment"] = comment
        norm_list.append(norm_item)
    return norm_list


def _best_fuzzy_hint(target, target_p, top_paras, all_paras):
    """Return a short diagnostic string showing the closest text in the target paragraph and across the document."""
    nt = norm_typo(target).lower()
    p_preview = ""
    if target_p is not None and target_p in top_paras:
        p_txt = get_para_full_text(top_paras[target_p])
        p_preview = f"[P{target_p}] text is: \"{p_txt[:110]}{'...' if len(p_txt) > 110 else ''}\""
    best_score = 0.0
    best_loc = ""
    best_snip = ""
    w_len = max(20, len(nt))
    for loc_label, p_el in all_paras:
        p_txt = get_para_full_text(p_el)
        if not p_txt:
            continue
        np = norm_typo(p_txt).lower()
        sm = difflib.SequenceMatcher(None, nt, np)
        match = sm.find_longest_match(0, len(nt), 0, len(np))
        if match.size > best_score:
            best_score = match.size
            best_loc = loc_label
            s0 = max(0, match.b - 15)
            s1 = min(len(p_txt), match.b + w_len + 15)
            best_snip = p_txt[s0:s1]
    if best_snip and p_preview:
        return f"{p_preview} | closest overall in {best_loc}: \"...{best_snip}...\""
    if best_snip:
        return f"closest match in {best_loc}: \"...{best_snip}...\""
    if p_preview:
        return p_preview
    return "no similar substring found in document"


def main():
    if len(sys.argv) < 4 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)

    in_docx, out_docx, edits_json = sys.argv[1], sys.argv[2], sys.argv[3]
    if os.path.abspath(in_docx) == os.path.abspath(out_docx):
        sys.exit("ERROR: OUTPUT.docx must be a different path from INPUT.docx (never overwrite original).")

    author = "Manuscript Audit"
    if "--author" in sys.argv:
        author = sys.argv[sys.argv.index("--author") + 1]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw_edits = json.load(open(edits_json, encoding="utf-8"))
    edits = validate_and_normalize_edits(raw_edits, edits_json)

    with zipfile.ZipFile(in_docx, "r") as zin:
        zin_files = {name: zin.read(name) for name in zin.namelist()}

    doc_root = ET.fromstring(zin_files["word/document.xml"])
    body = doc_root.find(wtag("body"))

    # Map 1-based top-level paragraph index [P#] -> <w:p> element, plus collect all <w:p> (including inside <w:sdt> and tables)
    top_paras = {}
    tbl_paras = {}
    all_paras = []
    p_idx = 0
    t_idx = 0
    for child in iter_body_blocks(body):
        if child.tag == wtag("p"):
            p_idx += 1
            top_paras[p_idx] = child
            all_paras.append((f"[P{p_idx}]", child))
        elif child.tag == wtag("tbl"):
            t_idx += 1
            tbl_paras[t_idx] = []
            for p_cell in child.findall(f".//{wtag('p')}"):
                tbl_paras[t_idx].append(p_cell)
                all_paras.append((f"[TABLE {t_idx}]", p_cell))

    rev_id_counter = [1001]
    comment_id_counter = [1]
    comments_list = []

    applied_edits = 0
    failed_edits = []

    for idx, edit in enumerate(edits, 1):
        target_p = edit.get("para")
        target_tbl = edit.get("para_tbl")
        matched = False
        if target_p is not None and target_p in top_paras:
            matched = apply_edit_to_paragraph(
                top_paras[target_p], edit, rev_id_counter, comment_id_counter, author, date_str, comments_list
            )
        elif target_tbl is not None and target_tbl in tbl_paras:
            for p_cell in tbl_paras[target_tbl]:
                if apply_edit_to_paragraph(
                    p_cell, edit, rev_id_counter, comment_id_counter, author, date_str, comments_list
                ):
                    matched = True
                    break
        if not matched:
            for loc_label, p_el in all_paras:
                if apply_edit_to_paragraph(
                    p_el, edit, rev_id_counter, comment_id_counter, author, date_str, comments_list
                ):
                    matched = True
                    break
        if matched:
            applied_edits += 1
        else:
            hint = _best_fuzzy_hint(edit["target"], target_p, top_paras, all_paras)
            failed_edits.append((idx, target_p, edit["target"][:70], hint))

    zin_files["word/document.xml"] = ET.tostring(doc_root, encoding="utf-8", xml_declaration=True)
    if comments_list:
        zin_files["word/comments.xml"] = build_comments_xml(
            zin_files.get("word/comments.xml"), comments_list, author, date_str
        )
    ensure_comments_relationships(zin_files)

    os.makedirs(os.path.dirname(os.path.abspath(out_docx)), exist_ok=True)
    with zipfile.ZipFile(out_docx, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in zin_files.items():
            zout.writestr(name, data)

    print(f"Saved annotated Word document: {out_docx}")
    print(f"  Applied {applied_edits}/{len(edits)} items ({len(comments_list)} margin comments, "
          f"{rev_id_counter[0] - 1001} tracked del/ins revisions).")
    if failed_edits:
        print(f"  !! WARNING — {len(failed_edits)} of {len(edits)} target(s) did not match (output file WAS saved with {applied_edits} applied edit(s)):")
        for i, p, t, hint in failed_edits:
            print(f"     Item #{i} (para={p}): '{t}'")
            print(f"       -> Hint: {hint}")
        if "--strict" in sys.argv or applied_edits == 0:
            sys.exit(1)
        sys.exit(2)


if __name__ == "__main__":
    main()
