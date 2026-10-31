#!/usr/bin/env python3
"""Reference & citation integrity audit on extracted manuscript text (or .docx).

Usage:
  ref_audit.py TEXTFILE [--docx ORIGINAL.docx] [--refs-heading "References"] [--style auto|numeric|superscript|author-year]

Supports both plain text and `--indexed` output (`[P12] ...`, `[T1] ...`) from `docx2txt.py`.
Handles all major numbered bibliography and in-text citation formats:
  - Reference list entries: `[1] ...`, `1. ...`, `1) ...`, `(1) ...`, `(1)\\t...`, `1\\t...`
  - In-text citations: `(1)`, `(4–9)`, `[1]`, `[4–9]`, and superscript-flattened citations
    (`hydrogel1`, `compartment.6–8`, `studies24,27–29`). If `--docx FILE.docx` is supplied,
    also inspects `<w:vertAlign w:val="superscript"/>` runs directly from the Word XML.

Reports:
  1. Reference list inventory (count, numbering range, gaps)
  2. Duplicate / near-duplicate reference entries + automatic renumbering impact
  3. Reference formatting & metadata anomalies (missing year, disguised preprints 10.1101/..., split words)
  4. Uncited references & dangling callouts (callout > max ref number)
  5. Sentence-by-sentence callout -> reference title map for topical verification
"""
import sys
import os
import re
import zipfile
import difflib
import unicodedata
import html as _html
import xml.etree.ElementTree as ET
from collections import Counter


def _fold_diacritics(s):
    """Strip combining diacritics and map special Latin letters (ł->l, ø->o, ß->ss) to ASCII."""
    s = s.replace("ł", "l").replace("Ł", "L").replace("ø", "o").replace("Ø", "O").replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

HEADING_CANDIDATES = (
    r'(?:authors?[\'\u2019]s?\s+|supplementary\s+|supporting\s+|si\s+)?references',
    r'(?:authors?[\'\u2019]s?\s+|supplementary\s+|supporting\s+|si\s+)?bibliography',
    r'(?:supplementary\s+|supporting\s+)?literature\s+cited',
    r'(?:supplementary\s+|supporting\s+)?works\s+cited',
    r'(?:supplementary\s+|supporting\s+)?reference\s+list',
    r'notes\s+and\s+references',
    r'references\s+and\s+notes',
)

STOP_HEADING_PAT = re.compile(
    r'^\s*(?:\[(?:P\d+(?:\.\d+)?|T\d+|TABLE\s+\d+|Row\s+\d+)\]\s*)?(?:\d+(?:\.\d+)*\.?\s+)?'
    r'(?:(?:main\s+|supplementary\s+|supplemental\s+|expanded\s+view\s+)?figure\s+legends?|'
    r'figures?|tables?|video\s+legends?|movie\s+legends?|acknowledg(?:e)?ments?|'
    r'supplementary\s+(?:information|materials?|text|tables?|figures?|notes?|methods?)|'
    r'expanded\s+view(?:\s+figure\s+legends?)?|'
    r'supporting\s+information|extended\s+data|author\s+contributions?|'
    r'competing\s+interests?|conflict\s+of\s+interest|disclosure\s+and\s+competing\s+interests?\s+statement|'
    r'data\s+(?:and\s+code\s+)?availability|funding|'
    r'reviewer\s*#?\s*\d+.*|major\s+(?:comments?|points?)|minor\s+(?:comments?|points?)|'
    r'response\s+to\s+reviewers?.*)\s*:?\s*$',
    re.I,
)

UNIT_OR_MATH_STEMS = {
    'mm', 'cm', 'nm', 'um', 'km', 'dm', 'ml', 'ul', 'dl', 'mg', 'ug', 'ng', 'pg', 'kg',
    'mol', 'mmol', 'umol', 'nmol', 'log', 'od', 'ic', 'ec', 'ld', 'ed', 'ph', 'fig',
    'figs', 'tab', 'table', 'eq', 'sec', 'min', 'hr', 'hrs', 'day', 'step', 'ch', 'vol',
    'no', 'id', 'ver', 'rev', 'sp', 'spp', 'st', 'nd', 'rd', 'th', 'he', 'li', 'be',
    'ne', 'na', 'mg', 'al', 'si', 'cl', 'ar', 'ca', 'sc', 'ti', 'cr', 'mn', 'fe', 'co',
    'ni', 'cu', 'zn', 'ga', 'ge', 'se', 'br', 'kr', 'rb', 'sr', 'zr', 'nb', 'mo', 'ru',
    'rh', 'pd', 'ag', 'cd', 'sn', 'sb', 'te', 'xe', 'cs', 'ba', 'la', 'ce', 'pr', 'nd',
}


def strip_ptag(line):
    """Return (ptag_or_empty, clean_line)."""
    m = re.match(r'^\s*(\[(?:P\d+(?:\.\d+)?|T\d+|TABLE\s+\d+|Row\s+\d+)\])\s*(.*)', line)
    if m:
        return m.group(1), m.group(2)
    return '', line


_PARTICLE_PAT = r"(?:(?:van(?:\s+der|\s+den)?|von|de(?:\s+la|\s+los)?|del|della|di|du|la|le|st\.?|mc|mac|o'|abu|al|el)\s+(?!et\s+al\b|and\b))"

_MONTH_NAMES = {
    'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec',
    'january', 'february', 'march', 'april', 'june', 'july', 'august', 'september', 'october', 'november', 'december',
}

_PROSE_START_WORDS = {
    'the', 'we', 'thank', 'thanks', 'based', 'in', 'as', 'for', 'to', 'this', 'these', 'those',
    'however', 'moreover', 'furthermore', 'therefore', 'thus', 'here', 'our', 'while', 'although',
    'since', 'because', 'when', 'where', 'if', 'upon', 'regarding', 'concerning', 'reviewer',
    'response', 'reply', 'comment', 'major', 'minor', 'point', 'line', 'lines', 'please', 'yes', 'no',
}


def _parse_author_year_from_ref(ref_text):
    """Extract (first_author_surname_lower, year_str, base_year_str) from a reference entry."""
    s_raw = re.sub(r'^\s*(?:\[S?\d+\]|\(S?\d+\)|S?\d+[.)\t])\s*', '', ref_text, flags=re.I).strip()
    s = _fold_diacritics(s_raw)
    # Allow optional leading initial for inverted 'N Saitou & M Nei (1987)' entries,
    # particles like van, de, von, le, Abu, and 2-word compound surnames before a comma or initial
    m_auth = re.match(
        rf"^(?:[A-Z]\.?\s+(?=[A-Z][a-z]{{2,}}\s*(?:,|&|\band\b)))?"
        rf"((?i:{_PARTICLE_PAT})?[A-Z][A-Za-z'\u2019\-*]+(?:\s+[A-Z][a-z'\u2019\-]{{2,}}(?=\s+[A-Z]{{1,3}}\b|\s*,))?)",
        s,
    )
    surname = m_auth.group(1).strip().rstrip('*').lower().replace('\u2019', "'") if m_auth else ""
    # Strip URLs, DOIs, and page ranges before extracting year so DOI segments (.2007., .2018.) are never mistaken for years
    s_no_doi = re.sub(r'https?://\S+|doi\s*:?\s*10\.\S+|\b10\.\d{4,9}/\S+', ' ', s, flags=re.I)
    s_no_doi = re.sub(r'\b\d{1,5}\s*[\u2013\u2014-]\s*\d{1,5}\b', ' ', s_no_doi)
    # Prefer year in parentheses (2020a) or after author block
    m_yr = re.search(r'\(\s*((?:19|20)\d{2}[a-z]?)\s*\)', s_no_doi)
    if not m_yr:
        m_yr = re.search(r'\b((?:19|20)\d{2}[a-z]?)\b', s_no_doi)
    yr = m_yr.group(1).lower() if m_yr else ""
    base_yr = yr[:4] if len(yr) >= 4 else yr
    return surname, yr, base_yr


