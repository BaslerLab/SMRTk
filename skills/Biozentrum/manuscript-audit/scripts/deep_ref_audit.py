#!/usr/bin/env python3
"""Deep bibliography & citation verification (offline metadata checks + optional live Crossref resolution).

Zero external dependencies (uses Python 3 standard library only).

Usage:
  deep_ref_audit.py TEXT_OR_REFS.txt [--email you@example.org] [--offline] [--refs 1,5,10-20] [--timeout 8]

Accepts either:
  - Full indexed manuscript text (`docx2txt.py FILE.docx --indexed > indexed.txt`), OR
  - Extracted references list (`docx2txt.py FILE.docx --extract-refs refs.txt`)

What it checks:
  1. Local reference metadata completeness (author, 4-digit year, title, journal/source, volume/pages/DOI,
     preprint server status).
  2. Live Crossref bibliographic verification (automatically skipped if `--offline` or network is unreachable):
     - Queries Crossref by DOI (if present) or bibliographic query (`query.bibliographic` + `query.author`).
     - Verifies Title similarity, First-Author surname, Publication Year, Journal/Venue (comparing against both
       Crossref full `container-title`, `short-container-title`, and ISO-4 abbreviation stems so abbreviations
       like `PNAS` or `J. Biol. Chem.` do not trigger false mismatches), and Retraction/Correction status.
  3. Sentence-to-reference topical review queue (when run on a full manuscript text file):
     - Explicitly labeled as a manual `REVIEW QUEUE` (never auto-reported as errors, since methods/software
       papers naturally share fewer surface keywords with domain prose).
"""
import sys
import os
import re
import json
import time
import difflib
import urllib.request
import urllib.parse
import urllib.error

# Import reference parser and callout extractor from sibling ref_audit.py
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from ref_audit import (
    find_refs_block,
    strip_ptag,
    extract_numeric_callouts,
    extract_author_year_callouts,
    _parse_author_year_from_ref,
    _extract_all_ref_surnames,
    _fold_diacritics,
    build_recurring_alnum_tokens,
    _parse_num_expr,
)

try:
    from docx2txt import extract_embedded_csl_json
except ImportError:
    extract_embedded_csl_json = None

# Common ISO-4 / journal abbreviation stem expansions to prevent false-positive journal mismatches
JOURNAL_ABBREV_MAP = {
    "pnas": "proceedings national academy sciences",
    "jbc": "journal biological chemistry",
    "jacs": "journal american chemical society",
    "nar": "nucleic acids research",
    "jem": "journal experimental medicine",
    "jci": "journal clinical investigation",
    "nejm": "new england journal medicine",
    "jama": "journal american medical association",
    "bmj": "british medical journal",
    "embo": "embo journal",
    "proc": "proceedings",
    "natl": "national",
    "acad": "academy",
    "sci": "sciences",
    "scis": "sciences",
    "j": "journal",
    "journ": "journal",
    "int": "international",
    "eur": "european",
    "am": "american",
    "soc": "society",
    "roy": "royal",
    "chem": "chemistry",
    "chemical": "chemistry",
    "biochem": "biochemistry",
    "biochemical": "biochemistry",
    "biol": "biology",
    "biological": "biology",
    "microbiol": "microbiology",
    "microbiological": "microbiology",
    "bacteriol": "bacteriology",
    "virol": "virology",
    "immunol": "immunology",
    "infect": "infection",
    "immun": "immunity",
    "mol": "molecular",
    "cell": "cellular",
    "dev": "development",
    "developmental": "development",
    "genet": "genetics",
    "genom": "genomics",
    "med": "medicine",
    "medical": "medicine",
    "clin": "clinical",
    "invest": "investigation",
    "pathol": "pathology",
    "pathog": "pathogens",
    "pharmacol": "pharmacology",
    "phys": "physics",
    "physical": "physics",
    "appl": "applied",
    "mater": "materials",
    "eng": "engineering",
    "biotechnol": "biotechnology",
    "bioeng": "bioengineering",
    "env": "environmental",
    "environ": "environmental",
    "ecol": "ecology",
    "evol": "evolution",
    "evolutionary": "evolution",
    "rev": "reviews",
    "ann": "annals",
    "annu": "annual",
    "lett": "letters",
    "commun": "communications",
    "rep": "reports",
    "trans": "transactions",
    "res": "research",
    "curr": "current",
    "opin": "opinion",
    "adv": "advances",
    "meth": "methods",
    "prot": "protocols",
    "protoc": "protocols",
    "struct": "structural",
    "comput": "computational",
    "syst": "systems",
    "synth": "synthetic",
    "neuro": "neuroscience",
    "neurosci": "neuroscience",
    "oncol": "oncology",
    "cardiol": "cardiology",
}

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "within", "between",
    "through", "during", "under", "over", "after", "before", "upon", "using", "based",
    "via", "their", "these", "those", "such", "were", "was", "are", "have", "has",
    "had", "been", "being", "also", "both", "each", "other", "more", "most", "some",
    "many", "several", "various", "different", "specific", "distinct", "novel", "new",
    "high", "low", "large", "small", "role", "effect", "effects", "analysis", "study",
    "studies", "reveals", "reveal", "show", "shows", "shown", "demonstrate", "demonstrates",
    "demonstrated", "identified", "characterization", "identification", "mechanism", "mechanisms",
    "indicate", "indicates", "indicated", "indicating", "suggest", "suggests", "suggested",
    "observe", "observed", "report", "reports", "reported", "describe", "describes", "described",
    "provide", "provides", "provided", "require", "requires", "required", "involve", "involves",
    "involved", "mediate", "mediates", "mediated", "induce", "induces", "induced", "promote",
    "promotes", "promoted", "regulate", "regulates", "regulated", "contain", "contains", "contained",
    "include", "includes", "included", "present", "presents", "presented", "process", "processes",
    "function", "functions", "functional", "activity", "activities", "active", "system", "systems",
    "result", "results", "resulting", "important", "essential", "critical", "significant",
    "significantly", "previously", "known", "found", "similar", "related", "associated",
    "dependent", "independent", "response", "responses", "level", "levels", "number", "numbers",
}


