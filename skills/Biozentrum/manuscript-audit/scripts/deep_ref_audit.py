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
    "identified", "characterization", "identification", "mechanism", "mechanisms",
}


def parse_refs_file_or_manuscript(lines):
    """Parse numbered references from either a full manuscript or an extracted refs.txt file."""
    # Check if the file is already a list of numbered references (e.g. from docx2txt.py --extract-refs)
    entry_pat = re.compile(r'^\s*(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3})[.)]|(\d{1,3})\t)\s*(.+)')
    direct_refs = {}
    direct_ptags = {}
    non_empty = [l for l in lines if l.strip()]
    first_matches = sum(1 for l in non_empty[:10] if entry_pat.match(l))
    if non_empty and first_matches >= min(3, len(non_empty)):
        cur = None
        for idx, raw in enumerate(lines):
            ptag, clean = strip_ptag(raw)
            clean = clean.strip()
            if not clean:
                continue
            m = entry_pat.match(clean)
            if m:
                num = int(m.group(1) or m.group(2) or m.group(3) or m.group(4))
                cur = num
                direct_refs[num] = m.group(5).strip()
                direct_ptags[num] = ptag or f"line {idx+1}"
            elif cur is not None:
                direct_refs[cur] += " " + clean
        return 0, len(lines), direct_refs, direct_ptags, False, False

    ref_start, ref_end, refs, ref_ptags, is_author_year = find_refs_block(lines, return_style=True)
    return ref_start, ref_end, refs, ref_ptags, True, is_author_year