def _extract_all_ref_surnames(ref_text):
    """Return (primary_surname, set_of_all_author_surname_keys, yr, base_yr) for an Author-Year reference."""
    primary, yr, base_yr = _parse_author_year_from_ref(ref_text)
    keys = set()
    if primary:
        keys.add(primary)
        no_part = re.sub(rf"^{_PARTICLE_PAT}", "", primary, flags=re.I)
        if no_part and len(no_part) >= 2:
            keys.add(no_part)
        if ' ' in no_part:
            for part_w in no_part.split():
                if len(part_w) >= 3:
                    keys.add(part_w)
    # Extract co-author surnames before (Year)
    s_folded = _fold_diacritics(re.sub(r'^\s*(?:\[S?\d+\]|\(S?\d+\)|S?\d+[.)\t])\s*', '', ref_text, flags=re.I).strip())
    m_yr_pos = re.search(r'\(\s*(?:19|20)\d{2}[a-z]?\s*\)|\b(?:19|20)\d{2}[a-z]?\b', s_folded)
    auth_block = s_folded[:m_yr_pos.start()] if m_yr_pos else s_folded[:140]
    for m_co in re.finditer(
        rf"(?:^|[,;&]|\band\b)\s*(?:[A-Z]\.?\s+)?((?i:{_PARTICLE_PAT})?[A-Z][a-z'\u2019\-*]{{1,}}(?:\s+[A-Z][a-z'\u2019\-]{{2,}})?)"
        r"(?=\s+[A-Z]{1,3}\b|\s*,\s*[A-Z]\.|\s*(?:[,;&)]|\band\b|$))",
        auth_block
    ):
        co_s = m_co.group(1).strip().rstrip('*').lower().replace('\u2019', "'")
        if co_s not in ('and', 'the', 'for', 'with', 'et', 'al'):
            keys.add(co_s)
            no_p = re.sub(rf"^{_PARTICLE_PAT}", "", co_s, flags=re.I)
            if no_p and len(no_p) >= 2:
                keys.add(no_p)
            if ' ' in no_p:
                for part_w in no_p.split():
                    if len(part_w) >= 3:
                        keys.add(part_w)
    return primary, keys, yr, base_yr


def _looks_like_unnumbered_ref_line(clean_stripped):
    """Return True if a paragraph in an unnumbered Author-Year bibliography looks like a genuine reference entry."""
    s = _fold_diacritics(clean_stripped)
    first_word = re.match(r"^([A-Za-z]+)", s)
    if first_word and first_word.group(1).lower() in _PROSE_START_WORDS:
        # Only allow if followed immediately by ', Initial.' (e.g. 'The, M.')
        if not re.match(r"^[A-Z][a-z]+,\s+[A-Z]\.", s):
            return False
    if re.match(r"^(?:L\.?\s*\d+|Line\s+\d+|Reviewer\b|Comment\b|Major\b|Minor\b|Response\b)", s, re.I):
        return False
    # Genuine Author-Year reference usually has (YYYY) within the first 220 chars or Author, I. pattern
    if re.search(r'\(\s*(?:19|20)\d{2}[a-z]?\s*\)', s[:240]):
        return True
    if re.match(r"^(?:[A-Z]\.?\s+)?[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)?(?:,\s+|\s+)[A-Z]", s) and re.search(r'\b(?:19|20)\d{2}[a-z]?\b', s):
        return True
    return False


def find_refs_block(lines, user_heading=None, return_style=False, return_meta=False):
    """Locate References block(s) and parse entries (numbered, S-prefixed, or Author-Year)."""
    if user_heading:
        pat = re.compile(rf'^\s*(?:\[(?:P\d+(?:\.\d+)?|T\d+)\]\s*)?(?:\d+\.?\s+)?{re.escape(user_heading)}\s*:?\s*$', re.I)
    else:
        joined = '|'.join(HEADING_CANDIDATES)
        pat = re.compile(rf'^\s*(?:\[(?:P\d+(?:\.\d+)?|T\d+)\]\s*)?(?:\d+\.?\s+)?(?:{joined})\s*:?\s*$', re.I)

    heading_indices = []
    for i, l in enumerate(lines):
        if l.startswith('# MANUSCRIPT-AUDIT EXTRACT'):
            continue
        if pat.match(l):
            heading_indices.append(i)
    if not heading_indices:
        sys.exit("Reference heading not found — pass --refs-heading <HEADING>")

    # Support [1], (1), 1., 1), (1)\t, 1\t and Supplementary [S1], S1., S1\t
    entry_pat = re.compile(
        r'^\s*(?:\[(S?\d{1,3})\]|\((S?\d{1,3})\)|(S?\d{1,3})[.)]|(S?\d{1,3})\t)\s*(.+)',
        re.I,
    )
    legend_stop_pat = re.compile(
        r'^(?:Supplementary\s+|Expanded\s+View\s+)?(?:Figure|Fig\.?|Table|Video|Movie)\s+(?:EV|S)?\d+\s*[.:–—-]',
        re.I
    )

    refs = {}
    ref_ptags = {}
    ref_ranges = []
    has_s_prefix = False
    is_author_year = False

    # First try numbered bibliography parsing across the primary reference block
    start = heading_indices[0] + 1
    end = len(lines)
    cur = None
    s_count = 0
    for idx in range(start, len(lines)):
        raw = lines[idx]
        if idx in heading_indices[1:]:
            end = idx
            break
        if STOP_HEADING_PAT.match(raw):
            end = idx
            break
        ptag, clean = strip_ptag(raw)
        clean_stripped = re.sub(r'^(?:\[\[IMAGE:[^\]]+\]\]\s*)+', '', clean).strip()
        if not clean_stripped:
            continue
        if ptag.startswith('[T') and '|' in clean_stripped and cur is not None:
            end = idx
            break
        if legend_stop_pat.match(clean_stripped) and (cur is not None or idx > start + 2):
            end = idx
            break
        m = entry_pat.match(clean_stripped)
        if m:
            raw_id = (m.group(1) or m.group(2) or m.group(3) or m.group(4)).upper()
            if raw_id.startswith('S'):
                s_count += 1
                num = int(raw_id[1:])
            else:
                num = int(raw_id)
            body = m.group(5).strip()
            if cur is None or num == cur + 1 or num not in refs:
                cur = num
                refs[cur] = body
                ref_ptags[cur] = ptag or f"line {idx+1}"
                continue
        if cur is not None:
            refs[cur] += ' ' + clean_stripped

    if len(refs) >= 3:
        ref_ranges.append((heading_indices[0], end))
        if s_count >= len(refs) * 0.5:
            has_s_prefix = True
        # Also record any additional reference section ranges so they are excluded from non_ref_lines
        for h_idx in heading_indices[1:]:
            b_end = len(lines)
            for idx in range(h_idx + 1, len(lines)):
                if STOP_HEADING_PAT.match(lines[idx]) or idx in heading_indices:
                    b_end = idx
                    break
            ref_ranges.append((h_idx, b_end))
    else:
        # Fallback: Unnumbered alphabetical / Author-Year bibliography across all reference heading blocks
        refs = {}
        ref_ptags = {}
        ref_ranges = []
        auto_num = 0
        for b_i, h_idx in enumerate(heading_indices):
            b_start = h_idx + 1
            b_end = len(lines)
            next_h = heading_indices[b_i + 1] if b_i + 1 < len(heading_indices) else len(lines)
            blk_count = 0
            for idx in range(b_start, next_h):
                raw = lines[idx]
                if STOP_HEADING_PAT.match(raw):
                    b_end = idx
                    break
                ptag, clean = strip_ptag(raw)
                clean_stripped = re.sub(r'^(?:\[\[IMAGE:[^\]]+\]\]\s*)+', '', clean).strip()
                if not clean_stripped:
                    continue
                if ptag.startswith('[T') and '|' in clean_stripped and blk_count > 0:
                    b_end = idx
                    break
                if legend_stop_pat.match(clean_stripped):
                    b_end = idx
                    break
                if (
                    blk_count >= 1
                    and len(clean_stripped) < 60
                    and not re.search(r'\b(?:19|20)\d{2}\b|doi|et\s+al', clean_stripped, re.I)
                    and re.match(r'^[A-Z][A-Za-z0-9\s,\-–—/()#.:]{2,55}$', clean_stripped)
                ):
                    b_end = idx
                    break
                if _looks_like_unnumbered_ref_line(clean_stripped):
                    auto_num += 1
                    blk_count += 1
                    refs[auto_num] = clean_stripped
                    ref_ptags[auto_num] = ptag or f"line {idx+1}"
                elif blk_count > 0:
                    # Stop if we hit a long non-reference prose paragraph (e.g. next reviewer section in rebuttal)
                    first_w = re.match(r'^([A-Za-z]+)', clean_stripped)
                    if (first_w and first_w.group(1).lower() in _PROSE_START_WORDS) or len(clean_stripped) > 260:
                        b_end = idx
                        break
                    refs[auto_num] += ' ' + clean_stripped
            ref_ranges.append((h_idx, b_end))
        if refs:
            is_author_year = True
        start = heading_indices[0] + 1
        end = ref_ranges[-1][1] if ref_ranges else len(lines)

    if return_meta:
        return start - 1, end, refs, ref_ptags, is_author_year, ref_ranges, has_s_prefix
    if return_style:
        return start - 1, end, refs, ref_ptags, is_author_year
    return start - 1, end, refs, ref_ptags