def parse_refs_file_or_manuscript(lines):
    """Parse numbered references from either a full manuscript or an extracted refs.txt file."""
    # Check if the file is already a list of numbered references (e.g. from docx2txt.py --extract-refs)
    entry_pat = re.compile(r'^\s*(?:\[([sS]?\d{1,3})\]|\(([sS]?\d{1,3})\)|([sS]?\d{1,3})[.)]|([sS]?\d{1,3})\t)\s*(.+)')
    direct_refs = {}
    direct_ptags = {}
    direct_supp_hits = 0
    non_empty = [l for l in lines if l.strip() and not l.startswith("# MANUSCRIPT-AUDIT EXTRACT")]
    first_matches = sum(1 for l in non_empty[:10] if entry_pat.match(strip_ptag(l)[1].strip()))
    if non_empty and first_matches >= min(3, len(non_empty)):
        cur = None
        for idx, raw in enumerate(lines):
            if raw.startswith("# MANUSCRIPT-AUDIT EXTRACT"):
                continue
            ptag, clean = strip_ptag(raw)
            clean = clean.strip()
            if not clean:
                continue
            m = entry_pat.match(clean)
            if m:
                raw_num = m.group(1) or m.group(2) or m.group(3) or m.group(4)
                if raw_num.lower().startswith("s"):
                    direct_supp_hits += 1
                    num = int(raw_num[1:])
                else:
                    num = int(raw_num)
                cur = num
                direct_refs[num] = m.group(5).strip()
                direct_ptags[num] = ptag or f"line {idx+1}"
            elif cur is not None:
                direct_refs[cur] += " " + clean
        is_supp = direct_supp_hits >= max(2, len(direct_refs) // 2)
        return 0, len(lines), direct_refs, direct_ptags, False, False, is_supp, [(0, len(lines))]

    ref_start, ref_end, refs, ref_ptags, is_author_year, ref_ranges, is_supp_ref = find_refs_block(
        lines, return_style=True, return_meta=True
    )
    return ref_start, ref_end, refs, ref_ptags, True, is_author_year, is_supp_ref, ref_ranges


def _looks_like_author_list(seg):
    """Return True if `seg` is predominantly author surnames and initials (e.g. 'Smith, J.; Doe, A.')."""
    if ';' in seg and re.search(r'[A-Z][a-z]+,\s*[A-Z]\.', seg):
        return True
    if re.search(r'\bet\s+al\.?$', seg.strip(), re.I):
        return True
    # Count initial tokens like 'M.', 'D.', 'F. G.', 'J.-P.'
    initials = len(re.findall(r'\b[A-Z](?:-[A-Z])?\.', seg))
    words = re.findall(r'\b[a-z]{4,}\b', seg)
    if initials >= 2 and len(words) <= 2:
        return True
    return False


def parse_single_ref_fields(ref_text):
    """Heuristically extract (first_author, year, title_guess, venue_guess, doi,doi_source) from a reference string."""
    t = re.sub(r'^\s*(?:\[[sS]?\d+\]|\([sS]?\d+\)|[sS]?\d+[.)\t])\s*', '', ref_text.strip()).strip()
    doi_m = re.search(r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', t)
    doi = doi_m.group(1).rstrip('.,;)') if doi_m else None

    # Strip URLs, DOIs, and page ranges BEFORE extracting years so DOI segments (.2007., .2018.) are never mistaken for years
    cleaned_no_doi = re.sub(r'https?://\S+|doi\s*:?\s*10\.\S+|\b10\.\d{4,9}/\S+', '', t, flags=re.I).strip()
    cleaned_no_doi_no_pages = re.sub(r'\b\d{1,5}\s*[\u2013\u2014-]\s*\d{1,5}\b', ' ', cleaned_no_doi)
    # Also strip disambiguation tags like "Science (1979)" before year extraction
    cleaned_for_year = re.sub(r'\b(?:Science|Nature|Cell|Lancet)\s*\(\s*19\d{2}\s*\)', ' ', cleaned_no_doi_no_pages)

    years = [int(m.group(1)) for m in re.finditer(r'\b(19\d{2}|20\d{2})[a-z]?\b', cleaned_for_year)]
    year = years[-1] if years else None
    # Prefer parenthesized year if present
    m_pyr = re.search(r'\(\s*(19\d{2}|20\d{2})[a-z]?\s*\)', cleaned_for_year)
    if m_pyr:
        year = int(m_pyr.group(1))

    # Extract first author surname using _parse_author_year_from_ref first (handles inverted initials & compound surnames)
    parsed_sname = _parse_author_year_from_ref(t)[0]
    if parsed_sname and len(parsed_sname) >= 2:
        first_author = parsed_sname
    else:
        t_folded = _fold_diacritics(t)
        m_auth = re.match(
            r"^\s*((?:(?:van|von|de|del|della|di|du|la|le|st\.?|mc|mac|o')\s+)?[A-Z][A-Za-z'\u2019-]+)",
            t_folded,
            re.I,
        )
        first_author = m_auth.group(1).strip().lower().replace('\u2019', "'") if m_auth else ""

    # Collapse periods after single-letter author initials (e.g. "M. D." or "M.; ") and taxonomic abbreviations
    # (e.g. "C. trachomatis") so re.split on sentence periods does not shatter author blocks or species titles.
    protected = re.sub(r'\b([A-Z])\.\s+(?=[A-Z]\.|&|and\b|[A-Z][a-z]+,\s*[A-Z])', r'\1_INIT_ ', cleaned_no_doi)
    protected = re.sub(r'\b([A-Z])\.;\s*', r'\1_INIT_; ', protected)
    protected = re.sub(r'\b([A-Z])\.\s+(?=[a-z]{3,}\b)', r'\1_SP_ ', protected)
    parts = [
        p.replace('_INIT_', '.').replace('_SP_', '.').strip()
        for p in re.split(r'(?<![A-Z])\.\s+|\(\d{4}[a-z]?\)\.?\s*', protected)
        if len(p.strip()) > 3
    ]

    # If the first segment still has an initial period followed by the title (e.g. "Author, A. B. Title words here"),
    # split after the last initial (`[A-Z]\.\s+(?=[A-Z][a-z]{2,}\s+[a-z])`)
    expanded_parts = []
    for idx_p, p in enumerate(parts):
        if idx_p == 0:
            m_end_auth = re.search(
                r'^(.*?(?:\b[A-Z](?:-[A-Z])?\.|\bet\s+al\.))\s+([A-Z0-9\u0391-\u03c9][A-Za-z0-9\u0391-\u03c9\s,\-:;()/\'""\u2018-\u201d]{12,})$',
                p,
            )
            if m_end_auth and not re.match(r'^[A-Z][a-z]+,\s*[A-Z]\.', m_end_auth.group(2)):
                expanded_parts.append(m_end_auth.group(1).strip())
                expanded_parts.append(m_end_auth.group(2).strip())
                continue
        expanded_parts.append(p)
    parts = expanded_parts

    title_guess = ""
    venue_guess = ""
    if len(parts) >= 2:
        non_auth = [(idx, p) for idx, p in enumerate(parts) if not (idx == 0 and _looks_like_author_list(p)) and not _looks_like_author_list(p)]
        if non_auth:
            # Prefer the first non-author segment that contains >= 3 words (or >=2 words with a colon/subtitle) and is not just a journal+volume tail
            chosen_idx, title_guess = non_auth[0]
            for idx, p in non_auth:
                w_cnt = len(re.findall(r'\b[A-Za-z]{3,}\b', p))
                if (w_cnt >= 3 or (w_cnt >= 2 and ':' in p)) and not re.search(r'^\S+\s+\d+\s*[,:;]\s*\d+', p):
                    chosen_idx, title_guess = idx, p
                    break
            if chosen_idx + 1 < len(parts):
                venue_guess = " ".join(parts[chosen_idx + 1:])
        else:
            title_guess = parts[1]
            venue_guess = " ".join(parts[2:]) if len(parts) > 2 else ""
    else:
        title_guess = cleaned_no_doi

    # Fallback: if title_guess is still short (< 10 chars) but the reference itself is >= 45 chars,
    # use the longest non-DOI clause so valid references are never falsely flagged as "Missing title"
    if len(title_guess.strip()) < 10 and len(cleaned_no_doi) >= 45:
        clauses = [c.strip() for c in re.split(r'[.;]\s+', cleaned_no_doi) if len(c.strip()) >= 15]
        if clauses:
            title_guess = max(clauses, key=len)

    # Strip trailing volume/issue/page numbers from venue_guess
    venue_clean = re.sub(r'\b(?:19|20)\d{2}\b.*$', '', venue_guess)
    venue_clean = re.sub(r'\b\d+\s*(?:\([^)]+\))?\s*[,:;]\s*[eE]?\d+.*$', '', venue_clean)
    venue_clean = re.sub(r'\b\d+\s*$', '', venue_clean).strip(' .,;:')
    if _looks_like_author_list(venue_clean):
        venue_clean = ""

    return {
        "first_author": first_author,
        "year": year,
        "title_guess": title_guess.strip(' .,;:""\u201c\u201d'),
        "venue_guess": venue_clean,
        "doi": doi,
        "doi_source": "manuscript" if doi else None,
    }


def normalize_journal_tokens(name):
    """Normalize a journal name or ISO-4 abbreviation into canonical word tokens."""
    s = re.sub(r'[^a-zA-Z0-9\s]', ' ', name.lower())
    raw_toks = [w for w in s.split() if w not in ("of", "the", "and", "in", "on", "for", "to", "a", "usa", "u", "s")]
    expanded = []
    for tok in raw_toks:
        mapped = JOURNAL_ABBREV_MAP.get(tok, tok)
        expanded.extend(mapped.split())
    return set(expanded)


def journals_match(ms_venue, cr_titles, raw_ref=""):
    """Return True if manuscript venue string (or reference tail) matches any Crossref container-title or short-container-title."""
    if not cr_titles:
        return True
    # Check both the extracted venue_guess and the full reference string so split abbreviations (e.g. 'Nat. Rev. Microbiol.') match
    ms_toks = normalize_journal_tokens(ms_venue) if ms_venue else set()
    ref_toks = normalize_journal_tokens(raw_ref) if raw_ref else set()
    if not ms_toks and not ref_toks:
        return True
    for ct in cr_titles:
        cr_toks = normalize_journal_tokens(ct)
        if not cr_toks:
            continue
        # 1. Check if Crossref container tokens appear in the full reference
        if ref_toks:
            cr_in_ref = sum(
                1 for ct_w in cr_toks
                if ct_w in ref_toks or any(rw.startswith(ct_w[:5]) or ct_w.startswith(rw[:5]) for rw in ref_toks if len(rw) >= 4 and len(ct_w) >= 4)
            )
            if cr_in_ref / max(1, len(cr_toks)) >= 0.65:
                return True
        # 2. Check ms_venue token overlap
        if ms_toks:
            if ms_toks == cr_toks or ms_toks.issubset(cr_toks) or cr_toks.issubset(ms_toks):
                return True
            matched_stems = 0
            for mt in ms_toks:
                if any(ct_w.startswith(mt[:5]) or mt.startswith(ct_w[:5]) for ct_w in cr_toks if len(mt) >= 4 and len(ct_w) >= 4):
                    matched_stems += 1
                elif mt in cr_toks:
                    matched_stems += 1
            if matched_stems / max(1, len(ms_toks)) >= 0.65:
                return True
    return False


def query_crossref(parsed, raw_ref, email=None, timeout=8.0):
    """Query Crossref API for a single reference. Returns (item_dict_or_None, error_or_None).
    Note: HTTP 400/404 on a single DOI does NOT abort live Crossref verification — it falls back to
    query.bibliographic and records `_doi_404` on the returned item so the invalid/unregistered DOI is flagged.
    """
    headers = {
        "User-Agent": f"ManuscriptAudit/1.0 ({'mailto:' + email if email else 'https://api.crossref.org'})",
        "Accept": "application/json",
    }
    doi_404 = None
    try:
        if parsed["doi"]:
            url = "https://api.crossref.org/works/" + urllib.parse.quote(parsed["doi"], safe="")
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    item = data.get("message")
                    if isinstance(item, dict):
                        item["_lookup_mode"] = "doi"
                    return item, None
            except urllib.error.HTTPError as he:
                if he.code in (400, 404):
                    doi_404 = parsed["doi"]
                else:
                    return None, str(he)

        params = {"rows": "3"}
        # Strip leading author list when title_guess is substantial so Crossref ranks by article title + year
        bib_q = parsed["title_guess"] if len(parsed["title_guess"]) >= 25 else raw_ref[:240]
        if parsed["year"]:
            bib_q = f"{bib_q} {parsed['year']}"
        if parsed["venue_guess"]:
            bib_q = f"{bib_q} {parsed['venue_guess']}"
        params["query.bibliographic"] = bib_q
        if parsed["first_author"]:
            params["query.author"] = parsed["first_author"]
        url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            items = data.get("message", {}).get("items", [])
            if not items:
                if doi_404:
                    return {"_doi_404": doi_404, "_lookup_mode": "fuzzy"}, None
                return None, None
            # Pick best candidate among top 3 by title similarity against reference text
            best_item = items[0]
            best_score = -1.0
            ref_norm = re.sub(r'[^a-z0-9 ]', ' ', raw_ref.lower())
            for it in items:
                cr_t = " ".join(it.get("title") or [])
                cr_norm = re.sub(r'[^a-z0-9 ]', ' ', cr_t.lower()).split()
                if not cr_norm:
                    continue
                overlap = sum(1 for w in cr_norm if len(w) > 3 and w in ref_norm) / max(1, len(cr_norm))
                if overlap > best_score:
                    best_score = overlap
                    best_item = it
            best_item = dict(best_item)
            best_item["_lookup_mode"] = "fuzzy"
            if doi_404:
                best_item["_doi_404"] = doi_404
            return best_item, None
    except urllib.error.HTTPError as he:
        if he.code in (400, 404):
            return None, None
        return None, str(he)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, str(e)


def extract_cr_year(item):
    """Extract publication year from Crossref work item."""
    for field in ("published-print", "published-online", "issued", "created"):
        dp = item.get(field, {}).get("date-parts", [])
        if dp and dp[0] and isinstance(dp[0][0], int):
            return dp[0][0]
    return None


def _is_book_reference(raw_ref, item=None):
    """Return True if the reference or Crossref item is a book/monograph/edition rather than a journal article."""
    if item and item.get("type") in ("book", "monograph", "edited-book", "reference-book", "book-chapter"):
        return True
    if re.search(r'\b(?:\d+(?:st|nd|rd|th)\s+ed\.?|edition|press|publisher|wiley|springer|elsevier|academic\s+press|cold\s+spring\s+harbor|oxford\s+univ|cambridge\s+univ|saunders|harper|mcgraw|crc\s+press)\b', raw_ref, re.I):
        return True
    return False


def compare_ref_with_crossref(num, raw_ref, parsed, item):
    """Compare manuscript reference against Crossref metadata and return list of findings."""
    findings = []
    if item.get("_doi_404"):
        findings.append(
            f"DOI '{item['_doi_404']}' returned HTTP 404 (Not Found) on Crossref (may be a non-Crossref registrar such as DataCite/mEDRA, or a malformed DOI — verify manually)."
        )
        if not item.get("title"):
            return findings

    cr_titles = item.get("title") or []
    cr_title = re.sub(r'<[^>]+>', '', cr_titles[0]).strip() if cr_titles else ""
    cr_year = extract_cr_year(item)
    cr_doi = item.get("DOI", "")
    cr_containers = (item.get("container-title") or []) + (item.get("short-container-title") or [])
    cr_authors = item.get("author") or []
    cr_first_author = (cr_authors[0].get("family", "") if cr_authors else "").lower()

    has_ms_doi = (parsed.get("doi_source") == "manuscript" and bool(parsed.get("doi")))
    is_fuzzy = (item.get("_lookup_mode") == "fuzzy" or not parsed.get("doi"))
    doi_label = (
        f"DOI {cr_doi}"
        if has_ms_doi
        else f"via Crossref fuzzy title search -> candidate DOI {cr_doi}; manuscript entry itself has NO DOI"
    )

    # Check if Crossref entry is even a plausible match for the queried reference
    ref_lower = re.sub(r'[^a-z0-9 ]', ' ', raw_ref.lower())
    cr_title_words = [w for w in re.sub(r'[^a-z0-9 ]', ' ', cr_title.lower()).split() if len(w) > 3 and w not in STOPWORDS]
    title_word_recall = (
        sum(1 for w in cr_title_words if w in ref_lower) / len(cr_title_words)
        if cr_title_words else 0.0
    )

    author_ok = (
        not parsed["first_author"]
        or not cr_first_author
        or parsed["first_author"] in cr_first_author
        or cr_first_author in parsed["first_author"]
        or difflib.SequenceMatcher(None, parsed["first_author"], cr_first_author).ratio() >= 0.75
    )

    if title_word_recall < 0.35 and not has_ms_doi:
        if _is_book_reference(raw_ref, item):
            return findings
        if not author_ok:
            findings.append(
                f"No high-confidence Crossref match found (top fuzzy hit: '{cr_title[:80]}' [{cr_year}], "
                f"{doi_label}) — verify reference manually."
            )
            return findings
        else:
            findings.append(
                f"Low title overlap ({title_word_recall:.0%}) with top Crossref hit by '{cr_first_author.title()}' "
                f"({cr_year}): '{cr_title[:90]}' ({doi_label}) — verify title accuracy."
            )
            return findings

    # 1. Check for Retraction / Correction / Expression of Concern / Preprint Version-of-Record
    if re.match(r'^(?:retracted|retraction|expression\s+of\s+concern)\b', cr_title, re.I) or item.get("update-to"):
        updates = item.get("update-to") or []
        up_types = [u.get("type", "update") for u in updates]
        if up_types or "retract" in cr_title.lower():
            findings.append(f"CRITICAL: Crossref flags update/retraction notice ({up_types or cr_title[:60]}) for {doi_label}!")

    rel = item.get("relation") or {}
    for rel_key in ("is-preprint-of", "has-version"):
        if rel_key in rel and rel[rel_key]:
            vor_ids = [r.get("id", "") for r in rel[rel_key] if r.get("id")]
            if vor_ids:
                findings.append(
                    f"Preprint/version notice: Crossref lists '{rel_key}' -> {vor_ids} (cite the peer-reviewed Version of Record if applicable)."
                )

    # 2. First-author check (when title clearly matched)
    if not author_ok and title_word_recall >= 0.6:
        findings.append(
            f"First-author mismatch: manuscript has '{parsed['first_author'].title()}', "
            f"Crossref has '{cr_first_author.title()}' ({doi_label})."
        )

    # 3. Publication year check (skip edition year offsets on fuzzy-matched books without a manuscript DOI)
    if parsed["year"] and cr_year and not (is_fuzzy and _is_book_reference(raw_ref, item)):
        diff = abs(parsed["year"] - cr_year)
        if diff >= 2:
            findings.append(
                f"Publication year discrepancy: manuscript has {parsed['year']}, Crossref has {cr_year} ({doi_label})."
            )
        elif diff == 1:
            # Check all date fields (online vs print often differ by 1 year)
            all_cr_years = {
                dp[0][0]
                for f in ("published-print", "published-online", "issued")
                for dp in [item.get(f, {}).get("date-parts", [])]
                if dp and dp[0] and isinstance(dp[0][0], int)
            }
            if parsed["year"] not in all_cr_years:
                findings.append(
                    f"Minor year offset (±1 yr): manuscript has {parsed['year']}, Crossref dates are {sorted(all_cr_years)} ({doi_label})."
                )

    # 4. Journal / venue check (with ISO-4 abbreviation normalization & full-ref container check)
    if parsed["venue_guess"] and cr_containers:
        if not journals_match(parsed["venue_guess"], cr_containers, raw_ref=raw_ref):
            findings.append(
                f"Venue/journal check: manuscript has '{parsed['venue_guess']}', "
                f"Crossref container is '{cr_containers[0]}' ({doi_label})."
            )

    # 5. Word-level title typo check against Crossref title (catches 1-char spelling typos in reference titles)
    ref_orig_words = {w.lower(): w for w in re.findall(r'\b[A-Za-z]{6,}\b', _fold_diacritics(raw_ref))}
    for cw_raw in re.findall(r'\b[A-Za-z]{6,}\b', _fold_diacritics(cr_title)):
        cw = cw_raw.lower()
        if cw in ref_orig_words:
            continue
        for rw_low, rw_orig in ref_orig_words.items():
            if abs(len(cw) - len(rw_low)) <= 1 and cw[:4] == rw_low[:4]:
                ratio = difflib.SequenceMatcher(None, cw, rw_low).ratio()
                if ratio >= 0.82 and not (cw + "s" == rw_low or rw_low + "s" == cw):
                    findings.append(
                        f"Title word spelling discrepancy vs Crossref: manuscript has '{rw_orig}', "
                        f"Crossref title has '{cw_raw}' ({doi_label})."
                    )
                    break

    return findings


def extract_keywords(text):
    """Extract significant >=4-char content word stems from a text string."""
    words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9-]{3,}\b', text.lower())
    return {w[:6] for w in words if w not in STOPWORDS and not w.isdigit()}


def _discover_and_load_csl_records(path, lines, parsed_map, refs):
    """Discover embedded CSL-JSON records from --csl / --csl-json, --docx, or the extract header and map them to ref numbers."""
    csl_path = opt("--csl", None) or opt("--csl-json", None)
    docx_path = opt("--docx", None)
    records = []

    if csl_path and os.path.isfile(csl_path):
        try:
            loaded = json.load(open(csl_path, encoding="utf-8"))
            if isinstance(loaded, dict):
                records = loaded.get("items") or loaded.get("records") or list(loaded.values())
            elif isinstance(loaded, list):
                records = loaded
        except Exception:
            records = []
    else:
        if not docx_path and lines and lines[0].startswith("# MANUSCRIPT-AUDIT EXTRACT"):
            m_path = re.search(r'\bpath=([^|]+)', lines[0])
            if m_path and os.path.isfile(m_path.group(1).strip()):
                docx_path = m_path.group(1).strip()
            else:
                m_file = re.search(r'\bfile=([^|]+)', lines[0]) or re.search(r'EXTRACT\s*\|\s*([^|]+?)\s*\|', lines[0])
                if m_file:
                    fname = m_file.group(1).strip()
                    base_dir = os.path.dirname(os.path.abspath(path))
                    for cand in (
                        os.path.join(base_dir, fname),
                        os.path.join(base_dir, "Original", fname),
                        os.path.join(os.path.dirname(base_dir), "Original", fname),
                        os.path.join(os.path.dirname(base_dir), fname),
                    ):
                        if os.path.isfile(cand):
                            docx_path = cand
                            break
        if docx_path and os.path.isfile(docx_path) and extract_embedded_csl_json is not None:
            try:
                import zipfile
                with zipfile.ZipFile(docx_path) as z:
                    res = extract_embedded_csl_json(z)
                    records = res[0] if isinstance(res, tuple) else res
            except Exception:
                records = []

    if not records:
        return {}

    # Normalize raw CSL-JSON fields (DOI/doi, author list, issued date-parts)
    norm_records = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        r = dict(rec)
        r["doi"] = (r.get("doi") or r.get("DOI") or "").strip()
        auth_val = r.get("author")
        if isinstance(auth_val, list) and auth_val and isinstance(auth_val[0], dict):
            r["author_family"] = auth_val[0].get("family", "")
        elif isinstance(auth_val, str):
            r["author_family"] = auth_val.split(",")[0].strip()
        else:
            r["author_family"] = ""
        if not r.get("year"):
            dp = (r.get("issued") or {}).get("date-parts") or []
            if dp and dp[0] and isinstance(dp[0][0], (int, str)):
                r["year"] = str(dp[0][0])
        norm_records.append(r)
    records = norm_records

    # Match CSL records to numbered references in `refs`
    csl_by_doi = {}
    for rec in records:
        d = (rec.get("doi") or "").strip().lower().rstrip(".,;)")
        if d:
            csl_by_doi[d] = rec

    csl_map = {}
    for n, raw in refs.items():
        p = parsed_map.get(n) or parse_single_ref_fields(raw)
        if p.get("doi") and p["doi"].lower() in csl_by_doi:
            csl_map[n] = csl_by_doi[p["doi"].lower()]
            continue
        ref_lower = re.sub(r'[^a-z0-9 ]', ' ', _fold_diacritics(raw).lower())
        best_rec = None
        best_score = 0.0
        for rec in records:
            ctitle = rec.get("title") or ""
            cwords = [w for w in re.sub(r'[^a-z0-9 ]', ' ', _fold_diacritics(ctitle).lower()).split() if len(w) > 3 and w not in STOPWORDS]
            if len(cwords) < 3:
                continue
            recall = sum(1 for w in cwords if w in ref_lower) / len(cwords)
            if recall < 0.65:
                continue
            cauth = _fold_diacritics(rec.get("author_family") or "").lower().strip()
            cyr = str(rec.get("year") or "")
            bonus = 0.0
            if cauth and cauth in ref_lower:
                bonus += 0.15
            if cyr and str(p.get("year") or "") == cyr:
                bonus += 0.15
            if recall + bonus > best_score:
                best_score = recall + bonus
                best_rec = rec
        if best_rec is not None and best_score >= 0.75:
            csl_map[n] = best_rec
    return csl_map


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def query_ncbi_geo(gse_acc, timeout=8.0):
    """Query NCBI E-utilities for a GEO Series/Dataset accession (e.g. GSE...). Returns (summary_str_or_None, err_or_None)."""
    headers = {"User-Agent": "ManuscriptAudit/1.0 (https://eutils.ncbi.nlm.nih.gov)"}
    try:
        q_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
            + urllib.parse.urlencode({"db": "gds", "term": f"{gse_acc}[ACCN]", "retmode": "json"})
        )
        req = urllib.request.Request(q_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        id_list = data.get("esearchresult", {}).get("idlist", [])
        if not id_list:
            return None, None
        s_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
            + urllib.parse.urlencode({"db": "gds", "id": id_list[0], "retmode": "json"})
        )
        req2 = urllib.request.Request(s_url, headers=headers)
        with urllib.request.urlopen(req2, timeout=timeout) as resp2:
            sdata = json.loads(resp2.read().decode("utf-8", errors="replace"))
        rec = sdata.get("result", {}).get(id_list[0], {})
        title = rec.get("title", "")
        taxon = rec.get("taxon", "")
        return f"{gse_acc}: \"{title[:110]}\" (Organism: {taxon})", None
    except Exception as e:
        return None, str(e)