def _looks_like_author_list(seg):
    """Return True if `seg` is predominantly author surnames and initials (e.g. 'Sauer, M.; Hamel, V.')."""
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
    """Heuristically extract (first_author, year, title_guess, venue_guess, doi) from a reference string."""
    t = re.sub(r'^\s*(?:\[\d+\]|\(\d+\)|\d+[.)\t])\s*', '', ref_text.strip()).strip()
    doi_m = re.search(r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', t)
    doi = doi_m.group(1).rstrip('.,;)') if doi_m else None

    years = [int(m.group(0)) for m in re.finditer(r'\b(19\d{2}|20\d{2})\b', t)]
    year = years[-1] if years else None
    # Prefer parenthesized year if present
    m_pyr = re.search(r'\(\s*(19\d{2}|20\d{2})[a-z]?\s*\)', t)
    if m_pyr:
        year = int(m_pyr.group(1))

    # Extract first author surname (diacritic-folded)
    t_folded = _fold_diacritics(t)
    m_auth = re.match(
        r"^\s*((?:(?:van|von|de|del|della|di|du|la|le|st\.?|mc|mac|o')\s+)?[A-Z][A-Za-z'\u2019-]+)",
        t_folded,
        re.I,
    )
    first_author = m_auth.group(1).strip().lower().replace('\u2019', "'") if m_auth else ""

    cleaned_no_doi = re.sub(r'https?://\S+|doi\s*:?\s*10\.\S+', '', t, flags=re.I).strip()

    # Collapse periods after single-letter author initials (e.g. "M. D." or "M.; ") so re.split on sentence periods
    # does not shatter author blocks in Nature, Cell, or ACS bibliography styles.
    protected = re.sub(r'\b([A-Z])\.\s+(?=[A-Z]\.|&|and\b|[A-Z][a-z]+,\s*[A-Z])', r'\1_INIT_ ', cleaned_no_doi)
    protected = re.sub(r'\b([A-Z])\.;\s*', r'\1_INIT_; ', protected)
    parts = [
        p.replace('_INIT_', '.').strip()
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
            # Prefer the first non-author segment that contains >= 3 words and is not just a journal+volume tail
            chosen_idx, title_guess = non_auth[0]
            for idx, p in non_auth:
                if len(re.findall(r'\b[A-Za-z]{3,}\b', p)) >= 3 and not re.search(r'^\S+\s+\d+\s*[,:;]\s*\d+', p):
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
    """Query Crossref API for a single reference. Returns (item_dict_or_None, error_or_None)."""
    headers = {
        "User-Agent": f"ManuscriptAudit/1.0 ({'mailto:' + email if email else 'https://api.crossref.org'})",
        "Accept": "application/json",
    }
    try:
        if parsed["doi"]:
            url = "https://api.crossref.org/works/" + urllib.parse.quote(parsed["doi"], safe="")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                return data.get("message"), None

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
            return best_item, None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, str(e)


def extract_cr_year(item):
    """Extract publication year from Crossref work item."""
    for field in ("published-print", "published-online", "issued", "created"):
        dp = item.get(field, {}).get("date-parts", [])
        if dp and dp[0] and isinstance(dp[0][0], int):
            return dp[0][0]
    return None


def compare_ref_with_crossref(num, raw_ref, parsed, item):
    """Compare manuscript reference against Crossref metadata and return list of findings."""
    findings = []
    cr_titles = item.get("title") or []
    cr_title = re.sub(r'<[^>]+>', '', cr_titles[0]).strip() if cr_titles else ""
    cr_year = extract_cr_year(item)
    cr_doi = item.get("DOI", "")
    cr_containers = (item.get("container-title") or []) + (item.get("short-container-title") or [])
    cr_authors = item.get("author") or []
    cr_first_author = (cr_authors[0].get("family", "") if cr_authors else "").lower()

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

    if title_word_recall < 0.35 and not parsed["doi"]:
        if not author_ok:
            findings.append(
                f"No high-confidence Crossref match found (top hit: '{cr_title[:80]}' [{cr_year}], "
                f"DOI {cr_doi}) — verify reference manually."
            )
            return findings
        else:
            findings.append(
                f"Low title overlap ({title_word_recall:.0%}) with top Crossref hit by '{cr_first_author.title()}' "
                f"({cr_year}): '{cr_title[:90]}' (DOI {cr_doi}) — verify title accuracy."
            )
            return findings

    # 1. Check for Retraction / Correction / Expression of Concern / Preprint Version-of-Record
    if re.match(r'^(?:retracted|retraction|expression\s+of\s+concern)\b', cr_title, re.I) or item.get("update-to"):
        updates = item.get("update-to") or []
        up_types = [u.get("type", "update") for u in updates]
        if up_types or "retract" in cr_title.lower():
            findings.append(f"CRITICAL: Crossref flags update/retraction notice ({up_types or cr_title[:60]}) for DOI {cr_doi}!")

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
            f"Crossref has '{cr_first_author.title()}' (DOI {cr_doi})."
        )

    # 3. Publication year check
    if parsed["year"] and cr_year:
        diff = abs(parsed["year"] - cr_year)
        if diff >= 2:
            findings.append(
                f"Publication year discrepancy: manuscript has {parsed['year']}, Crossref has {cr_year} (DOI {cr_doi})."
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
                    f"Minor year offset (±1 yr): manuscript has {parsed['year']}, Crossref dates are {sorted(all_cr_years)} (DOI {cr_doi})."
                )

    # 4. Journal / venue check (with ISO-4 abbreviation normalization & full-ref container check)
    if parsed["venue_guess"] and cr_containers:
        if not journals_match(parsed["venue_guess"], cr_containers, raw_ref=raw_ref):
            findings.append(
                f"Venue/journal check: manuscript has '{parsed['venue_guess']}', "
                f"Crossref container is '{cr_containers[0]}' (DOI {cr_doi})."
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
                        f"Crossref title has '{cw_raw}' (DOI {cr_doi})."
                    )
                    break

    return findings


def extract_keywords(text):
    """Extract significant >=4-char content word stems from a text string."""
    words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9-]{3,}\b', text.lower())
    return {w[:6] for w in words if w not in STOPWORDS and not w.isdigit()}


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def query_ncbi_geo(gse_acc, timeout=8.0):
    """Query NCBI E-utilities for a GEO Series/Dataset accession (e.g. GSE100001). Returns (summary_str_or_None, err_or_None)."""
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


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)

    path = sys.argv[1]
    email = opt("--email", None)
    offline = "--offline" in sys.argv
    timeout = float(opt("--timeout", "8"))
    refs_filter_str = opt("--refs", None)
    default_cache = "scratch_audit/webcheck_cache.json" if os.path.isdir("scratch_audit") else None
    cache_path = opt("--cache", default_cache)
    cache_data = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            cache_data = json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            cache_data = {}

    lines = open(path, encoding="utf-8").read().splitlines()
    ref_start, ref_end, refs, ref_ptags, is_full_ms, is_author_year = parse_refs_file_or_manuscript(lines)

    if not refs:
        sys.exit("No references found in input file.")

    selected_nums = sorted(refs.keys())
    if refs_filter_str:
        wanted = _parse_num_expr(refs_filter_str, max(refs.keys()))
        selected_nums = [n for n in selected_nums if n in wanted]

    print("== 1. LOCAL REFERENCE METADATA COMPLETENESS ==")
    parsed_map = {}
    local_issues = 0
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
        if missing:
            local_issues += 1
            print(f"  !! Ref [{n}] ({ref_ptags.get(n, '')}): Missing or unparseable {', '.join(missing)}")
            print(f"     {raw[:140]}")
    if local_issues == 0:
        print(f"  All {len(selected_nums)} inspected reference(s) contain author, year, and title segments.")

    print("\n== 2. LIVE CROSSREF BIBLIOGRAPHIC VERIFICATION ==")
    net_failed = False
    if offline:
        print("  [WEB VERIFICATION: SKIPPED (--offline flag supplied)]")
    else:
        verified = 0
        flagged = 0
        cache_dirty = False
        for idx, n in enumerate(selected_nums):
            raw = refs[n]
            p = parsed_map[n]
            cache_key = f"cr:{p['doi'] or raw[:160]}"
            if cache_key in cache_data:
                item, err = cache_data[cache_key], None
            else:
                item, err = query_crossref(p, raw, email=email, timeout=timeout)
                if not err and cache_path:
                    cache_data[cache_key] = item
                    cache_dirty = True
                time.sleep(0.08)
            if err:
                if idx == 0:
                    print(f"  [WEB VERIFICATION: SKIPPED — live Crossref lookup unavailable in this environment ({err})]")
                    print("  Continuing with offline reference checks only.")
                    net_failed = True
                    break
                continue
            if item is None:
                flagged += 1
                print(f"  ? Ref [{n}] ({ref_ptags.get(n, '')}): No Crossref result returned — verify manually:")
                print(f"    {raw[:130]}")
                continue
            verified += 1
            issues = compare_ref_with_crossref(n, raw, p, item)
            if issues:
                flagged += 1
                print(f"  !! Ref [{n}] ({ref_ptags.get(n, '')}): {raw[:120]}")
                for iss in issues:
                    print(f"     - {iss}")
        if cache_dirty and cache_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
                json.dump(cache_data, open(cache_path, "w", encoding="utf-8"), indent=2)
            except Exception:
                pass
        if not net_failed:
            print(f"  Crossref check summary: {verified}/{len(selected_nums)} resolved, {flagged} with notes/discrepancies.")

    if is_full_ms:
        print("\n== 2B. REPOSITORY ACCESSION & DATASET VERIFICATION ==")
        acc_pat = re.compile(r"\b(GSE\d{4,8}|GSM\d{4,8}|GDS\d{3,8}|PRJNA\d{4,8}|PRJEB\d{4,8}|PXD\d{5,8}|E-MTAB-\d{3,8}|EMD-\d{4,6})\b")
        bare_geo_pat = re.compile(
            r"\b(GEO|Gene\s+Expression\s+Omnibus)\b[^.]{0,55}?\b(?:accession(?:\s+number)?|number|id|under|:)\s+(\d{5,8})\b",
            re.I,
        )
        found_accs = []
        for line in lines[:ref_start] + lines[ref_end:]:
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
        non_ref_lines = [l for l in (lines[:ref_start] + lines[ref_end:]) if not l.startswith("# MANUSCRIPT-AUDIT EXTRACT")]
        body_text = "\n".join(non_ref_lines)
        max_ref = max(refs.keys())
        recurring_tokens = build_recurring_alnum_tokens(body_text)
        paren_cites = extract_numeric_callouts(body_text, max_ref, allow_superscript=False) if not is_author_year else set()
        use_sup = (len(paren_cites) < len(refs) * 0.5) if not is_author_year else False

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

        ref_kw = {n: extract_keywords(refs[n]) for n in refs}
        queue_count = 0
        for line in non_ref_lines:
            ptag, clean = strip_ptag(line)
            if not clean.strip():
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
                    scites = sorted(extract_numeric_callouts(sent, max_ref, allow_superscript=use_sup, recurring_tokens=recurring_tokens))
                if not scites:
                    continue
                skw = extract_keywords(sent)
                if len(skw) < 3:
                    continue
                zero_overlap_cites = [
                    c for c in scites
                    if c in ref_kw and len(ref_kw[c]) >= 3 and not (skw & ref_kw[c])
                ]
                if zero_overlap_cites:
                    queue_count += 1
                    loc_str = f"{ptag} " if ptag else ""
                    print(f"\n  [REVIEW QUEUE] {loc_str}{sent.strip()[:160]}")
                    for c in zero_overlap_cites:
                        print(f"    ? [{c}] (0 shared keyword stems): {refs[c][:125]}")
        if queue_count == 0:
            print("  [REVIEW QUEUE (0 items)] All citing sentences share at least one keyword stem with their cited references.")


if __name__ == "__main__":
    main()