def norm_entry(t):
    t = _html.unescape(t).lower()
    t = re.sub(r'doi\s*:?\s*10\.\S+', ' ', t)
    t = re.sub(r'[^a-z0-9 ]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def _parse_num_expr(expr, max_ref, allow_s_prefix=False):
    """Parse a comma/semicolon/dash expression like '1, 4-6, 12' (or 'S1–S6') into a set of integers."""
    out = set()
    for tok in re.split(r'\s*[,;]\s*', expr.strip()):
        if not tok:
            continue
        if allow_s_prefix:
            tok = re.sub(r'^[Ss](?=\d)', '', tok)
        if any(d in tok for d in ('\u2013', '\u2014', '-')):
            parts = re.split(r'\s*[\u2013\u2014-]\s*', tok)
            if allow_s_prefix and len(parts) == 2:
                parts = [re.sub(r'^[Ss](?=\d)', '', p) for p in parts]
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                a, b = int(parts[0]), int(parts[1])
                if 1 <= a <= b <= max(max_ref + 50, 300) and (b - a) < 50:
                    out.update(range(a, b + 1))
        elif tok.isdigit():
            v = int(tok)
            if 1 <= v <= max(max_ref + 50, 300) and not (1900 <= v <= 2099 and max_ref < 500):
                out.add(v)
    return out


def extract_docx_superscript_cites(docx_path, max_ref):
    """Extract superscript citation numbers directly from word/document.xml if .docx is given."""
    cites = set()
    if not docx_path or not os.path.isfile(docx_path):
        return cites
    try:
        with zipfile.ZipFile(docx_path) as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        for p in root.iter(f"{{{W_NS}}}p"):
            sup_buf = []
            prev_text = ""
            for r in p.iter(f"{{{W_NS}}}r"):
                rpr = r.find(f"{{{W_NS}}}rPr")
                va = rpr.find(f"{{{W_NS}}}vertAlign") if rpr is not None else None
                is_sup = va is not None and va.attrib.get(f"{{{W_NS}}}val") == "superscript"
                t_str = "".join(t.text or "" for t in r.iter(f"{{{W_NS}}}t"))
                if is_sup:
                    # Ignore 10^6, cm^2, mm^2, um^2, m^3, etc.
                    if not re.search(r'(?:10|\b(?:mm|cm|nm|um|\u03bcm|\u00b5m|m|s))$', prev_text.strip(), re.I):
                        sup_buf.append(t_str)
                else:
                    if sup_buf:
                        joined = "".join(sup_buf).strip("()[] ")
                        cites.update(_parse_num_expr(joined, max_ref))
                        sup_buf = []
                    prev_text += t_str
            if sup_buf:
                joined = "".join(sup_buf).strip("()[] ")
                cites.update(_parse_num_expr(joined, max_ref))
    except Exception:
        pass
    return cites


def build_recurring_alnum_tokens(body_text):
    """Find word+digit tokens (like geneA1, mutB2, HEK293, Cas9) that recur as fixed names."""
    counts = Counter(re.findall(r'\b[A-Za-z]{2,}\d+\b', body_text))
    return {tok for tok, c in counts.items() if c >= 2}


def extract_numeric_callouts(text, max_ref, allow_superscript=True, recurring_tokens=None, allow_s_prefix=False):
    """Extract numeric callouts from parenthesized, bracketed, or superscript-flattened ranges."""
    cites = set()
    if recurring_tokens is None:
        recurring_tokens = set()
    # Strip [[IMAGE:...]] tokens so image filenames (e.g. image1.png) are never parsed as citations
    text = re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', text)

    # 1. Standard parenthesized (12), (4-9), (11,70) and bracketed [12], [4-9], [S1–S6]
    num_tok = r'S?\d{1,3}' if allow_s_prefix else r'\d{1,3}'
    pats = [
        rf'(?<!\bstep\s)(?<!\beq\.\s)(?<!\bfig\.\s)(?<!\bfigure\s)(?<!\btable\s)\(({num_tok}(?:\s*[,;\u2013\u2014-]\s*{num_tok})*)\)',
        rf'(?<![A-Za-z0-9_\[])\[({num_tok}(?:\s*[,;\u2013\u2014-]\s*{num_tok})*)\](?!\])',
    ]
    for pat in pats:
        for m in re.finditer(pat, text, re.I):
            cites.update(_parse_num_expr(m.group(1), max_ref, allow_s_prefix=allow_s_prefix))

    if not allow_superscript:
        return cites

    # 2. Superscript-flattened citations after punctuation: e.g. "word.1,2", "word,6–8", "word)15", "word”12"
    punct_sup_pat = re.compile(
        r'(?:(?<=[A-Za-z]{3}\.)|(?<=[A-Za-z\)\u201d"\u2019\'][.,;]))'
        r'(\d{1,3}(?:[,;\u2013\u2014-]\d{1,3})*)(?=[.,;:)\s]|$)(?!\.[A-Za-z0-9])'
    )
    for m in punct_sup_pat.finditer(text):
        cites.update(_parse_num_expr(m.group(1), max_ref))

    # 3. Superscript-flattened citations attached directly to end of a word:
    #    3a. Multi-citation range/list attached to a word: e.g. "hydrogels6–8", "studies24,27–29"
    word_range_pat = re.compile(
        r'\b([a-zA-Z]{3,})(\d{1,3}(?:[,;\u2013\u2014-]\d{1,3})+)(?=[.,;:)\s]|$)(?!\.[A-Za-z0-9])'
    )
    for m in word_range_pat.finditer(text):
        stem = m.group(1).lower()
        if stem not in UNIT_OR_MATH_STEMS:
            cites.update(_parse_num_expr(m.group(2), max_ref))

    #    3b. Single citation integer attached to a lowercase word: e.g. "hydrogel1", "microscopy14"
    word_single_pat = re.compile(
        r'\b([a-z]{4,})(\d{1,3})(?=[.,;:)\s]|$)(?!\.[A-Za-z0-9])'
    )
    for m in word_single_pat.finditer(text):
        full_tok = m.group(0)
        stem = m.group(1).lower()
        num = int(m.group(2))
        if stem in UNIT_OR_MATH_STEMS:
            continue
        if full_tok in recurring_tokens:
            continue
        if 1 <= num <= max_ref:
            cites.add(num)

    return cites


def _harvest_corpus_binomials(body_text, refs):
    """Harvest Latin binomial pairs (genus_lower, species_lower) and abbreviated (g_initial, species_lower)
    from the manuscript body and reference list, supplemented by standard taxonomic genus suffixes.
    """
    full_binomials = set()
    abbr_binomials = set()
    combined = (body_text or "") + "\n" + "\n".join(refs.values())
    # Look for standard lowercase species epithets after a Genus that is also abbreviated as 'G. species' in text,
    # or whose genus/species carries a standard Latin taxonomic suffix.
    tax_genus_re = re.compile(
        r'^(?:[A-Z][a-z]+(?:omonas|bacter|coccus|bacillus|myces|vibrio|ella|spira|clostridium|plasma|phaga|monas|trichum|spora|rhizobium|virus|phyta|cystis|zoon|nema)|'
        r'Escherichia|Pseudomonas|Staphylococcus|Streptococcus|Salmonella|Bacillus|Saccharomyces|Candida|Aspergillus|'
        r'Plasmodium|Caenorhabditis|Drosophila|Arabidopsis|Danio|Xenopus|Mus|Rattus|Homo)$'
    )
    tax_species_re = re.compile(
        r'^[a-z]{3,}(?:ensis|iensis|ii|ae|is|us|um|ans|icans|fera|phila|philus|lytica|lyticus|genes|gena|cola|vorans|formis|oides|coli|ogaster)$'
    )
    common_english_stop = {
        'cells', 'strains', 'species', 'mutants', 'proteins', 'genes', 'infection', 'infections', 'growth',
        'media', 'buffer', 'system', 'systems', 'model', 'models', 'analysis', 'assay', 'assays', 'figure',
        'table', 'method', 'methods', 'results', 'discussion', 'authors', 'university', 'press', 'journal',
        'review', 'reviews', 'this', 'that', 'with', 'from', 'were', 'have', 'been', 'each', 'both', 'such',
        'more', 'most', 'only', 'also', 'when', 'where', 'which', 'while', 'after', 'before', 'between',
        'through', 'during', 'without', 'within', 'under', 'over', 'into', 'onto', 'upon', 'about', 'above',
        'below', 'across', 'against', 'among', 'around', 'behind', 'beyond', 'despite', 'except', 'inside',
        'outside', 'toward', 'towards', 'unlike', 'until', 'versus', 'via', 'data', 'status', 'virus', 'focus',
        'basis', 'crisis', 'thesis', 'axis', 'synopsis', 'synthesis', 'hypothesis', 'diagnosis', 'prognosis',
        'medium', 'serum', 'spectrum', 'calcium', 'sodium', 'potassium', 'magnesium', 'minimum', 'maximum',
        'optimum', 'momentum', 'quantum', 'consortium', 'symposium', 'bacterium', 'memorandum', 'curriculum',
        'human', 'humans', 'organs', 'means', 'plans', 'scans', 'spans', 'trans', 'domains', 'chains', 'grains',
        'brains', 'stains', 'trains', 'remains', 'contains', 'maintains', 'obtains', 'explains', 'sustains',
        'formulae', 'larvae', 'pupae', 'algae', 'fungi', 'stimuli', 'nuclei', 'loci', 'foci', 'radii', 'termini',
    }
    # 1. Abbreviated 'G. species' in body text or references
    abbr_candidates = set()
    for m in re.finditer(r'\b([A-Z])\.\s+([a-z]{4,})\b', combined):
        g_init, sp = m.group(1).lower(), m.group(2).lower()
        if sp not in common_english_stop:
            abbr_candidates.add((g_init, sp))
            if tax_species_re.match(sp):
                abbr_binomials.add((g_init, sp))
    # 2. Full 'Genus species' in body text or references where species is lowercase
    for m in re.finditer(r'\b([A-Z][a-z]{3,})\s+([a-z]{4,})\b', combined):
        g, sp = m.group(1), m.group(2)
        if sp.lower() in common_english_stop:
            continue
        if tax_genus_re.match(g) or (g[0].lower(), sp.lower()) in abbr_candidates:
            full_binomials.add((g.lower(), sp.lower()))
            abbr_binomials.add((g[0].lower(), sp.lower()))
    return full_binomials, abbr_binomials, tax_genus_re


def audit_ref_formatting(refs, ref_ptags, body_text=""):
    """Check each reference entry for formatting corruptions, missing year/journal/volume/pages, disguised preprints, or broken words."""
    issues = []
    all_words = set()
    for t in refs.values():
        for w in re.findall(r'\b[a-zA-Z]{6,}\b', t):
            all_words.add(w.lower())

    full_binomials, abbr_binomials, tax_genus_re = _harvest_corpus_binomials(body_text, refs)

    # Check for duplicate DOIs across distinct reference numbers
    doi_to_refs = {}
    for n in sorted(refs):
        all_dois = [d.rstrip('.,;)').lower() for d in re.findall(r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', refs[n])]
        if all_dois:
            for d_clean in set(all_dois):
                doi_to_refs.setdefault(d_clean, []).append(n)
    for d_clean, r_list in doi_to_refs.items():
        if len(r_list) >= 2:
            for n in r_list:
                others = [x for x in r_list if x != n]
                issues.append((
                    n, ref_ptags.get(n, ''),
                    f"CRITICAL DUPLICATE DOI '{d_clean}' shared with Ref(s) {others}",
                    refs[n]
                ))

    for n in sorted(refs):
        t = refs[n]
        loc = ref_ptags.get(n, '')
        if re.search(r' {3,}|\t+', t):
            issues.append((n, loc, "Multi-space / tab whitespace corruption", t))
        if '\u00a0' in t:
            issues.append((n, loc, f"Contains {t.count(chr(0x00a0))} non-breaking space character(s) (U+00A0)", t))
        if re.search(r'&(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);', t):
            issues.append((n, loc, "Unescaped HTML/XML entity (e.g. '&amp;') in reference text", t))

        t_no_doi = re.sub(r'https?://\S+|doi\s*:?\s*10\.\S+|\b10\.\d{4,9}/\S+', '', t, flags=re.I).strip()
        t_no_doi_no_pages = re.sub(r'\b\d{1,5}\s*[\u2013\u2014-]\s*\d{1,5}\b', ' ', t_no_doi)

        if not re.search(r'\b(?:19|20)\d{2}[a-z]?\b', t_no_doi_no_pages):
            issues.append((n, loc, "Missing 4-digit publication year (19xx/20xx) outside DOI/page range", t))

        # Intra-entry duplicate DOI
        entry_dois = [d.rstrip('.,;)').lower() for d in re.findall(r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', t)]
        if len(entry_dois) >= 2:
            issues.append((n, loc, f"Intra-entry duplicate DOI string ({len(entry_dois)} occurrences: {entry_dois})", t))

        # DOI placed in journal title slot (immediately before volume/page/year)
        if re.search(r'\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\s*,\s*\d{1,4}\b|\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\s*\(\s*(?:19|20)\d{2}\s*\)', t):
            issues.append((n, loc, "DOI string placed in journal title slot (missing journal name before volume/year)", t))

        # Duplicated web-scraper YYYY Vol:Issue Vol, Pages pattern (e.g. "2020 202:5 202, 1343–1354")
        m_dup_vol = re.search(r'\b((?:19|20)\d{2})\s+(\d{1,4})\s*:\s*(\d{1,5})\s+\2\s*[,:]\s*\d', t_no_doi)
        if m_dup_vol:
            issues.append((n, loc, f"Duplicated year/volume:issue metadata artifact '{m_dup_vol.group(0)}' (web-scraper citation export)", t))

        # Web-scraper journal disambiguation tag like "Science (1979)." followed by actual year
        m_disamb = re.search(r'\b(Science|Nature|Cell|Lancet)\s*\(\s*(19\d{2})\s*\)\.?\s+(?:19|20)\d{2}\b', t_no_doi)
        if m_disamb:
            issues.append((n, loc, f"Database disambiguation tag '{m_disamb.group(1)} ({m_disamb.group(2)})' left in journal title", t))

        # Journal article formatted with book-chapter "in <Journal>, <Vol>, <Pages>" prefix
        m_in_j = re.search(
            r'\.\s+in\s+((?:Science|Nature(?:\s+[A-Za-z]+)?|Cell(?:\s+[A-Za-z]+)?|PNAS|Proc\.?\s+Natl\.?\s+Acad\.?\s+Sci[^,]*|PLoS\s+[A-Za-z]+|mBio|eLife|EMBO\s+[A-Za-z]+|J\.?\s+Bacteriol[^,]*|Mol\.?\s+Microbiol[^,]*|Nucleic\s+Acids\s+Res[^,]*|Nat\.?\s+[A-Za-z.]+))\s*,\s*\d{1,4}\b',
            t_no_doi
        )
        if m_in_j:
            issues.append((n, loc, f"Journal article formatted with book-chapter 'in {m_in_j.group(1)}' prefix", t))

        # ALL-CAPS article title detection (5+ consecutive ALL-CAPS words of >=4 letters)
        m_caps = re.search(r'(?:\b[A-Z]{4,}\b[\s,:;-]+){4,}\b[A-Z]{4,}\b', t_no_doi)
        if m_caps:
            issues.append((n, loc, f"ALL-CAPS title capitalization '{m_caps.group(0)[:60]}...'", t))

        # Taxonomic binomial title-case error (e.g. "Escherichia Coli", "Drosophila Melanogaster", "E. Coli")
        for m_bin in re.finditer(r'\b([A-Z][a-z]{3,})\s+([A-Z][a-z]{3,})\b', t_no_doi):
            g_w, sp_w = m_bin.group(1), m_bin.group(2)
            # Skip author block before the first period or year
            if m_bin.start() < 35:
                continue
            if (g_w.lower(), sp_w.lower()) in full_binomials or (tax_genus_re.match(g_w) and (g_w[0].lower(), sp_w.lower()) in abbr_binomials):
                issues.append((n, loc, f"Capitalized species epithet in taxonomic binomial '{g_w} {sp_w}' -> should be '{g_w} {sp_w.lower()}'", t))
        for m_abin in re.finditer(r'\b([A-Z])\.\s+([A-Z][a-z]{3,})\b', t_no_doi):
            g_init, sp_w = m_abin.group(1), m_abin.group(2)
            if (g_init.lower(), sp_w.lower()) in abbr_binomials:
                issues.append((n, loc, f"Capitalized species epithet in abbreviated binomial '{g_init}. {sp_w}' -> should be '{g_init}. {sp_w.lower()}'", t))

        # Preprint DOI (10.1101 = bioRxiv/medRxiv except CSHL Press journals; 10.21203 = Research Square)
        is_cshl_journal = bool(re.search(r'10\.1101/(?:cshperspect|gr|gad|lm|sqb|coldspringharb|mcs|pdb)\b', t, re.I))
        if (re.search(r'10\.1101/|10\.21203/', t) and not is_cshl_journal):
            if not re.search(r'\b(?:biorxiv|medrxiv|preprint|research\s*square|arxiv)\b', t, re.I):
                issues.append((n, loc, "Preprint DOI (10.1101/... or 10.21203/...) cited under a non-preprint name without 'bioRxiv/medRxiv/preprint' label", t))
            else:
                issues.append((n, loc, "Preprint citation (verify whether a peer-reviewed published version is now available)", t))
        # Retraction check
        if re.search(
            r'(?:^\s*|\.\s+|\(\s*|\[\s*)(?:retracted(?:\s+article)?|retraction(?:\s+note|\s+notice|\s+of)?|expression\s+of\s+concern)\b'
            r'|10\.\d{4,9}/\S*(?:retract|errat)',
            t, re.I
        ):
            issues.append((n, loc, "Retraction / erratum indicator detected in reference entry or DOI", t))
        # Broken words where a single capital letter is separated by a space from its stem (e.g. "B acteria")
        for m_br in re.finditer(r'\b([A-Z])\s+([a-z]{4,})\b', t):
            joined = (m_br.group(1) + m_br.group(2)).lower()
            if joined in all_words and m_br.group(1) not in ('A',):
                issues.append((n, loc, f"Broken word '{m_br.group(0)}' (matches '{m_br.group(1) + m_br.group(2)}' elsewhere in refs)", t))

        # Truncated page range (e.g. "6-." or "124–," or "6- (2022)" where ending page number is missing)
        m_trunc = re.search(r'(?:[:,;]\s*|\bpp?\.\s*)(\d+\s*[\u2013\u2014-])(?=\s*(?:[.,;)]|\(\s*(?:19|20)\d{2})|\s*$)', t_no_doi)
        if m_trunc:
            issues.append((n, loc, f"Truncated page range '{m_trunc.group(1)}' (missing ending page number)", t))
        # Editorial / draft placeholders left inside reference entry
        m_ed = re.search(r'(\(\s*if\s+this\s+paper[^)]*\)|\b(?:TODO|TBD|FIXME|insert\s+ref|citation\s+needed|manuscript\s+submitted|in\s+preparation)\b)', t, re.I)
        if m_ed:
            issues.append((n, loc, f"Editorial placeholder / unpublished note '{m_ed.group(0)}' inside reference", t))

        # Missing volume or missing page numbers / article ID across citation styles:
        if re.search(r'\b\d{1,4}\s*,\s*\(\s*(?:19|20)\d{2}[a-z]?\s*\)', t_no_doi):
            issues.append((n, loc, "Missing page numbers / article ID between volume and (Year)", t))
        elif re.search(r'\.\s+[A-Z][A-Za-z.\s&]{1,35}\s+\d{1,4}\s*\(\s*(?:19|20)\d{2}[a-z]?\s*\)\s*\.?\s*$', t_no_doi):
            issues.append((n, loc, "Missing page numbers / article ID between volume and (Year) (volume-only entry)", t))
        elif re.search(r'(?<!\d\s)\.\s+[A-Z][A-Za-z.\s]{1,30},\s+\d{1,5}\s*[\u2013\u2014-]\s*\d{1,5}\s*\(\s*(?:19|20)\d{2}[a-z]?\s*\)', t_no_doi):
            issues.append((n, loc, "Missing volume number before page range", t))
        elif re.search(r'\b(?:19|20)\d{2}[a-z]?\s*;\s*\d{1,4}(?:\s*\([^)]+\))?\s*\.?\s*$', t_no_doi):
            issues.append((n, loc, "Missing page numbers / article ID after volume in Vancouver-style reference", t))

    return issues


def extract_author_year_callouts(text):
    """Extract (surname_lower, year_lower, raw_label) tuples and in-text citation syntax anomalies from text."""
    text = _fold_diacritics(re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', text))
    callouts = []
    dup_year_artifacts = []

    # 1. Detect duplicate-year citation artifacts ("Author et al, 2019, 2019"),
    #    truncated 3-digit years ("Author et al, 2017, 201"), and missing comma before year ("Author et al 2019")
    dup_pat = re.compile(
        rf'\b((?i:{_PARTICLE_PAT})?[A-Z][A-Za-z\'\u2019\-]+'
        rf'(?:\s+et\s+al\.?|\s*(?:&|and)\s*(?i:{_PARTICLE_PAT})?[A-Z][A-Za-z\'\u2019\-]+)?'
        r'\s*,\s*((?:19|20)\d{2}[a-z]?)\s*,\s*\2)\b'
    )
    for m in dup_pat.finditer(text):
        dup_year_artifacts.append(m.group(1))

    trunc_yr_pat = re.compile(
        rf'\b((?i:{_PARTICLE_PAT})?[A-Z][A-Za-z\'\u2019\-]+'
        rf'(?:\s+et\s+al\.?|\s*(?:&|and)\s*(?i:{_PARTICLE_PAT})?[A-Z][A-Za-z\'\u2019\-]+)?'
        r'\s*,\s*(?:(?:19|20)\d{2}[a-z]?\s*,\s*)*((?:19|20)\d))\b(?![0-9])'
    )
    for m in trunc_yr_pat.finditer(text):
        dup_year_artifacts.append(f"{m.group(1)} [truncated 3-digit year '{m.group(2)}']")

    # 2. Parenthetical citation blocks: e.g. "(Author et al., 2020; Other & Person, 2018, 2019a; N Saitou & M Nei, 1987)"
    cite_item_pat = re.compile(
        r'(?<!&\s)(?<!&\s\s)(?<!\band\s)'
        r'\b(?:[A-Z]\.?\s+(?=[A-Z][a-z]{2,}\s*(?:&|\band\b)))?'
        rf'((?i:{_PARTICLE_PAT})?[A-Z][A-Za-z\'\u2019\-]+(?:\s+[A-Z][a-z\'\u2019\-]{{2,}}(?=\s+et\s+al\b|\s*(?:,|&|\band\b)))?)'
        rf'(?:\s+et\s+al\.?|\s*(?:&|and)\s*(?:[A-Z]\.?\s+)?(?i:{_PARTICLE_PAT})?[A-Z][A-Za-z\'\u2019\-]+)?'
        r'\s*,?\s*((?:19|20)\d{2}[a-z]?(?:\s*,\s*(?:19|20)\d{2}[a-z]?)*)\b'
    )
    for m_paren in re.finditer(r'\(([^()]*?\b(?:19|20)\d{2}[a-z]?[^()]*)\)', text):
        inner = m_paren.group(1)
        # Check for missing comma between 'et al' and year inside parenthetical citation
        for m_mc in re.finditer(r'\b([A-Z][A-Za-z\'\u2019\-]+\s+et\s+al\.?\s+(?:19|20)\d{2}[a-z]?)\b', inner):
            dup_year_artifacts.append(f"{m_mc.group(1)} [missing comma before year]")
        for m_c in cite_item_pat.finditer(inner):
            surname = m_c.group(1).strip().lower().replace('\u2019', "'")
            if surname in ('fig', 'figure', 'table', 'movie', 'video', 'box', 'eq', 'panel', 'see', 'e.g', 'i.e') or surname in _MONTH_NAMES:
                continue
            years_raw = m_c.group(2)
            for yr in re.findall(r'(?:19|20)\d{2}[a-z]?', years_raw):
                callouts.append((surname, yr.lower(), f"{m_c.group(1)} ({yr})"))

    # 3. Narrative citations: e.g. "Author et al. (2020)" or "Author and Coauthor (2019a, 2019b)"
    narr_pat = re.compile(
        r'(?<!&\s)(?<!\band\s)'
        rf'\b((?i:{_PARTICLE_PAT})?[A-Z][A-Za-z\'\u2019\-]+(?:\s+[A-Z][a-z\'\u2019\-]{{2,}}(?=\s+et\s+al\b|\s+(?:&|and)\s+))?)'
        rf'(?:\s+et\s+al\.?|\s+(?:&|and)\s+(?i:{_PARTICLE_PAT})?[A-Z][A-Za-z\'\u2019\-]+)?'
        r'\s*\(\s*((?:19|20)\d{2}[a-z]?(?:\s*,\s*(?:19|20)\d{2}[a-z]?)*)\s*\)'
    )
    for m_n in narr_pat.finditer(text):
        surname = m_n.group(1).strip().lower().replace('\u2019', "'")
        if surname in ('fig', 'figure', 'table', 'movie', 'video', 'box', 'eq', 'panel', 'see') or surname in _MONTH_NAMES:
            continue
        for yr in re.findall(r'(?:19|20)\d{2}[a-z]?', m_n.group(2)):
            callouts.append((surname, yr.lower(), f"{m_n.group(1)} ({yr})"))

    return callouts, dup_year_artifacts


def extract_docx_zotero_cites(docx_path):
    """Extract (surname_lower, year_str, title) from embedded Zotero/Mendeley CSL-JSON in a .docx archive."""
    import json as _json
    out = []
    if not docx_path or not os.path.isfile(docx_path):
        return out
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        from docx2txt import extract_embedded_csl_json
        with zipfile.ZipFile(docx_path) as zf:
            csl_items, _ = extract_embedded_csl_json(zf)
        for idata in csl_items:
            authors = idata.get("author", [])
            surname = _fold_diacritics((authors[0].get("family", "") if authors else "").strip().lower().replace('\u2019', "'"))
            dp = idata.get("issued", {}).get("date-parts", [[None]])
            yr = str(dp[0][0]) if (dp and dp[0] and dp[0][0]) else ""
            title = idata.get("title", "")
            if surname and yr:
                out.append((surname, yr, title))
        if out:
            return out
    except Exception:
        pass
    try:
        with zipfile.ZipFile(docx_path) as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        for p in root.iter(f"{{{W_NS}}}p"):
            instr_buf = "".join(n.text or "" for n in p.iter(f"{{{W_NS}}}instrText"))
            for m_z in re.finditer(r'CSL_CITATION\s+(\{.*?\})\s*(?:$|ADDIN)', instr_buf, re.S):
                try:
                    data = _json.loads(m_z.group(1))
                    for item in data.get("citationItems", []):
                        idata = item.get("itemData", {})
                        authors = idata.get("author", [])
                        surname = _fold_diacritics((authors[0].get("family", "") if authors else "").strip().lower().replace('\u2019', "'"))
                        dp = idata.get("issued", {}).get("date-parts", [[None]])
                        yr = str(dp[0][0]) if (dp and dp[0] and dp[0][0]) else ""
                        title = idata.get("title", "")
                        if surname and yr:
                            out.append((surname, yr, title))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        sys.exit(__doc__)

    path = sys.argv[1]
    hp = None
    if '--refs-heading' in sys.argv:
        hp = sys.argv[sys.argv.index('--refs-heading') + 1]
    docx_path = None
    if '--docx' in sys.argv:
        docx_path = sys.argv[sys.argv.index('--docx') + 1]
    style_arg = 'auto'
    if '--style' in sys.argv:
        style_arg = sys.argv[sys.argv.index('--style') + 1].lower()

    lines = open(path, encoding='utf-8').read().splitlines()
    if not docx_path and lines and lines[0].startswith('# MANUSCRIPT-AUDIT EXTRACT'):
        m_dp = re.search(r'\bpath=([^|\n]+)', lines[0])
        if m_dp and os.path.isfile(m_dp.group(1).strip()):
            docx_path = m_dp.group(1).strip()

    ref_start, ref_end, refs, ref_ptags, is_author_year, ref_ranges, has_s_prefix = find_refs_block(
        lines, hp, return_meta=True
    )
    if style_arg == 'author-year':
        is_author_year = True

    if not refs:
        print("No references parsed. Check --refs-heading or citation style.")
        return

    max_ref = max(refs)
    ref_line_set = set()
    for r_s, r_e in ref_ranges:
        ref_line_set.update(range(r_s, r_e))
    non_ref_lines = [
        l for idx_l, l in enumerate(lines)
        if idx_l not in ref_line_set and not l.startswith('# MANUSCRIPT-AUDIT EXTRACT')
    ]
    body_text = '\n'.join(non_ref_lines)
    recurring_tokens = build_recurring_alnum_tokens(body_text)

    ref_cite_sites = {}
    dangling_ay = []
    dup_year_findings = []
    prose_placeholder_findings = []
    zotero_items = extract_docx_zotero_cites(docx_path) if docx_path else []

    # Scan body lines for unresolved prose citation placeholders ("in reference", "(#)", "[?]", "(citation needed)")
    placeholder_pat = re.compile(
        r'(\bin\s+references?\b(?!\s*(?:S?\d|\[|\(|to\b|with\b|list|section|table|frame|genome|strain|set|data|panel|model))|'
        r'\(\s*#\s*\)|\[\s*\?\s*\]|\(\s*citation\s+needed\s*\)|\[\s*citation\s+needed\s*\])',
        re.I
    )
    for idx_ln, line in enumerate(non_ref_lines, 1):
        ptag, clean = strip_ptag(line)
        for m_ph in placeholder_pat.finditer(clean):
            prose_placeholder_findings.append((ptag or f"line {idx_ln}", m_ph.group(1), clean[:120]))

    if is_author_year:
        # Build lookup index from (surname_lower, year_str) and (surname_lower, base_year) -> list of ref_nums
        ay_exact = {}
        ay_base = {}
        ref_ay_labels = {}
        for rnum, rtxt in refs.items():
            sname, all_snames, yr, base_yr = _extract_all_ref_surnames(rtxt)
            ref_ay_labels[rnum] = f"{sname.title()} ({yr})" if sname else f"Ref #{rnum}"
            if all_snames and yr:
                for sk in all_snames:
                    if rnum not in ay_exact.setdefault((sk, yr), []):
                        ay_exact[(sk, yr)].append(rnum)
                    if rnum not in ay_base.setdefault((sk, base_yr), []):
                        ay_base[(sk, base_yr)].append(rnum)

        cites = set()
        for idx_ln, line in enumerate(non_ref_lines, 1):
            ptag, clean = strip_ptag(line)
            if not clean.strip():
                continue
            loc_label = ptag or f"line {idx_ln}"
            l_calls, l_dups = extract_author_year_callouts(clean)
            for d_art in l_dups:
                dup_year_findings.append((loc_label, d_art))
            for sname, yr, raw_lbl in l_calls:
                matched = ay_exact.get((sname, yr)) or ay_base.get((sname, yr[:4]), [])
                if not matched:
                    # Try prefix/hyphen match on surname (e.g. compound surname)
                    for (ks, kyr), rlist in ay_base.items():
                        if kyr == yr[:4] and (ks.startswith(sname) or sname.startswith(ks)):
                            matched = rlist
                            break
                if matched:
                    for c in matched:
                        cites.add(c)
                        sites = ref_cite_sites.setdefault(c, [])
                        if loc_label not in sites:
                            sites.append(loc_label)
                else:
                    dangling_ay.append((loc_label, raw_lbl))

        for sname, yr, _ in zotero_items:
            for c in (ay_exact.get((sname, yr)) or ay_base.get((sname, yr[:4]), [])):
                cites.add(c)
        paren_matches, brack_matches, use_sup = [], [], False
    else:
        # First check parenthesized/bracketed callouts; if they cover < 50% of refs, also enable superscript-flattened detection
        num_tok = r'S?\d{1,3}' if has_s_prefix else r'\d{1,3}'
        paren_only_pat = re.compile(rf'(?<!\bstep\s)(?<!\beq\.\s)(?<!\bfig\.\s)(?<!\bfigure\s)(?<!\btable\s)\(({num_tok}(?:\s*[,;\u2013\u2014-]\s*{num_tok})*)\)', re.I)
        brack_only_pat = re.compile(rf'(?<![A-Za-z0-9_\[])\[({num_tok}(?:\s*[,;\u2013\u2014-]\s*{num_tok})*)\](?!\])', re.I)
        paren_matches = [m.group(0) for m in paren_only_pat.finditer(re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', body_text)) if _parse_num_expr(m.group(1), max_ref, allow_s_prefix=has_s_prefix)]
        brack_matches = [m.group(0) for m in brack_only_pat.finditer(re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', body_text)) if _parse_num_expr(m.group(1), max_ref, allow_s_prefix=has_s_prefix)]

        paren_cites = extract_numeric_callouts(body_text, max_ref, allow_superscript=False, allow_s_prefix=has_s_prefix)
        use_sup = (len(paren_cites) < len(refs) * 0.5 and not has_s_prefix) or style_arg == 'superscript'
        cites = extract_numeric_callouts(body_text, max_ref, allow_superscript=use_sup, recurring_tokens=recurring_tokens, allow_s_prefix=has_s_prefix)
        if docx_path and not has_s_prefix:
            cites.update(extract_docx_superscript_cites(docx_path, max_ref))

        # Map each reference number to the exact paragraphs [P#] where it is cited
        for idx_ln, line in enumerate(non_ref_lines, 1):
            ptag, clean = strip_ptag(line)
            if not clean.strip():
                continue
            lcites = extract_numeric_callouts(clean, max_ref, allow_superscript=use_sup, recurring_tokens=recurring_tokens, allow_s_prefix=has_s_prefix)
            loc_label = ptag or f"line {idx_ln}"
            for c in lcites:
                sites = ref_cite_sites.setdefault(c, [])
                if loc_label not in sites:
                    sites.append(loc_label)

    def _fmt_rnum(n):
        return f"S{n}" if has_s_prefix else str(n)

    print(f"== 1. REFERENCE INVENTORY ==")
    mode_str = "Author-Year (unnumbered bibliography)" if is_author_year else ("Supplementary numbered bibliography (S-prefixed)" if has_s_prefix else "Numbered bibliography")
    print(f"Parsed {len(refs)} reference entries [{mode_str}] (range: {_fmt_rnum(min(refs))}..{_fmt_rnum(max_ref)}, lines {ref_start+1}..{ref_end})")
    if zotero_items:
        print(f"  Embedded CSL-JSON citation items detected in DOCX archive: {len(zotero_items)}")
    missing_nums = sorted(set(range(min(refs), max_ref + 1)) - set(refs))
    if missing_nums and not is_author_year:
        print(f"  !! Gaps in reference numbering: {[_fmt_rnum(x) for x in missing_nums]}")
    elif not is_author_year:
        print("  Numbering sequence: contiguous (no gaps)")

    print(f"\n== 2. DUPLICATE / NEAR-DUPLICATE REFERENCES ==")
    keys = {n: norm_entry(t) for n, t in refs.items()}
    dois = {}
    for n, t in refs.items():
        m_d = re.search(r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', t)
        if m_d:
            dois[n] = m_d.group(1).rstrip('.,;)').lower()
    ns = sorted(keys)
    dups = []
    for i in range(len(ns)):
        for j in range(i + 1, len(ns)):
            a, b = ns[i], ns[j]
            ka, kb = keys[a], keys[b]
            r = difflib.SequenceMatcher(None, ka, kb).ratio()
            head = len(ka) >= 45 and ka[:45] == kb[:45]
            same_doi = (a in dois and b in dois and dois[a] == dois[b])
            if r > 0.82 or head or same_doi:
                dups.append((a, b, r, head))
                sites_a = ", ".join(ref_cite_sites.get(a, ["uncited"]))
                sites_b = ", ".join(ref_cite_sites.get(b, ["uncited"]))
                is_exact = (r >= 0.97 or ka == kb or same_doi)
                lbl = "CRITICAL EXACT DUPLICATE REFERENCE" if is_exact else "DUPLICATE CANDIDATE"
                doi_note = f", identical DOI {dois[a]}" if same_doi else ""
                print(f"  !! {lbl}: Ref {_fmt_rnum(a)} ({ref_ptags[a]}) ~= Ref {_fmt_rnum(b)} ({ref_ptags[b]}) "
                      f"[similarity={r:.2f}{', identical prefix' if head else ''}{doi_note}]")
                print(f"     [{_fmt_rnum(a)}] {refs[a][:120]}")
                print(f"     [{_fmt_rnum(b)}] {refs[b][:120]}")
                print(f"     -> In-text citation sites: Ref [{_fmt_rnum(a)}] cited at {sites_a}; Ref [{_fmt_rnum(b)}] cited at {sites_b}")
                print(f"     -> Action required if [{_fmt_rnum(b)}] is removed: re-point callout [{_fmt_rnum(b)}] -> [{_fmt_rnum(a)}] at {sites_b}, "
                      f"and shift Refs {_fmt_rnum(b+1)}..{_fmt_rnum(max_ref)} down by -1")
    if not dups:
        print("  None detected.")

    print(f"\n== 3. REFERENCE FORMATTING & METADATA ANOMALIES ==")
    fmt_issues = audit_ref_formatting(refs, ref_ptags, body_text=body_text)
    # Also check software/tool version strings between body text and references (e.g. ToolX v1.8.7 in text vs v1.8.5 in ref)
    ver_pat = re.compile(r'\b([A-Z][A-Za-z0-9_-]{2,})\s+\(?(?:v\.?|version\s+)(\d+\.\d+(?:\.\d+)?)\)?', re.I)
    body_vers = {}
    for line in non_ref_lines:
        ptag, clean = strip_ptag(line)
        for m_v in ver_pat.finditer(clean):
            body_vers.setdefault(m_v.group(1).lower(), []).append((m_v.group(1), m_v.group(2), ptag or "body"))
    for n, t in sorted(refs.items()):
        for m_v in ver_pat.finditer(t):
            k = m_v.group(1).lower()
            r_ver = m_v.group(2)
            if k in body_vers:
                for b_name, b_ver, b_loc in body_vers[k]:
                    if b_ver != r_ver:
                        fmt_issues.append((
                            n, ref_ptags.get(n, ''),
                            f"Software version mismatch: Ref [{_fmt_rnum(n)}] has '{m_v.group(1)} v{r_ver}', whereas {b_loc} has '{b_name} v{b_ver}'",
                            t
                        ))
    if fmt_issues:
        for n, loc, reason, txt in fmt_issues:
            print(f"  !! Ref {_fmt_rnum(n)} ({loc}): {reason}")
            print(f"     {txt[:130]}")
    else:
        print("  None detected.")

    print(f"\n== 4. IN-TEXT CALLOUT COVERAGE & CITATION STYLE (MAIN TEXT + TABLES + LEGENDS) ==")
    if prose_placeholder_findings:
        for loc_lbl, ph_tok, ctx in prose_placeholder_findings:
            print(f"  !! UNRESOLVED PROSE CITATION PLACEHOLDER in {loc_lbl}: '{ph_tok}' in \"{ctx}\"")
    if is_author_year:
        print("  In-text citation style distribution: Author-Year parenthetical/narrative citations")
        if dup_year_findings:
            for loc_lbl, d_art in dup_year_findings:
                print(
                    f"  !! IN-TEXT CITATION SYNTAX / DUPLICATE-YEAR ARTIFACT in {loc_lbl}: '{d_art}'"
                )
        uncited = sorted(set(refs) - cites)
        print(f"Distinct bibliography entries cited in document: {len(cites)} / {len(refs)}")
        if uncited:
            uncited_desc = [f"#{u} {ref_ay_labels.get(u, '')} ({ref_ptags.get(u, '')})" for u in uncited]
            print(f"Uncited references ({len(uncited)}): {', '.join(uncited_desc)}")
        else:
            print("Uncited references: none")
        if dangling_ay:
            print(f"Dangling Author-Year callouts ({len(dangling_ay)}):")
            for loc_lbl, raw_lbl in dangling_ay:
                print(f"  !! {loc_lbl}: '{raw_lbl}' not found in References section")
        else:
            print("Dangling Author-Year callouts: none")
    else:
        style_parts = []
        if paren_matches:
            style_parts.append(f"parenthetical '(n)': {len(paren_matches)} occurrence(s)")
        if brack_matches:
            style_parts.append(f"bracketed '[n]': {len(brack_matches)} occurrence(s)")
        if use_sup:
            style_parts.append("superscript / ACS-style numeric citations")
        print(f"  In-text citation style distribution: {', '.join(style_parts) if style_parts else 'none detected'}")
        if paren_matches and brack_matches:
            print(f"  !! MIXED IN-TEXT CITATION STYLES: Both parenthetical '(n)' ({len(paren_matches)}x) and bracketed '[n]' ({len(brack_matches)}x) callouts appear in text.")
        elif paren_matches and not brack_matches and not use_sup:
            print("  Note: Manuscript uses parenthetical '(n)' in-text citations; verify whether the target journal requires bracketed '[n]', superscript, or author-year format.")

        # Displaced / orphan / unspaced bracketed citation placement checks
        if brack_matches:
            num_tok = r'S?\d{1,3}' if has_s_prefix else r'\d{1,3}'
            cite_blk_pat = rf'\[(?:{num_tok})(?:\s*[,;\u2013\u2014-]\s*{num_tok})*\]'
            spaced_count = len(re.findall(rf'\s{cite_blk_pat}', body_text))
            unspaced_count = len(re.findall(rf'(?<=[a-zA-Z0-9]){cite_blk_pat}', body_text))
            prefers_spaced = (spaced_count > unspaced_count * 2 and spaced_count >= 5)
            for idx_ln, line in enumerate(non_ref_lines, 1):
                ptag, clean = strip_ptag(line)
                if not clean.strip() or ptag.startswith('[T'):
                    continue
                loc_lbl = ptag or f"line {idx_ln}"
                clean_no_img = re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', clean)
                # 1. Sentence-leading citation attached to capitalized word: e.g. ". [13]Previously,"
                for m_lead in re.finditer(rf'(?:^|(?<=[.?!]\s))({cite_blk_pat})\s*([A-Z][a-z]+)', clean_no_img):
                    c_nums = _parse_num_expr(m_lead.group(1).strip('[]'), max_ref, allow_s_prefix=has_s_prefix)
                    if c_nums:
                        print(f"  !! DISPLACED SENTENCE-LEADING CITATION in {loc_lbl}: '{m_lead.group(0)}' (citation {m_lead.group(1)} appears at the start of a new sentence before '{m_lead.group(2)}' instead of before the preceding period)")
                # 2. Bare post-period citation block at end of sentence/paragraph or followed by another period: e.g. "synthetase.[10,12]" or ".[104]."
                for m_post in re.finditer(rf'([A-Za-z0-9\)]+\.)\s*({cite_blk_pat})(\s*\.)?(?=\s+[A-Z]|\s*$)', clean_no_img):
                    c_nums = _parse_num_expr(m_post.group(2).strip('[]'), max_ref, allow_s_prefix=has_s_prefix)
                    if c_nums:
                        dbl_dot = " with double period" if m_post.group(3) else " placed after sentence period"
                        print(f"  !! POST-PERIOD / ORPHAN CITATION PLACEMENT in {loc_lbl}: '{m_post.group(0).strip()}' (citation {m_post.group(2)}{dbl_dot})")
                # 3. Missing space before bracketed citation when document uses spaced [N]
                if prefers_spaced:
                    for m_unsp in re.finditer(rf'\b([A-Za-z]{{3,}})({cite_blk_pat})', clean_no_img):
                        c_nums = _parse_num_expr(m_unsp.group(2).strip('[]'), max_ref, allow_s_prefix=has_s_prefix)
                        if c_nums:
                            print(f"  !! MISSING SPACE BEFORE BRACKETED CITATION in {loc_lbl}: '{m_unsp.group(0)}' -> should be '{m_unsp.group(1)} {m_unsp.group(2)}'")

        uncited = [_fmt_rnum(x) for x in sorted(set(refs) - cites)]
        dangling = [_fmt_rnum(x) for x in sorted(cites - set(refs))]
        print(f"Distinct reference numbers cited in document: {len(cites)} / {len(refs)}")
        print(f"Uncited references: {uncited if uncited else 'none'}")
        print(f"Dangling callouts (number > {_fmt_rnum(max_ref)} or missing from list): {dangling if dangling else 'none'}")

    print(f"\n== 5. SENTENCE-BY-SENTENCE CALLOUT -> REFERENCE MAP (VERIFY TOPICAL ACCURACY) ==")
    for line in non_ref_lines:
        ptag, clean = strip_ptag(line)
        if not clean.strip():
            continue
        sents = re.split(r'(?<=[.?!])\s+(?=[A-Z0-9(\["\'])', clean)
        for sent in sents:
            if is_author_year:
                s_calls, _ = extract_author_year_callouts(sent)
                if s_calls:
                    loc_str = f"{ptag} " if ptag else ""
                    print(f"\n  {loc_str}{sent.strip()}")
                    for sname, yr, raw_lbl in s_calls:
                        matched = ay_exact.get((sname, yr)) or ay_base.get((sname, yr[:4]), [])
                        if matched:
                            for c in matched:
                                print(f"    -> [{c}] ({ref_ptags.get(c, '')}) {refs[c][:115]}")
                        else:
                            print(f"    -> [{raw_lbl}] !! DANGLING (not in reference list)")
            else:
                scites = sorted(extract_numeric_callouts(sent, max_ref, allow_superscript=use_sup, recurring_tokens=recurring_tokens, allow_s_prefix=has_s_prefix))
                if scites:
                    loc_str = f"{ptag} " if ptag else ""
                    print(f"\n  {loc_str}{sent.strip()}")
                    for c in scites:
                        if c in refs:
                            print(f"    -> [{_fmt_rnum(c)}] {refs[c][:115]}")
                        else:
                            print(f"    -> [{_fmt_rnum(c)}] !! DANGLING (not in reference list)")


if __name__ == '__main__':
    main()