def fetch_pubmed_or_epmc_abstract(parsed, raw_ref, cr_item=None, timeout=6.0):
    """Fetch abstract and publication type (e.g. Review) for a reference via Crossref JATS abstract,
    EuropePMC REST API, or NCBI PubMed E-utilities (esearch + efetch).
    Returns dict: {"abstract": str, "source": str, "is_review": bool, "pmid": str} or None on network error.
    """
    # 1. Check if Crossref item already includes a JATS abstract
    if isinstance(cr_item, dict) and cr_item.get("abstract"):
        raw_jats = cr_item.get("abstract") or ""
        clean_abs = re.sub(r"<[^>]+>", " ", raw_jats)
        clean_abs = re.sub(r"\s+", " ", clean_abs).strip()
        clean_abs = re.sub(r"^(?:Abstract|Summary)\s*[:.\-]?\s*", "", clean_abs, flags=re.I)
        if len(clean_abs) >= 40:
            is_rev = bool(re.search(r"\breview\b", " ".join(cr_item.get("type", "") if isinstance(cr_item.get("type"), list) else [str(cr_item.get("type", ""))]), re.I))
            return {"abstract": clean_abs, "source": "Crossref", "is_review": is_rev, "pmid": ""}

    doi = (parsed.get("doi") or (cr_item.get("DOI") if isinstance(cr_item, dict) else "") or "").strip()
    title_q = (parsed.get("title_guess") or "").strip()
    if not title_q and isinstance(cr_item, dict) and cr_item.get("title"):
        title_q = (cr_item["title"][0] if cr_item["title"] else "").strip()

    headers = {"User-Agent": "ManuscriptAudit/1.0 (https://eutils.ncbi.nlm.nih.gov)", "Accept": "application/json"}

    # 2. Try NCBI PubMed E-utilities (esearch -> efetch XML for full abstract & PublicationType)
    try:
        term = f"{doi}[LID]" if doi else ""
        if not term and len(title_q) >= 15:
            t_words = " ".join(re.findall(r"[A-Za-z0-9]{3,}", title_q)[:9])
            term = f"({t_words}[Title])"
            if parsed.get("first_author"):
                term += f" AND {parsed['first_author']}[Author]"
            if parsed.get("year"):
                term += f" AND {parsed['year']}[DP]"
        if term:
            es_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(
                {"db": "pubmed", "term": term, "retmax": "1", "retmode": "json"}
            )
            req = urllib.request.Request(es_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                es_data = json.loads(resp.read().decode("utf-8", errors="replace"))
            id_list = es_data.get("esearchresult", {}).get("idlist", [])
            if id_list:
                pmid = id_list[0]
                ef_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(
                    {"db": "pubmed", "id": pmid, "retmode": "xml"}
                )
                req_ef = urllib.request.Request(ef_url, headers=headers)
                with urllib.request.urlopen(req_ef, timeout=timeout) as resp_ef:
                    xml_txt = resp_ef.read().decode("utf-8", errors="replace")
                abs_parts = re.findall(r"<AbstractText[^>]*>(.*?)</AbstractText>", xml_txt, flags=re.S | re.I)
                clean_abs = re.sub(r"<[^>]+>", " ", " ".join(abs_parts))
                clean_abs = re.sub(r"\s+", " ", clean_abs).strip()
                is_rev = bool(re.search(r"<PublicationType[^>]*>\s*Review\s*</PublicationType>", xml_txt, re.I))
                if clean_abs:
                    return {"abstract": clean_abs, "source": f"PubMed (PMID:{pmid})", "is_review": is_rev, "pmid": pmid}
    except Exception:
        pass

    # 3. Fallback to EuropePMC REST API (returns abstractText & pubTypeList in a single JSON call)
    try:
        epmc_q = f'DOI:"{doi}"' if doi else ""
        if not epmc_q and len(title_q) >= 15:
            epmc_q = f'TITLE:"{title_q[:120]}"'
            if parsed.get("year"):
                epmc_q += f" AND PUB_YEAR:{parsed['year']}"
        if epmc_q:
            ep_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(
                {"query": epmc_q, "resultType": "core", "pageSize": "1", "format": "json"}
            )
            req_ep = urllib.request.Request(ep_url, headers=headers)
            with urllib.request.urlopen(req_ep, timeout=timeout) as resp_ep:
                ep_data = json.loads(resp_ep.read().decode("utf-8", errors="replace"))
            res_list = ep_data.get("resultList", {}).get("result", [])
            if res_list:
                r0 = res_list[0]
                clean_abs = re.sub(r"<[^>]+>", " ", r0.get("abstractText") or "")
                clean_abs = re.sub(r"\s+", " ", clean_abs).strip()
                pt_str = json.dumps(r0.get("pubTypeList") or "")
                is_rev = bool(re.search(r"\breview\b", pt_str, re.I))
                pmid = str(r0.get("pmid") or "")
                if clean_abs:
                    return {"abstract": clean_abs, "source": f"EuropePMC{f' (PMID:{pmid})' if pmid else ''}", "is_review": is_rev, "pmid": pmid}
    except Exception:
        pass

    return {"abstract": "", "source": "", "is_review": False, "pmid": ""}


def _extract_specific_entities(clause):
    """Extract technical symbols/identifiers (gene/protein/pathway/complex tokens such as BRCA1, TP53, CRISPR-Cas9, Hsp70)
    from a citing clause, excluding common citation/figure prefixes and Roman numerals.
    """
    cleaned = re.sub(r'\[\[IMAGE:[^\]]+\]\]', ' ', clause)
    cleaned = re.sub(r'\b(?:Fig(?:ure)?s?|Tables?|Videos?|Movies?|Box(?:es)?|Refs?|Equations?|Eqs?|Panels?|Suppl(?:ementary)?)\s*[A-Za-z0-9\-–—]*', ' ', cleaned, flags=re.I)
    entities = set()
    # Mixed-case or alphanumeric technical symbols (e.g. BRCA1, TP53, Cas9, Hsp70, Rab5a, Complex-1)
    for m in re.finditer(r'\b([A-Za-z][A-Za-z0-9\-]{1,12}[0-9A-Z][A-Za-z0-9\-]*)\b', cleaned):
        tok = m.group(1).strip('-')
        if len(tok) < 3 or tok.isdigit():
            continue
        if re.match(r'^[IVXLCDM]+$', tok):
            continue
        if re.match(r'^(?:19|20)\d{2}[a-z]?$', tok):
            continue
        if tok.lower() in ('and', 'the', 'for', 'with', 'dna', 'rna', 'mrna', 'rrna', 'trna', 'atp', 'gtp', 'gdp', 'adp', 'nad', 'nadh', 'pbs', 'pcr', 'qpcr', 'sd', 'sem', 'anova', 'fdr', 'usa', 'uk', 'eu'):
            continue
        entities.add(tok.lower())
    return entities


_NEGATION_RE = re.compile(
    r'\b(?:not\s+(?:defective|required|essential|necessary|involved|affected|observed|detected|changed|altered|regulated|induced)|'
    r'no\s+(?:significant|detectable|apparent|effect|difference|change|defect|role)|'
    r'independent(?:ly)?\s+of|dispensable\s+for|failed\s+to|did\s+not\s+(?:affect|alter|reduce|increase|inhibit|prevent|require)|'
    r'unable\s+to|lack(?:ed|ing)?\s+(?:any|a)\b|inhibit(?:s|ed|ing)|repress(?:es|ed|ing)|antagoniz(?:es|ed|ing)|suppress(?:es|ed|ing))\b',
    re.I,
)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)

    path = sys.argv[1]
    email = opt("--email", None)
    offline = "--offline" in sys.argv
    timeout = float(opt("--timeout", "8"))
    refs_filter_str = opt("--refs", None)
    default_cache = None
    for cand_c in ("scratch_audit/webcheck_cache.json", "webcheck_cache.json"):
        if os.path.isfile(cand_c):
            default_cache = cand_c
            break
    if not default_cache and os.path.isdir("scratch_audit"):
        default_cache = "scratch_audit/webcheck_cache.json"
    cache_path = opt("--cache", default_cache)
    cache_data = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            cache_data = json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            cache_data = {}

    lines = open(path, encoding="utf-8").read().splitlines()
    ref_start, ref_end, refs, ref_ptags, is_full_ms, is_author_year, is_supp_ref, ref_ranges = parse_refs_file_or_manuscript(lines)

    if not refs:
        sys.exit("No references found in input file.")

    def ref_label(n):
        return f"S{n}" if is_supp_ref else str(n)

    selected_nums = sorted(refs.keys())
    if refs_filter_str:
        wanted = _parse_num_expr(refs_filter_str, max(refs.keys()))
        selected_nums = [n for n in selected_nums if n in wanted]

    print("== 1. LOCAL REFERENCE METADATA COMPLETENESS ==")
    parsed_map = {}
    local_issues = 0
    # Check whether the bibliography predominantly includes author initials
    refs_with_initials = sum(
        1 for n in selected_nums
        if re.search(r'^\s*(?:\[[sS]?\d+\]|\([sS]?\d+\)|[sS]?\d+[.)\t])?\s*[^.]{2,60}\b[A-Z]\.', refs[n])
    )
    expects_initials = (len(selected_nums) >= 5 and refs_with_initials >= len(selected_nums) * 0.8)

    for n in selected_nums:
        raw = refs[n]
        p = parse_single_ref_fields(raw)
        parsed_map[n] = p
        missing = []
        if not p["first_author"]:
            missing.append("first author")
        if not p["year"]:
            missing.append("4-digit year")
        if len(p["title_guess"]) < 10:
            missing.append("article/book title")
        if expects_initials:
            body_no_num = re.sub(r'^\s*(?:\[[sS]?\d+\]|\([sS]?\d+\)|[sS]?\d+[.)\t])\s*', '', raw)
            if re.match(r"^[A-Za-z'\u2019\-]+\s+et\s+al\.?", body_no_num) and not re.match(r"^[A-Za-z'\u2019\-]+,\s*[A-Z]", body_no_num):
                missing.append("first-author initials before 'et al.'")
        if missing:
            local_issues += 1
            print(f"  !! Ref [{ref_label(n)}] ({ref_ptags.get(n, '')}): Missing or unparseable {', '.join(missing)}")
            print(f"     {raw[:140]}")
    if local_issues == 0:
        print(f"  All {len(selected_nums)} inspected reference(s) contain author, year, and title segments.")

    csl_map = _discover_and_load_csl_records(path, lines, parsed_map, refs)
    if csl_map:
        abs_cnt = sum(1 for r in csl_map.values() if r.get("abstract"))
        doi_cnt = sum(1 for r in csl_map.values() if r.get("doi"))
        print(f"  [EMBEDDED CSL-JSON METADATA]: Matched {len(csl_map)}/{len(refs)} references from document field codes ({doi_cnt} with DOI, {abs_cnt} with full abstract).")
        # Enrich missing parsed DOIs from embedded CSL-JSON (recording doi_source='csl')
        for n, rec in csl_map.items():
            if n in parsed_map and not parsed_map[n].get("doi") and rec.get("doi"):
                parsed_map[n]["doi"] = rec["doi"].strip().rstrip(".,;)")
                parsed_map[n]["doi_source"] = "csl"

    print("\n== 2. LIVE CROSSREF & PUBMED/EUROPEPMC BIBLIOGRAPHIC + ABSTRACT VERIFICATION ==")
    net_failed = False
    verified = 0
    flagged = 0
    pubmed_abs_fetched = 0
    cache_dirty = False
    cr_items_map = {}
    pubmed_abs_map = {}
    if offline and not cache_data:
        print("  [WEB VERIFICATION: SKIPPED (--offline flag supplied)]")
    else:
        for idx, n in enumerate(selected_nums):
            raw = refs[n]
            p = parsed_map[n]
            ck_primary = f"cr:{p['doi'] or raw[:160]}"
            ck_alt = f"cr:{raw[:160]}"
            if ck_primary in cache_data:
                item, err = cache_data[ck_primary], None
            elif ck_alt in cache_data:
                item, err = cache_data[ck_alt], None
            elif offline or net_failed:
                continue
            else:
                item, err = query_crossref(p, raw, email=email, timeout=timeout)
                if not err:
                    cache_data[ck_primary] = item
                    if cache_path:
                        cache_dirty = True
                time.sleep(0.08)
            if err:
                if not net_failed:
                    print(f"  [WEB VERIFICATION: SKIPPED — live Crossref/PubMed lookup unavailable in this environment ({err})]")
                    print("  Continuing with cached/embedded CSL-JSON reference checks only.")
                    net_failed = True
                continue
            if item is None:
                flagged += 1
                print(f"  ? Ref [{ref_label(n)}] ({ref_ptags.get(n, '')}): No Crossref result returned — verify manually:")
                print(f"    {raw[:130]}")
                continue
            cr_items_map[n] = item
            verified += 1
            issues = compare_ref_with_crossref(n, raw, p, item)
            if issues:
                flagged += 1
                print(f"  !! Ref [{ref_label(n)}] ({ref_ptags.get(n, '')}): {raw[:120]}")
                for iss in issues:
                    print(f"     - {iss}")

            # Fetch PubMed / EuropePMC / Crossref abstract when no embedded CSL-JSON abstract is present
            has_csl_abs = bool((csl_map.get(n) or {}).get("abstract"))
            if not has_csl_abs:
                ck_abs = f"abs:{p.get('doi') or (item.get('DOI') if isinstance(item, dict) else '') or raw[:160]}"
                if ck_abs in cache_data and isinstance(cache_data[ck_abs], dict):
                    abs_info = cache_data[ck_abs]
                elif not offline and not net_failed:
                    abs_info = fetch_pubmed_or_epmc_abstract(p, raw, cr_item=item, timeout=min(timeout, 5.0))
                    if abs_info is not None:
                        cache_data[ck_abs] = abs_info
                        cache_dirty = True
                    time.sleep(0.08)
                else:
                    abs_info = None
                if isinstance(abs_info, dict) and abs_info.get("abstract"):
                    pubmed_abs_map[n] = abs_info
                    pubmed_abs_fetched += 1

        if cache_dirty and cache_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
                json.dump(cache_data, open(cache_path, "w", encoding="utf-8"), indent=2)
            except Exception:
                pass
        if verified > 0 or not net_failed:
            print(
                f"  Crossref + PubMed check summary: {verified}/{len(selected_nums)} resolved, "
                f"{pubmed_abs_fetched} additional PubMed/EuropePMC abstract(s) retrieved, {flagged} with notes/discrepancies."
            )

    # Also check for Preprint references in the local bibliography (bioRxiv, medRxiv, arXiv, Research Square, 10.1101/)
    preprint_re = re.compile(r'\b(?:biorxiv|medrxiv|arxiv|chemrxiv|research\s+square|ssrn|preprint|10\.1101/|10\.21203/)\b', re.I)
    for n in selected_nums:
        if preprint_re.search(refs[n]):
            print(f"  [PREPRINT NOTICE] Ref [{ref_label(n)}] ({ref_ptags.get(n, '')}) cites a preprint ('{refs[n][:110]}...') — verify whether a peer-reviewed journal Version of Record has since been published.")

    excluded_ref_idxs = set()
    for rs, re_idx in ref_ranges:
        excluded_ref_idxs.update(range(rs, re_idx))
    non_ref_lines = [
        l for idx, l in enumerate(lines)
        if idx not in excluded_ref_idxs and not l.startswith("# MANUSCRIPT-AUDIT EXTRACT")
    ]

    if is_full_ms:
        print("\n== 2B. REPOSITORY ACCESSION & DATASET VERIFICATION ==")
        acc_pat = re.compile(r"\b(GSE\d{4,8}|GSM\d{4,8}|GDS\d{3,8}|PRJNA\d{4,8}|PRJEB\d{4,8}|PXD\d{5,8}|E-MTAB-\d{3,8}|EMD-\d{4,6})\b")
        bare_geo_pat = re.compile(
            r"\b(GEO|Gene\s+Expression\s+Omnibus)\b[^.]{0,55}?\b(?:accession(?:\s+number)?|number|id|under|:)\s+(\d{5,8})\b",
            re.I,
        )
        found_accs = []
        for line in non_ref_lines:
            ptag, clean = strip_ptag(line)
            loc = ptag or "body"
            for m_a in acc_pat.finditer(clean):
                found_accs.append((loc, m_a.group(1), False))
            for m_b in bare_geo_pat.finditer(clean):
                num = m_b.group(2)
                print(f"  !! {loc}: Bare numeric GEO accession '{num}' is missing its official prefix (likely 'GSE{num}')!")
                found_accs.append((loc, f"GSE{num}", True))
        if not found_accs:
            print("  No repository accession codes detected in manuscript text.")
        else:
            seen_acc = set()
            for loc, acc, was_bare in found_accs:
                if acc in seen_acc:
                    continue
                seen_acc.add(acc)
                if acc.startswith("GSE") and not offline and not net_failed:
                    ck = f"geo:{acc}"
                    if ck in cache_data:
                        g_sum, g_err = cache_data[ck], None
                    else:
                        g_sum, g_err = query_ncbi_geo(acc, timeout=timeout)
                        if not g_err and cache_path:
                            cache_data[ck] = g_sum
                            try:
                                json.dump(cache_data, open(cache_path, "w", encoding="utf-8"), indent=2)
                            except Exception:
                                pass
                    if g_sum:
                        print(f"  [VERIFIED GEO] {loc} -> {g_sum}")
                    elif not g_err:
                        print(f"  !! {loc}: GEO accession '{acc}' returned 0 records in NCBI GDS — verify accession number!")
                    else:
                        print(f"  - {loc}: Accession '{acc}' detected (live NCBI lookup skipped: {g_err})")
                else:
                    print(f"  - {loc}: Accession '{acc}' detected.")

    if is_full_ms:
        print("\n== 3. SENTENCE-TO-REFERENCE TOPICAL REVIEW QUEUE (INFORMATIONAL — VERIFY MANUALLY, DO NOT AUTO-REPORT) ==")
        print("  NOTE: Low lexical overlap is common for methods/software/statistical citations or domain synonyms.")
        print("        Treat items below ONLY as a manual reading queue; never report a citation mismatch without reading both.")
        body_text = "\n".join(non_ref_lines)
        max_ref = max(refs.keys())
        recurring_tokens = build_recurring_alnum_tokens(body_text)
        if is_author_year:
            paren_cites = set()
            use_sup = False
        else:
            paren_cites = extract_numeric_callouts(body_text, max_ref, allow_superscript=False, allow_s_prefix=is_supp_ref)
            use_sup = (len(paren_cites) < len(refs) * 0.5) if not is_supp_ref else False

        ay_exact = {}
        ay_base = {}
        if is_author_year:
            for rnum, rtxt in refs.items():
                _, all_snames, yr, base_yr = _extract_all_ref_surnames(rtxt)
                if all_snames and yr:
                    for sk in all_snames:
                        if rnum not in ay_exact.setdefault((sk, yr), []):
                            ay_exact[(sk, yr)].append(rnum)
                        if rnum not in ay_base.setdefault((sk, base_yr), []):
                            ay_base[(sk, base_yr)].append(rnum)

        # Separate title keywords from abstract keywords, and compute Corpus-IDF background stems
        # (any 6-char stem appearing in >= 20% of the manuscript's reference entries is background domain vocabulary
        # and must not silence topical review when abstracts are concatenated!)
        from collections import Counter as _Counter
        title_kw = {}
        abs_kw = {}
        ref_abs_text = {}
        ref_abs_source = {}
        ref_is_review = {}
        ref_full_text_low = {}
        stem_doc_freq = _Counter()
        review_venue_re = re.compile(
            r'\b(?:trends\s+in|nature\s+reviews?|annual\s+review|current\s+opinion|fems\s+microbiol\w*\s+rev|'
            r'microbiol\w*\s+mol\w*\s+biol\w*\s+rev|clin\w*\s+microbiol\w*\s+rev|physiol\w*\s+rev|chem\w*\s+rev|'
            r'pharmacol\w*\s+rev|immunol\w*\s+rev|biol\w*\s+rev|crit\w*\s+rev)\b',
            re.I,
        )
        for n in refs:
            csl_rec = csl_map.get(n) or {}
            pm_rec = pubmed_abs_map.get(n) or {}
            t_str = f"{refs[n]} {csl_rec.get('title', '')}"
            if csl_rec.get("abstract"):
                a_str = csl_rec.get("abstract") or ""
                a_src = "CSL-JSON"
            elif pm_rec.get("abstract"):
                a_str = pm_rec.get("abstract") or ""
                a_src = pm_rec.get("source") or "PubMed/EuropePMC"
            else:
                a_str = ""
                a_src = ""
            ref_abs_text[n] = a_str
            ref_abs_source[n] = a_src
            ref_is_review[n] = bool(
                pm_rec.get("is_review")
                or review_venue_re.search(t_str)
                or re.search(r'\b(?:a\s+review|review\s+of|overview\s+of)\b', t_str, re.I)
            )
            tk = extract_keywords(t_str)
            ak = extract_keywords(a_str)
            title_kw[n] = tk
            abs_kw[n] = ak
            ref_full_text_low[n] = (t_str + " " + a_str).lower()
            for st in tk:
                stem_doc_freq[st] += 1

        bg_threshold = max(4, int(len(refs) * 0.20))
        corpus_bg_stems = {st for st, df in stem_doc_freq.items() if df >= bg_threshold}
        primary_claim_re = re.compile(
            r'\b(?:first\s+(?:discovered|described|reported|identified|demonstrated|isolated|shown|observed)|'
            r'originally\s+(?:isolated|described|constructed|identified|reported)|'
            r'was\s+constructed\s+(?:by|in)|plasmid\s+from|strain\s+from|in\s+18\d{2}|in\s+19[0-6]\d)\b',
            re.I,
        )

        queue_count = 0
        digest_lines = [
            "# CITATION-BY-CITATION VERIFICATION DIGEST (SENTENCE -> REFERENCE TITLE + EMBEDDED/PUBMED ABSTRACT)",
            f"# Corpus-wide background stems auto-filtered (DF >= {bg_threshold}/{len(refs)} refs): {sorted(corpus_bg_stems)[:20]}",
            "",
        ]
        affil_re = re.compile(r'\b(?:department\s+of|university\s+of|institute\s+of|faculty\s+of|school\s+of|hospital|cedex|street|avenue|electronic\s+address|corresponding\s+author)\b|@', re.I)

        for idx_ln, line in enumerate(non_ref_lines):
            ptag, clean = strip_ptag(line)
            if not clean.strip():
                continue
            # Skip front-matter author list / affiliation lines ([P1]..[P6])
            if idx_ln < 10 and affil_re.search(clean):
                continue
            sents = re.split(r'(?<=[.?!])\s+(?=[A-Z0-9(\["\'])', clean)
            for sent in sents:
                if is_author_year:
                    s_calls, _ = extract_author_year_callouts(sent)
                    scites_set = set()
                    for sname, yr, _ in s_calls:
                        for c in (ay_exact.get((sname, yr)) or ay_base.get((sname, yr[:4]), [])):
                            scites_set.add(c)
                    scites = sorted(scites_set)
                else:
                    scites = sorted(
                        extract_numeric_callouts(
                            sent, max_ref, allow_superscript=use_sup, recurring_tokens=recurring_tokens, allow_s_prefix=is_supp_ref
                        )
                    )
                if not scites:
                    continue

                loc_str = f"{ptag} " if ptag else ""
                digest_lines.append(f"=== {loc_str}{sent.strip()} ===")
                skw = extract_keywords(sent)
                spec_skw = skw - corpus_bg_stems

                # Split multi-clause sentences (e.g. "; whereas ...", ", while ...", ", and ...") to isolate clause-specific citations
                subclauses = re.split(r';\s+|,\s+(?:whereas|while|but|and|or)\s+', sent)
                cite_to_clause = {}
                if len(subclauses) > 1 and not is_author_year:
                    for cl in subclauses:
                        cl_cites = extract_numeric_callouts(cl, max_ref, allow_superscript=use_sup, recurring_tokens=recurring_tokens, allow_s_prefix=is_supp_ref)
                        for cc in cl_cites:
                            cite_to_clause[cc] = cl

                flagged_in_sent = []
                for c in scites:
                    if c not in refs:
                        continue
                    clause_for_c = cite_to_clause.get(c, sent)
                    cl_kw = extract_keywords(clause_for_c)
                    cl_spec = (cl_kw - corpus_bg_stems) or spec_skw

                    tk_spec = title_kw[c] - corpus_bg_stems
                    ak_spec = abs_kw[c] - corpus_bg_stems
                    title_shared = cl_spec & tk_spec
                    abs_shared = cl_spec & ak_spec
                    any_shared = skw & (title_kw[c] | abs_kw[c])

                    # Check specific technical identifiers (e.g. gene/protein/pathway symbols) in the citing clause
                    cl_entities = _extract_specific_entities(clause_for_c)
                    missing_entities = [
                        ent for ent in sorted(cl_entities)
                        if ent not in ref_full_text_low[c] and ent[:4] not in ref_full_text_low[c]
                    ]

                    # Check abstract polarity / negation sentences (from CSL-JSON or PubMed/EuropePMC)
                    abs_txt = ref_abs_text.get(c) or ""
                    abs_src = ref_abs_source.get(c) or ""
                    neg_sents = []
                    if abs_txt:
                        for a_s in re.split(r'(?<=[.?!])\s+', abs_txt):
                            if _NEGATION_RE.search(a_s):
                                neg_sents.append(a_s.strip())

                    # Check if a primary discovery/historical/construct claim is attributed solely to a Review article
                    is_primary_to_review = bool(
                        len(scites) == 1
                        and ref_is_review.get(c)
                        and primary_claim_re.search(clause_for_c)
                        and (parsed_map.get(c, {}).get("year") or 2020) >= 2000
                    )

                    # Record in comprehensive digest
                    digest_lines.append(f"  -> [{ref_label(c)}] ({ref_ptags.get(c, '')}) {refs[c][:145]}")
                    if abs_txt:
                        digest_lines.append(f"     Abstract [{abs_src}]: {abs_txt[:340]}{'...' if len(abs_txt) > 340 else ''}")
                    if neg_sents:
                        digest_lines.append(f"     [ABSTRACT POLARITY/NEGATION CUE]: {' | '.join(neg_sents[:2])[:260]}")
                    if missing_entities:
                        digest_lines.append(f"     [CLAUSE IDENTIFIER CHECK]: Citing clause mentions {missing_entities}, not found in Ref [{ref_label(c)}] title/abstract.")
                    if is_primary_to_review:
                        digest_lines.append(f"     [PRIMARY CLAIM -> REVIEW ARTICLE CHECK]: Clause attributes a primary/historical discovery or construct to Ref [{ref_label(c)}], which appears to be a secondary Review article.")

                    # Decide whether to surface in the high-priority REVIEW QUEUE
                    reasons = []
                    if len(skw) >= 3 and not any_shared:
                        reasons.append("0 shared keyword stems with title/abstract")
                    elif len(cl_spec) >= 2 and not title_shared and len(abs_shared) <= 1:
                        reasons.append(f"0 non-background title stems & low specific abstract overlap ({sorted(abs_shared)})")
                    if missing_entities and len(scites) <= 3 and not title_shared:
                        reasons.append(f"clause identifier(s) {missing_entities} absent from ref title/abstract")
                    if neg_sents and not _NEGATION_RE.search(clause_for_c) and (title_shared or len(abs_shared) >= 2):
                        reasons.append(f"abstract contains negation/contrast cue ('{neg_sents[0][:110]}...')")
                    if is_primary_to_review:
                        reasons.append("primary/historical discovery or construct attributed to a secondary Review article")

                    if reasons:
                        flagged_in_sent.append((c, "; ".join(reasons), abs_txt))

                digest_lines.append("")
                if flagged_in_sent:
                    queue_count += 1
                    print(f"\n  [REVIEW QUEUE] {loc_str}{sent.strip()[:175]}")
                    for c, r_str, abs_txt in flagged_in_sent:
                        print(f"    ? [{ref_label(c)}] ({r_str}): {refs[c][:125]}")
                        if abs_txt:
                            print(f"      Abstract snippet: {abs_txt[:250]}...")

        if os.path.isdir("scratch_audit"):
            try:
                with open("scratch_audit/citation_verification_digest.txt", "w", encoding="utf-8") as df:
                    df.write("\n".join(digest_lines) + "\n")
                print(f"\n  [DIGEST SAVED]: Full sentence-to-reference + abstract verification digest written to scratch_audit/citation_verification_digest.txt ({len(digest_lines)} lines).")
            except Exception:
                pass
        if queue_count == 0:
            print("  [REVIEW QUEUE (0 items)] All citing sentences share specific keyword stems with their cited references.")


if __name__ == "__main__":
    main()
