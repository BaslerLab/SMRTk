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
    r'references',
    r'bibliography',
    r'literature\s+cited',
    r'works\s+cited',
    r'reference\s+list',
    r'notes\s+and\s+references',
)

STOP_HEADING_PAT = re.compile(
    r'^\s*(?:\[(?:P\d+(?:\.\d+)?|T\d+|TABLE\s+\d+|Row\s+\d+)\]\s*)?(?:\d+(?:\.\d+)*\.?\s+)?'
    r'(?:(?:main\s+|supplementary\s+|supplemental\s+|expanded\s+view\s+)?figure\s+legends?|'
    r'figures?|tables?|video\s+legends?|movie\s+legends?|acknowledg(?:e)?ments?|'
    r'supplementary\s+(?:information|materials?|text|tables?|figures?)|'
    r'expanded\s+view(?:\s+figure\s+legends?)?|'
    r'supporting\s+information|extended\s+data|author\s+contributions?|'
    r'competing\s+interests?|conflict\s+of\s+interest|disclosure\s+and\s+competing\s+interests?\s+statement|'
    r'data\s+availability|funding)\s*:?\s*$',
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


def _parse_author_year_from_ref(ref_text):
    """Extract (first_author_surname_lower, year_str, base_year_str) from a reference entry."""
    s_raw = re.sub(r'^\s*(?:\[\d+\]|\(\d+\)|\d+[.)\t])\s*', '', ref_text).strip()
    s = _fold_diacritics(s_raw)
    # First author surname: handle particles like van, de, von, del, la, le, St., O'
    m_auth = re.match(
        r"^((?:(?:van|von|de|del|della|di|du|la|le|st\.?|mc|mac|o')\s+)?[A-Z][A-Za-z'\u2019\-]+)",
        s, re.I
    )
    surname = m_auth.group(1).strip().lower().replace('\u2019', "'") if m_auth else ""
    # Prefer year in parentheses (2020a) or after author block
    m_yr = re.search(r'\(\s*((?:19|20)\d{2}[a-z]?)\s*\)', s)
    if not m_yr:
        m_yr = re.search(r'\b((?:19|20)\d{2}[a-z]?)\b', s)
    yr = m_yr.group(1).lower() if m_yr else ""
    base_yr = yr[:4] if len(yr) >= 4 else yr
    return surname, yr, base_yr


def _extract_all_ref_surnames(ref_text):
    """Return (primary_surname, set_of_all_author_surname_keys, yr, base_yr) for an Author-Year reference."""
    primary, yr, base_yr = _parse_author_year_from_ref(ref_text)
    keys = set()
    if primary:
        keys.add(primary)
        # Also index base surname without particle (e.g. 'le goff' -> 'goff')
        no_part = re.sub(r"^(?:van|von|de|del|della|di|du|la|le|st\.?)\s+", "", primary)
        if no_part and len(no_part) >= 3:
            keys.add(no_part)
    # Extract co-author surnames before (Year)
    s_folded = _fold_diacritics(re.sub(r'^\s*(?:\[\d+\]|\(\d+\)|\d+[.)\t])\s*', '', ref_text).strip())
    m_yr_pos = re.search(r'\(\s*(?:19|20)\d{2}[a-z]?\s*\)|\b(?:19|20)\d{2}[a-z]?\b', s_folded)
    auth_block = s_folded[:m_yr_pos.start()] if m_yr_pos else s_folded[:140]
    for m_co in re.finditer(
        r"(?:^|[,;&]|\band\b)\s*((?:(?:van|von|de|del|della|di|du|la|le|st\.?|mc|mac|o')\s+)?[A-Z][a-z'\u2019\-]{2,})"
        r"(?=\s+[A-Z]{1,3}\b|\s*,\s*[A-Z]\.|\s*(?:[,;&)]|\band\b|$))",
        auth_block
    ):
        co_s = m_co.group(1).strip().lower().replace('\u2019', "'")
        if co_s not in ('and', 'the', 'for', 'with'):
            keys.add(co_s)
            no_p = re.sub(r"^(?:van|von|de|del|della|di|du|la|le|st\.?)\s+", "", co_s)
            if no_p and len(no_p) >= 3:
                keys.add(no_p)
    return primary, keys, yr, base_yr


def find_refs_block(lines, user_heading=None, return_style=False):
    """Locate start and end line indices of the References block and parse entries (numbered or Author-Year)."""
    if user_heading:
        pat = re.compile(rf'^\s*(?:\[(?:P\d+(?:\.\d+)?|T\d+)\]\s*)?(?:\d+\.?\s+)?{re.escape(user_heading)}\s*:?\s*$', re.I)
    else:
        joined = '|'.join(HEADING_CANDIDATES)
        pat = re.compile(rf'^\s*(?:\[(?:P\d+(?:\.\d+)?|T\d+)\]\s*)?(?:\d+\.?\s+)?(?:{joined})\s*:?\s*$', re.I)

    start = None
    for i, l in enumerate(lines):
        if l.startswith('# MANUSCRIPT-AUDIT EXTRACT'):
            continue
        if pat.match(l):
            start = i + 1
            break
    if start is None:
        sys.exit("Reference heading not found — pass --refs-heading <HEADING>")

    refs = {}
    ref_ptags = {}
    cur = None
    end = len(lines)

    # Support [1], (1), 1., 1), (1)\t, 1\t
    entry_pat = re.compile(
        r'^\s*(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3})[.)]|(\d{1,3})\t)\s*(.+)'
    )
    legend_stop_pat = re.compile(
        r'^(?:Supplementary\s+|Expanded\s+View\s+)?(?:Figure|Fig\.?|Table|Video|Movie)\s+(?:EV|S)?\d+\s*[.:–—-]',
        re.I
    )

    for idx in range(start, len(lines)):
        raw = lines[idx]
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
            num = int(m.group(1) or m.group(2) or m.group(3) or m.group(4))
            body = m.group(5).strip()
            if cur is None or num == cur + 1 or num not in refs:
                cur = num
                refs[cur] = body
                ref_ptags[cur] = ptag or f"line {idx+1}"
                continue
        if cur is not None:
            refs[cur] += ' ' + clean_stripped

    is_author_year = False
    if len(refs) < 3:
        # Fallback: Unnumbered alphabetical / Author-Year bibliography (one reference per paragraph)
        refs = {}
        ref_ptags = {}
        auto_num = 0
        end = len(lines)
        for idx in range(start, len(lines)):
            raw = lines[idx]
            if STOP_HEADING_PAT.match(raw):
                end = idx
                break
            ptag, clean = strip_ptag(raw)
            clean_stripped = re.sub(r'^(?:\[\[IMAGE:[^\]]+\]\]\s*)+', '', clean).strip()
            if not clean_stripped:
                continue
            if ptag.startswith('[T') and '|' in clean_stripped and auto_num > 0:
                end = idx
                break
            if legend_stop_pat.match(clean_stripped):
                end = idx
                break
            # Heuristic: stop if we hit a short section heading after >= 3 refs
            if (
                auto_num >= 3
                and len(clean_stripped) < 60
                and not re.search(r'\b(?:19|20)\d{2}\b|doi|et\s+al', clean_stripped, re.I)
                and re.match(r'^[A-Z][A-Za-z0-9\s,\-–—/()]{2,55}$', clean_stripped)
            ):
                end = idx
                break
            if re.search(r'\b(?:19|20)\d{2}[a-z]?\b', clean_stripped) or re.match(r'^[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ\'\-]+,\s+[A-Z]', clean_stripped):
                auto_num += 1
                refs[auto_num] = clean_stripped
                ref_ptags[auto_num] = ptag or f"line {idx+1}"
            elif auto_num > 0:
                refs[auto_num] += ' ' + clean_stripped
        if refs:
            is_author_year = True

    if return_style:
        return start - 1, end, refs, ref_ptags, is_author_year
    return start - 1, end, refs, ref_ptags


def norm_entry(t):
    t = _html.unescape(t).lower()
    t = re.sub(r'doi\s*:?\s*10\.\S+', ' ', t)
    t = re.sub(r'[^a-z0-9 ]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def _parse_num_expr(expr, max_ref):
    """Parse a comma/semicolon/dash expression like '1, 4-6, 12' into a set of integers."""
    out = set()
    for tok in re.split(r'\s*[,;]\s*', expr.strip()):
        if not tok:
            continue
        if any(d in tok for d in ('\u2013', '\u2014', '-')):
            parts = re.split(r'\s*[\u2013\u2014-]\s*', tok)
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


def extract_numeric_callouts(text, max_ref, allow_superscript=True, recurring_tokens=None):
    """Extract numeric callouts from parenthesized, bracketed, or superscript-flattened ranges."""
    cites = set()
    if recurring_tokens is None:
        recurring_tokens = set()
    # Strip [[IMAGE:...]] tokens so image filenames (e.g. image1.png) are never parsed as citations
    text = re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', text)

    # 1. Standard parenthesized (12), (4-9), (11,70) and bracketed [12], [4-9]
    pats = [
        r'(?<!\bstep\s)(?<!\beq\.\s)(?<!\bfig\.\s)(?<!\bfigure\s)(?<!\btable\s)\((\d{1,3}(?:\s*[,;\u2013\u2014-]\s*\d{1,3})*)\)',
        r'(?<![A-Za-z0-9_\[])\[(\d{1,3}(?:\s*[,;\u2013\u2014-]\s*\d{1,3})*)\](?!\])',
    ]
    for pat in pats:
        for m in re.finditer(pat, text, re.I):
            cites.update(_parse_num_expr(m.group(1), max_ref))

    if not allow_superscript:
        return cites

    # 2. Superscript-flattened citations after punctuation: e.g. "word.1,2", "word,6–8", "word)15", "word”12"
    # Require >=3 letters before '.' so decimal numbers like 3.5 or 0.05 are never matched.
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


def audit_ref_formatting(refs, ref_ptags):
    """Check each reference entry for formatting corruptions, missing year, disguised preprints, or broken words."""
    issues = []
    all_words = set()
    for t in refs.values():
        for w in re.findall(r'\b[a-zA-Z]{6,}\b', t):
            all_words.add(w.lower())

    for n in sorted(refs):
        t = refs[n]
        loc = ref_ptags.get(n, '')
        if re.search(r' {3,}|\t+', t):
            issues.append((n, loc, "Multi-space / tab whitespace corruption", t))
        if not re.search(r'\b(?:19|20)\d{2}\b', t):
            issues.append((n, loc, "Missing 4-digit publication year (19xx/20xx)", t))
        # Preprint DOI (10.1101 = bioRxiv/medRxiv except CSHL Press journals; 10.21203 = Research Square)
        is_cshl_journal = bool(re.search(r'10\.1101/(?:cshperspect|gr|gad|lm|sqb|coldspringharb|mcs|pdb)\b', t, re.I))
        if (re.search(r'10\.1101/|10\.21203/', t) and not is_cshl_journal):
            if not re.search(r'\b(?:biorxiv|medrxiv|preprint|research\s*square|arxiv)\b', t, re.I):
                issues.append((n, loc, "Preprint DOI (10.1101/... or 10.21203/...) cited under a non-preprint name without 'bioRxiv/medRxiv/preprint' label", t))
            else:
                issues.append((n, loc, "Preprint citation (verify whether a peer-reviewed published version is now available)", t))
        if re.search(r'\bretract(?:ed|ion)\b|10\.\d{4,9}/\S*(?:retract|errat)', t, re.I):
            issues.append((n, loc, "Retraction / erratum indicator detected in reference entry or DOI", t))
        # Broken words where a single capital letter is separated by a space from its stem (e.g. "B acteria")
        for m_br in re.finditer(r'\b([A-Z])\s+([a-z]{4,})\b', t):
            joined = (m_br.group(1) + m_br.group(2)).lower()
            if joined in all_words and m_br.group(1) not in ('A',):
                issues.append((n, loc, f"Broken word '{m_br.group(0)}' (matches '{m_br.group(1) + m_br.group(2)}' elsewhere in refs)", t))
        for m_ocr in re.finditer(r'\b[A-Za-z]+(?:biotie|bacteriurn|pathoqen|infeetion|celI)\b', t, re.I):
            issues.append((n, loc, f"Suspected OCR/spelling corruption in title word '{m_ocr.group(0)}'", t))
        # Truncated page range (e.g. "6-." or "124–," where ending page number is missing)
        t_no_doi = re.sub(r'https?://\S+|doi\s*:?\s*10\.\S+', '', t, flags=re.I)
        m_trunc = re.search(r'(?:[:,;]\s*|\bpp?\.\s*)(\d+\s*[\u2013\u2014-])(?=\s*[.,;)]|\s*$)', t_no_doi)
        if m_trunc:
            issues.append((n, loc, f"Truncated page range '{m_trunc.group(1)}' (missing ending page number)", t))
        # Editorial / draft placeholders left inside reference entry
        m_ed = re.search(r'(\(\s*if\s+this\s+paper[^)]*\)|\b(?:TODO|TBD|FIXME|insert\s+ref|citation\s+needed|manuscript\s+submitted|in\s+preparation)\b)', t, re.I)
        if m_ed:
            issues.append((n, loc, f"Editorial placeholder / unpublished note '{m_ed.group(0)}' inside reference", t))
    return issues


def extract_author_year_callouts(text):
    """Extract (surname_lower, year_lower, raw_label) tuples and duplicate-year artifacts from text."""
    text = _fold_diacritics(re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', text))
    callouts = []
    dup_year_artifacts = []

    # 1. Detect duplicate-year citation artifacts: e.g. "Author et al, 2019, 2019" or "Author & Coauthor, 2020, 2020"
    dup_pat = re.compile(
        r'\b((?:(?:van|von|de|del|la|le|st\.?|mc|mac|o\')\s+)?[A-Z][A-Za-z\'\u2019\-]+'
        r'(?:\s+et\s+al\.?|\s*(?:&|and)\s*(?:(?:van|von|de|del|la|le|st\.?|mc|mac|o\')\s+)?[A-Z][A-Za-z\'\u2019\-]+)?'
        r'\s*,\s*((?:19|20)\d{2}[a-z]?)\s*,\s*\2)\b'
    )
    for m in dup_pat.finditer(text):
        dup_year_artifacts.append(m.group(1))

    # 2. Parenthetical citation blocks: e.g. "(Author et al., 2020; Other & Person, 2018, 2019a)"
    cite_item_pat = re.compile(
        r'\b((?:(?:van|von|de|del|della|di|du|la|le|st\.?|mc|mac|o\')\s+)?[A-Z][A-Za-z\'\u2019\-]+)'
        r'(?:\s+et\s+al\.?|\s*(?:&|and)\s*(?:(?:van|von|de|del|della|di|du|la|le|st\.?|mc|mac|o\')\s+)?[A-Z][A-Za-z\'\u2019\-]+)?'
        r'\s*,?\s*((?:19|20)\d{2}[a-z]?(?:\s*,\s*(?:19|20)\d{2}[a-z]?)*)\b'
    )
    for m_paren in re.finditer(r'\(([^()]*?\b(?:19|20)\d{2}[a-z]?[^()]*)\)', text):
        inner = m_paren.group(1)
        for m_c in cite_item_pat.finditer(inner):
            surname = m_c.group(1).strip().lower().replace('\u2019', "'")
            if surname in ('fig', 'figure', 'table', 'movie', 'video', 'box', 'eq', 'panel', 'see', 'e.g', 'i.e'):
                continue
            years_raw = m_c.group(2)
            for yr in re.findall(r'(?:19|20)\d{2}[a-z]?', years_raw):
                callouts.append((surname, yr.lower(), f"{m_c.group(1)} ({yr})"))

    # 3. Narrative citations: e.g. "Author et al. (2020)" or "Author and Coauthor (2019a, 2019b)"
    narr_pat = re.compile(
        r'\b((?:(?:van|von|de|del|della|di|du|la|le|st\.?|mc|mac|o\')\s+)?[A-Z][A-Za-z\'\u2019\-]+)'
        r'(?:\s+et\s+al\.?|\s+(?:&|and)\s+(?:(?:van|von|de|del|della|di|du|la|le|st\.?|mc|mac|o\')\s+)?[A-Z][A-Za-z\'\u2019\-]+)?'
        r'\s*\(\s*((?:19|20)\d{2}[a-z]?(?:\s*,\s*(?:19|20)\d{2}[a-z]?)*)\s*\)'
    )
    for m_n in narr_pat.finditer(text):
        surname = m_n.group(1).strip().lower().replace('\u2019', "'")
        if surname in ('fig', 'figure', 'table', 'movie', 'video', 'box', 'eq', 'panel', 'see'):
            continue
        for yr in re.findall(r'(?:19|20)\d{2}[a-z]?', m_n.group(2)):
            callouts.append((surname, yr.lower(), f"{m_n.group(1)} ({yr})"))

    return callouts, dup_year_artifacts


def extract_docx_zotero_cites(docx_path):
    """Extract (surname_lower, year_str, title) from Zotero CSL_CITATION fields in word/document.xml."""
    import json as _json
    out = []
    if not docx_path or not os.path.isfile(docx_path):
        return out
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
    ref_start, ref_end, refs, ref_ptags, is_author_year = find_refs_block(lines, hp, return_style=True)
    if style_arg == 'author-year':
        is_author_year = True

    if not refs:
        print("No references parsed. Check --refs-heading or citation style.")
        return

    max_ref = max(refs)
    non_ref_lines = [l for l in (lines[:ref_start] + lines[ref_end:]) if not l.startswith('# MANUSCRIPT-AUDIT EXTRACT')]
    body_text = '\n'.join(non_ref_lines)
    recurring_tokens = build_recurring_alnum_tokens(body_text)

    ref_cite_sites = {}
    dangling_ay = []
    dup_year_findings = []
    zotero_items = extract_docx_zotero_cites(docx_path) if docx_path else []

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
        paren_only_pat = re.compile(r'(?<!\bstep\s)(?<!\beq\.\s)(?<!\bfig\.\s)(?<!\bfigure\s)(?<!\btable\s)\((\d{1,3}(?:\s*[,;\u2013\u2014-]\s*\d{1,3})*)\)', re.I)
        brack_only_pat = re.compile(r'(?<![A-Za-z0-9_\[])\[(\d{1,3}(?:\s*[,;\u2013\u2014-]\s*\d{1,3})*)\](?!\])')
        paren_matches = [m.group(0) for m in paren_only_pat.finditer(re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', body_text)) if _parse_num_expr(m.group(1), max_ref)]
        brack_matches = [m.group(0) for m in brack_only_pat.finditer(re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', body_text)) if _parse_num_expr(m.group(1), max_ref)]

        paren_cites = extract_numeric_callouts(body_text, max_ref, allow_superscript=False)
        use_sup = len(paren_cites) < len(refs) * 0.5 or style_arg == 'superscript'
        cites = extract_numeric_callouts(body_text, max_ref, allow_superscript=use_sup, recurring_tokens=recurring_tokens)
        if docx_path:
            cites.update(extract_docx_superscript_cites(docx_path, max_ref))

        # Map each reference number to the exact paragraphs [P#] where it is cited
        for idx_ln, line in enumerate(non_ref_lines, 1):
            ptag, clean = strip_ptag(line)
            if not clean.strip():
                continue
            lcites = extract_numeric_callouts(clean, max_ref, allow_superscript=use_sup, recurring_tokens=recurring_tokens)
            loc_label = ptag or f"line {idx_ln}"
            for c in lcites:
                sites = ref_cite_sites.setdefault(c, [])
                if loc_label not in sites:
                    sites.append(loc_label)

    print(f"== 1. REFERENCE INVENTORY ==")
    mode_str = "Author-Year (unnumbered bibliography)" if is_author_year else "Numbered bibliography"
    print(f"Parsed {len(refs)} reference entries [{mode_str}] (range: {min(refs)}..{max_ref}, lines {ref_start+1}..{ref_end})")
    if zotero_items:
        print(f"  Active Zotero CSL_CITATION items detected in DOCX XML: {len(zotero_items)}")
    missing_nums = sorted(set(range(min(refs), max_ref + 1)) - set(refs))
    if missing_nums and not is_author_year:
        print(f"  !! Gaps in reference numbering: {missing_nums}")
    elif not is_author_year:
        print("  Numbering sequence: contiguous (no gaps)")

    print(f"\n== 2. DUPLICATE / NEAR-DUPLICATE REFERENCES ==")
    keys = {n: norm_entry(t) for n, t in refs.items()}
    ns = sorted(keys)
    dups = []
    for i in range(len(ns)):
        for j in range(i + 1, len(ns)):
            a, b = ns[i], ns[j]
            ka, kb = keys[a], keys[b]
            r = difflib.SequenceMatcher(None, ka, kb).ratio()
            head = len(ka) >= 45 and ka[:45] == kb[:45]
            if r > 0.82 or head:
                dups.append((a, b, r, head))
                sites_a = ", ".join(ref_cite_sites.get(a, ["uncited"]))
                sites_b = ", ".join(ref_cite_sites.get(b, ["uncited"]))
                print(f"  !! DUPLICATE CANDIDATE: Ref {a} ({ref_ptags[a]}) ~= Ref {b} ({ref_ptags[b]}) "
                      f"[similarity={r:.2f}{', identical prefix' if head else ''}]")
                print(f"     [{a}] {refs[a][:120]}")
                print(f"     [{b}] {refs[b][:120]}")
                print(f"     -> In-text citation sites: Ref [{a}] cited at {sites_a}; Ref [{b}] cited at {sites_b}")
                print(f"     -> Action required if [{b}] is removed: re-point callout [{b}] -> [{a}] at {sites_b}, "
                      f"and shift Refs {b+1}..{max_ref} down by -1")
    if not dups:
        print("  None detected.")

    print(f"\n== 3. REFERENCE FORMATTING & METADATA ANOMALIES ==")
    fmt_issues = audit_ref_formatting(refs, ref_ptags)
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
                            f"Software version mismatch: Ref [{n}] has '{m_v.group(1)} v{r_ver}', whereas {b_loc} has '{b_name} v{b_ver}'",
                            t
                        ))
    if fmt_issues:
        for n, loc, reason, txt in fmt_issues:
            print(f"  !! Ref {n} ({loc}): {reason}")
            print(f"     {txt[:130]}")
    else:
        print("  None detected.")

    print(f"\n== 4. IN-TEXT CALLOUT COVERAGE & CITATION STYLE (MAIN TEXT + TABLES + LEGENDS) ==")
    if is_author_year:
        print("  In-text citation style distribution: Author-Year parenthetical/narrative citations")
        if dup_year_findings:
            for loc_lbl, d_art in dup_year_findings:
                print(
                    f"  !! DUPLICATE-YEAR CITATION ARTIFACT in {loc_lbl}: '{d_art}' "
                    f"(likely two unmerged citation manager entries from the same year; disambiguate as YYYY-a/b or merge)"
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
        uncited = sorted(set(refs) - cites)
        dangling = sorted(cites - set(refs))
        print(f"Distinct reference numbers cited in document: {len(cites)} / {len(refs)}")
        print(f"Uncited references: {uncited if uncited else 'none'}")
        print(f"Dangling callouts (number > {max_ref} or missing from list): {dangling if dangling else 'none'}")

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
                scites = sorted(extract_numeric_callouts(sent, max_ref, allow_superscript=use_sup, recurring_tokens=recurring_tokens))
                if scites:
                    loc_str = f"{ptag} " if ptag else ""
                    print(f"\n  {loc_str}{sent.strip()}")
                    for c in scites:
                        if c in refs:
                            print(f"    -> [{c}] {refs[c][:115]}")
                        else:
                            print(f"    -> [{c}] !! DANGLING (not in reference list)")


if __name__ == '__main__':
    main()
