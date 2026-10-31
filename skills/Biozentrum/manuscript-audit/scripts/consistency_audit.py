#!/usr/bin/env python3
"""Cross-manuscript nomenclature, typo, homophone, unit hygiene, quantitative recipe, and rebuttal audit.

Usage:
  consistency_audit.py TEXTFILE [--rebuttal REBUTTAL.txt] [--cover-letter COVER.txt] [--format-audit FORMAT_AUDIT.txt]

Works on plain or `--indexed` output (`[P12] ...`, `[T1] ...`) from `docx2txt.py`.

Reports:
  1. Consecutive duplicate words & duplicated multi-word clauses within the same sentence
  2. Nomenclature, scientific homophones & near-miss spelling anomalies:
     - Mutant / allele tokens (Δ/∆ symbol mixing, near-miss names like ΔgeneA vs ΔgeneA1)
     - Compound / strain / construct codes & hyphenation/transposition variants within the document
     - Taxonomic genus names (e.g. Bacillus vs Baccillus, 'Candidatus spp.' rank misuse)
     - Context-sensitive scientific homophones ("commercialized dies" -> "dyes", "mutant stain" -> "strain", "respiratory track" -> "tract", "unclear invasion" -> "nuclear invasion", etc.)
     - Filtered high-frequency bigram & singleton near-misses (with UK/US spelling mix separation)
  3. Unit, time abbreviation, degree symbol & parenthesis consistency (mL vs ml, µL vs uL, sec vs s, 17h vs 17 h, 37oC vs 37 °C)
  4. Quantitative & experimental parameter cross-check (dilution math, concentrations, time windows, MOIs, n)
  5. Journal declaration completeness & Author Contributions byline coverage check
  6. Rebuttal letter cross-audit (when --rebuttal is passed, with automatic line-drift & legend title matching)
  7. Cover letter cross-audit (when --cover-letter is passed, checking figure/table/video callouts & title/claim alignment)
"""
import sys
import os
import re
from collections import Counter, defaultdict


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


COMMON_STOP_FIRST_WORDS = {
    'water', 'after', 'under', 'other', 'their', 'there', 'where', 'these', 'those',
    'which', 'while', 'first', 'three', 'total', 'using', 'based', 'shown', 'found',
    'taken', 'given', 'prior', 'below', 'above', 'inner', 'outer', 'upper', 'lower',
    'early', 'later', 'large', 'small', 'short', 'whole', 'human', 'mouse', 'plant',
    'fixed', 'fresh', 'final', 'equal', 'every', 'about', 'since', 'until', 'among',
    'along', 'within', 'without', 'before', 'during', 'through', 'between', 'around',
}

ANTONYM_PREFIX_PAIRS = (
    ('intra', 'extra'),
    ('inter', 'intra'),
    ('pre', 'post'),
    ('sub', 'sup'),
    ('up', 'down'),
    ('in', 'ex'),
    ('un', 'in'),
    ('de', 're'),
    ('endo', 'exo'),
    ('homo', 'hetero'),
    ('mono', 'poly'),
    ('micro', 'macro'),
    ('hyper', 'hypo'),
    ('over', 'under'),
    ('vivo', 'vitro'),
)

SCIENTIFIC_HOMOPHONE_PATTERNS = [
    (
        re.compile(r'\b(commercial(?:ized)?|fluorescent|membrane|nuclear|vital|organic|cyanine|rhodamine|alex[a-z]*|ester|bright|stable)\s+(dies|died)(?=\d|\b)', re.I),
        "Suspected homophone typo '{match}' -> did you mean 'dyes' / 'dyed'?",
    ),
    (
        re.compile(r'\b(dies)\s+(?:such\s+as|like|including|for\s+labeling|for\s+staining|to\s+label|to\s+stain)\b', re.I),
        "Suspected homophone typo '{match}' -> did you mean 'dyes'?",
    ),
    (
        re.compile(r'\b(unclear)\s+(envelope|pore|pores|invasion|entry|localization|import|export|membrane|membranes|staining|fraction|extract|dna|compartment|lamina|matrix|periphery|region|regions|roi|rois)(?=\d|\b)', re.I),
        "Suspected autocorrect typo '{match}' -> did you mean 'nuclear {g2}'?",
    ),
    (
        re.compile(r'\b(gastrointestinal|respiratory|urinary|digestive|biliary|reproductive|genital|intestinal|vocal)\s+(tracks?)(?=\d|\b)', re.I),
        "Suspected homophone typo '{match}' -> anatomical 'tract(s)', not 'track(s)'",
    ),
    (
        re.compile(r'\b(mutant|wild-?type|parental|bacterial|virulent|avirulent|attenuated|isogenic|complemented|resistant|susceptible|knockout|deletion)\s+(stains?)(?=\d|\b)', re.I),
        "Suspected typo '{match}' -> did you mean 'strain(s)' instead of 'stain(s)'?",
    ),
    (
        re.compile(r'\b(compliment)\s+(fixation|cascade|system|activation|pathway|receptor|component|factor|mediated|lysis)(?=\d|\b)|\b(complimented)\s+(strain|mutant|allele|plasmid|cells?)(?=\d|\b)', re.I),
        "Suspected homophone typo '{match}' -> biological 'complement' / 'complemented' (with an 'e')",
    ),
    (
        re.compile(r'\b(principle)\s+(component|components|investigator|investigators|mechanism|finding|findings|cause|driver|axis)(?=\d|\b)', re.I),
        "Suspected homophone typo '{match}' -> 'principal' (main/primary), not 'principle'",
    ),
    (
        re.compile(r'\b(discreet)\s+(bands?|puncta|foci|populations?|clusters?|regions?|steps?|domains?|peaks?|subpopulations?|events?)(?=\d|\b)', re.I),
        "Suspected homophone typo '{match}' -> 'discrete' (distinct/separate), not 'discreet'",
    ),
    (
        re.compile(r'\b(?:no|little|significant|profound|marked|direct|adverse|inhibitory|stimulatory)\s+(affect)\s+on\b', re.I),
        "Suspected noun/verb typo '{match}' -> noun 'effect on', not 'affect on'",
    ),
    (
        re.compile(r'\b(statically)\s+(analy[sz]ed|significant|tested|compared|evaluated|assessed|different)\b', re.I),
        "Suspected malapropism '{match}' -> did you mean 'statistically {g2}'?",
    ),
    (
        re.compile(r'\b(rational)\s+for\s+(selection|choosing|using|design|inclusion|exclusion|testing|this)\b', re.I),
        "Suspected noun/adjective confusion '{match}' -> noun 'rationale for {g2}', not 'rational for'",
    ),
]


def strip_ptag(line):
    m = re.match(r'^\s*(\[(?:P\d+(?:\.\d+)?|T\d+(?:,\s*Row\s+\d+)?|TABLE\s+\d+|Row\s+\d+)\])\s*(.*)', line)
    if m:
        return m.group(1), m.group(2)
    return '', line


def is_antonym_pair(w1, w2):
    if {w1, w2} in (
        {'infected', 'uninfected'},
        {'treated', 'untreated'},
        {'biotic', 'abiotic'},
        {'specific', 'nonspecific'},
        {'motile', 'nonmotile'},
        {'invasive', 'noninvasive'},
        {'coding', 'noncoding'},
        {'polar', 'nonpolar'},
        {'viable', 'nonviable'},
        {'linear', 'nonlinear'},
        {'symmetric', 'asymmetric'},
        {'typical', 'atypical'},
        {'biotic', 'prebiotic'},
        {'cellular', 'acellular'},
    ):
        return True
    for p1, p2 in ANTONYM_PREFIX_PAIRS:
        if (w1.startswith(p1) and w2.startswith(p2) and w1[len(p1):] == w2[len(p2):] and len(w1[len(p1):]) >= 4):
            return True
        if (w1.startswith(p2) and w2.startswith(p1) and w1[len(p2):] == w2[len(p1):] and len(w1[len(p2):]) >= 4):
            return True
    return False


def _normalize_uk_us(w):
    """Normalize UK/US spelling variants to a common canonical form to detect UK/US mixes rather than typos."""
    s = w.lower()
    s = re.sub(r'our(s|ed|ing|able|ite|ites)?$', r'or\1', s)
    s = re.sub(r'bour', 'bor', s)
    s = re.sub(r'iour', 'ior', s)
    s = re.sub(r'([a-z]{3,})is(e|es|ed|ing|ation|ations)$', r'\1iz\2', s)
    s = re.sub(r'([a-z]{2,})ys(e|es|ed|ing)$', r'\1yz\2', s)
    s = re.sub(r'([a-z]{2,})ell(ed|ing|er|ers)$', r'\1el\2', s)
    s = re.sub(r'(cent|met|fib|lit|theat|cal|spect)re(s)?$', r'\1er\2', s)
    return s


def _is_inflection_pair(a, b):
    """Return True if a and b are morphological inflections or derivations of the same stem."""
    if a == b:
        return True
    suffixes = (
        '', 's', 'es', 'd', 'ed', 'ing', 'er', 'ers', 'or', 'ors', 'y', 'ly', 'ies', 'ied',
        'ia', 'iae', 'ii', 'um', 'us', 'is', 'sis', 'ses', 'sed', 'ze', 'zed', 'zes',
        'se', 'tes', 'ted', 'te', 'al', 'ally', 'ic', 'ical', 'ive', 'ion', 'ions',
        'ory', 'ry', 'ries', 'cy', 'cies', 'ty', 'ties', 'ate', 'ated', 'ates', 'ating',
    )
    for s1 in suffixes:
        if a.endswith(s1):
            stem = a[:-len(s1)] if s1 else a
            if len(stem) >= 4:
                for s2 in suffixes:
                    if stem + s2 == b:
                        return True
    na, nb = _normalize_uk_us(a), _normalize_uk_us(b)
    if na != a or nb != b:
        if na == nb:
            return True
        for s1 in suffixes:
            if na.endswith(s1):
                stem = na[:-len(s1)] if s1 else na
                if len(stem) >= 4:
                    for s2 in suffixes:
                        if stem + s2 == nb:
                            return True
    return False


def edit_distance_le2(a, b):
    if a == b or abs(len(a) - len(b)) > 2:
        return False
    dp = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        new_dp = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            new_dp.append(min(new_dp[-1] + 1, dp[j] + 1, dp[j - 1] + cost))
        dp = new_dp
    if len(a) == len(b):
        diffs = [k for k in range(len(a)) if a[k] != b[k]]
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]:
            return True
    return dp[-1] <= 2


def edit_distance_le1(a, b):
    """Return True if a and b have Levenshtein distance 1 or a single adjacent transposition."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [k for k in range(len(a)) if a[k] != b[k]]
        if len(diffs) == 1:
            return True
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]:
            return True
        return False
    short, long_s = (a, b) if len(a) < len(b) else (b, a)
    i = j = diffs = 0
    while i < len(short) and j < len(long_s):
        if short[i] == long_s[j]:
            i += 1
            j += 1
        else:
            diffs += 1
            j += 1
            if diffs > 1:
                return False
    return True


def build_manuscript_legend_map(raw_lines):
    """Extract a map of canonical display-item keys (e.g. 'Figure 1', 'Figure S5', 'Figure EV1', 'Video S1') to (ptag, title_preview)."""
    legend_map = {}
    leg_pat = re.compile(
        r'^(?:\[\[IMAGE:[^\]]+\]\]\s*)*(?:(Supplementary|Supplemental|Expanded\s+View|Extended\s+Data)\s+)?'
        r'(Figure|Fig\.?|Table|Video|Movie)\s+((?:EV|ED|S)?\d+)\s*[.:–—-]\s*(.+)',
        re.I
    )
    for line in raw_lines:
        ptag, clean = strip_ptag(line)
        m = leg_pat.match(clean.strip())
        if not m:
            continue
        sup_prefix, kind_raw, num_str, rest = m.group(1), m.group(2), m.group(3).upper(), m.group(4).strip()
        k_low = kind_raw.lower().rstrip('.')
        if k_low in ('fig', 'figure'):
            kind = 'Figure'
        elif k_low == 'table':
            kind = 'Table'
        else:
            kind = 'Video'
        if sup_prefix and not num_str.startswith(('S', 'EV', 'ED')):
            mod = sup_prefix.lower()
            num_str = ('EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')) + num_str
        key = f"{kind} {num_str}"
        first_sent = re.split(r'(?<=[.?!])\s+', rest)[0][:120]
        if key not in legend_map:
            legend_map[key] = f"{ptag} {key}. {first_sent}"
    return legend_map


def normalize_callout_key(callout_str):
    """Normalize 'Fig. S5B' -> 'Figure S5', 'Figure EV1a' -> 'Figure EV1', 'Movies EV1' -> 'Video EV1', etc."""
    m = re.match(
        r'^(?:(Supplementary|Supplemental|Expanded\s+View|Extended\s+Data)\s+)?(Figures?|Figs?\.?|Tables?|Videos?|Movies?)\s+((?:EV|ED|S)?\d+)',
        callout_str.strip(),
        re.I,
    )
    if not m:
        return None
    sup, kind_raw, num_str = m.group(1), m.group(2).lower().rstrip('.'), m.group(3).upper()
    if kind_raw.startswith('fig'):
        kind = 'Figure'
    elif kind_raw.startswith('tab'):
        kind = 'Table'
    else:
        kind = 'Video'
    if sup and not num_str.startswith(('S', 'EV', 'ED')):
        mod = sup.lower()
        num_str = ('EV' if 'expanded' in mod else ('ED' if 'extended' in mod else 'S')) + num_str
    return f"{kind} {num_str}"


def parse_format_audit_file(fmt_path):
    """Parse output of `docx2txt.py --audit-format` to map line numbers to paragraphs and empty-line drift."""
    if not fmt_path or not os.path.isfile(fmt_path):
        return 0, []
    txt = open(fmt_path, encoding='utf-8').read()
    m_emp = re.search(r'Empty spacer paragraphs without <w:suppressLineNumbers/>:\s*(\d+)', txt)
    empty_count = int(m_emp.group(1)) if m_emp else 0
    row_pat = re.compile(
        r'^\s*\[(P\d+)\s*\]\s*\|\s*L(\d+)\s*-\s*(\d+)\s*\|\s*(?:Word\s+L(\d+)\s*-\s*(\d+)|\(suppressed\))\s*\|\s*(.*)$'
    )
    rows = []
    for ln in txt.splitlines():
        m = row_pat.match(ln)
        if m:
            pid = m.group(1)
            ne_s, ne_e = int(m.group(2)), int(m.group(3))
            w_s = int(m.group(4)) if m.group(4) else ne_s
            w_e = int(m.group(5)) if m.group(5) else ne_e
            rows.append((pid, ne_s, ne_e, w_s, w_e, m.group(6).strip()))
    return empty_count, rows


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        sys.exit(__doc__)

    path = sys.argv[1]
    reb_path = None
    if '--rebuttal' in sys.argv:
        reb_path = sys.argv[sys.argv.index('--rebuttal') + 1]
    cov_path = None
    if '--cover-letter' in sys.argv:
        cov_path = sys.argv[sys.argv.index('--cover-letter') + 1]
    fmt_path = None
    if '--format-audit' in sys.argv:
        fmt_path = sys.argv[sys.argv.index('--format-audit') + 1]

    # Collect companion / supplementary text files passed via positional args (sys.argv[2:])
    supp_txt_paths = []
    skip_next = False
    for idx_a in range(2, len(sys.argv)):
        if skip_next:
            skip_next = False
            continue
        arg = sys.argv[idx_a]
        if arg in ('--rebuttal', '--cover-letter', '--format-audit', '--ignore'):
            skip_next = True
            continue
        if not arg.startswith('-') and os.path.isfile(arg) and arg.lower().endswith('.txt'):
            supp_txt_paths.append(arg)

    ignore_set = load_ignore_set()
    raw_lines = [l for l in open(path, encoding='utf-8').read().splitlines() if not l.startswith('# MANUSCRIPT-AUDIT EXTRACT')]
    for sp in supp_txt_paths:
        sp_base = os.path.basename(sp)
        for idx_l, ln in enumerate(open(sp, encoding='utf-8', errors='replace').read().splitlines(), 1):
            if ln.startswith('# MANUSCRIPT-AUDIT EXTRACT') or not ln.strip():
                continue
            ptag_sp, clean_sp = strip_ptag(ln)
            if ptag_sp:
                raw_lines.append(f"[{sp_base}:{ptag_sp[1:-1]}] {clean_sp}")
            else:
                raw_lines.append(f"[{sp_base}:L{idx_l}] {clean_sp}")

    legend_map = build_manuscript_legend_map(raw_lines)
    empty_spacer_count, fmt_rows = parse_format_audit_file(fmt_path)

    body_lines = []
    in_refs = False
    cur_tbl = None
    for line in raw_lines:
        m_th = re.match(r'^\s*(?:\[[^\]:]+:)?(TABLE\s+\d+)\]?\s*$', line)
        if m_th:
            cur_tbl = f"T{re.search(r'\d+', m_th.group(1)).group(0)}"
            in_refs = False
            continue
        ptag, clean = strip_ptag(line)
        if not ptag:
            m_sp_ptag = re.match(r'^\s*(\[[^\]]+\])\s*(.*)', line)
            if m_sp_ptag:
                ptag, clean = m_sp_ptag.group(1), m_sp_ptag.group(2)
        if ('Row ' in ptag or ptag.startswith('[Row')) and cur_tbl and not ptag.startswith(f"[{cur_tbl}"):
            ptag = f"[{cur_tbl}, {ptag[1:-1]}]"
        if re.match(r'^\s*(?:\d+\.?\s+)?(?:main\s+|supplementary\s+|supplemental\s+)?(?:references|bibliography|literature\s+cited)\s*:?\s*$', clean, re.I):
            in_refs = True
            continue
        if in_refs and (
            re.match(
                r'^\s*(?:(?:main\s+|supplementary\s+|expanded\s+view\s+|extended\s+data\s+)?(?:figure\s+legends?|figures?|tables?|video\s+legends?|methods?|note|text)|'
                r'data\s+(?:and\s+code\s+)?availability|code\s+availability|acknowledg(?:e)?ments?|funding|author\s+contributions?|competing\s+interests?|conflicts?\s+of\s+interest)\b',
                clean,
                re.I,
            )
            or re.match(r'^\[[^\]:]+:P1\]$', ptag)
        ):
            in_refs = False
        if not in_refs:
            body_lines.append((ptag or 'body', clean))

    print("== 1. CONSECUTIVE DUPLICATE WORDS & GARBLED DUPLICATED CLAUSES ==")
    dup_found = False
    for loc, clean in body_lines:
        # 1a. Immediate duplicate word: "images images", "the the"
        for m in re.finditer(r'\b([A-Za-z]{2,})\s+\1\b', clean, re.I):
            toks = m.group(0).split()
            if len(toks) == 2 and (toks[0].isupper() != toks[1].isupper()):
                continue
            print(f"  !! {loc}: repeated word '{m.group(0)}' in: ...{clean[max(0, m.start()-25):m.end()+35]}...")
            dup_found = True

        # 1b. Duplicated 2-to-3-word phrase within the SAME unpunctuated clause
        if not (loc.startswith(('[T', '[Row')) or 'Row ' in loc):
            clauses = re.split(r'[.?!,;:()\[\]]|\b(?:and|or|whereas|while|versus|vs|than|against|between|from|to)\b', clean, flags=re.I)
            for clause in clauses:
                raw_words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9-]*\b', clause)
                words = [w.lower() for w in raw_words if len(w) >= 2]
                if len(words) < 5:
                    continue
                for n_len in (3, 2):
                    seen_ngrams = {}
                    found_in_clause = False
                    for k in range(len(words) - n_len + 1):
                        ng = tuple(words[k:k + n_len])
                        if all(len(w) <= 3 or w in ('that', 'this', 'with', 'from', 'were', 'have', 'been', 'the', 'for', 'are') for w in ng):
                            continue
                        if any('-' in w for w in ng):
                            continue
                        if ng in seen_ngrams and (k - seen_ngrams[ng]) <= 6:
                            prev_k = seen_ngrams[ng]
                            raw_ng1 = raw_words[prev_k:prev_k + n_len]
                            raw_ng2 = raw_words[k:k + n_len]
                            if any(w[0].isupper() for w in raw_ng1) != any(w[0].isupper() for w in raw_ng2):
                                continue
                            phrase_str = ' '.join(ng)
                            print(f"  !! {loc}: garbled/repeated phrase '{phrase_str}' within single clause: \"{clause.strip()[:130]}\"")
                            dup_found = True
                            found_in_clause = True
                            break
                        seen_ngrams[ng] = k
                    if found_in_clause:
                        break
    if not dup_found:
        print("  None detected.")

    print("\n== 2. NOMENCLATURE, HOMOPHONES & NEAR-MISS TYPO AUDIT ==")
    # 2a. Mutant tokens (Δ or ∆)
    delta_chars = Counter()
    mutants = defaultdict(list)
    for loc, clean in body_lines:
        for m in re.finditer(r'([\u0394\u2206])([A-Za-z0-9_-]+)', clean):
            delta_chars[m.group(1)] += 1
            mutants[m.group(2)].append(loc)
    if len(delta_chars) > 1:
        print(f"  [Note] Mixed Unicode delta characters used for mutants: "
              f"U+0394 Greek Delta 'Δ' ({delta_chars['\u0394']}x) vs U+2206 Math Increment '∆' ({delta_chars['\u2206']}x)")
    mut_names = sorted(mutants.keys())
    for i in range(len(mut_names)):
        for j in range(i + 1, len(mut_names)):
            m1, m2 = mut_names[i], mut_names[j]
            if set(mutants[m1]) & set(mutants[m2]):
                continue
            if edit_distance_le1(m1, m2):
                print(f"  !! Near-miss mutant names: 'Δ{m1}' ({len(mutants[m1])}x at {mutants[m1][:5]}) "
                      f"vs 'Δ{m2}' ({len(mutants[m2])}x at {mutants[m2][:5]})")

    # 2b. Compound / strain / construct codes, table-vs-prose inventory cross-check, hyphenated variants, and transposed acronyms
    codes = defaultdict(list)
    table_codes_by_prefix = defaultdict(set)
    prose_codes_by_prefix = defaultdict(lambda: defaultdict(list))
    hyph_tokens = defaultdict(list)
    all_raw_tokens = defaultdict(list)
    short_alnum_tokens = defaultdict(list)
    for loc, clean in body_lines:
        is_tbl_loc = (loc.startswith(('[T', '[Row')) or 'Row ' in loc or '|' in clean)
        for m in re.finditer(r'\b([A-Z]{2,5}-?\d{2,6}[A-Za-z]?|p[A-Z]{2,5}\d{1,5}[A-Za-z0-9_-]*|P[a-z]{3,4}[A-Z]-[A-Za-z0-9_-]+)\b', clean):
            c_tok = m.group(1)
            codes[c_tok].append(loc)
            m_pfx = re.match(r'^([A-Za-z]+)-?(\d+)$', c_tok)
            if m_pfx:
                pfx = m_pfx.group(1)
                if is_tbl_loc:
                    table_codes_by_prefix[pfx].add(c_tok.replace('-', ''))
                else:
                    prose_codes_by_prefix[pfx][c_tok].append(loc)
        for m_h in re.finditer(r'\b([A-Za-z0-9]{2,}-[A-Za-z0-9]{1,}(?:-[A-Za-z0-9]{1,})?)\b', clean):
            ht = m_h.group(1)
            if not ht[0].isdigit() and any(c.isupper() for c in ht):
                hyph_tokens[ht].append(loc)
        for m_t in re.finditer(r'\b([A-Za-z0-9]{3,})\b', clean):
            tok = m_t.group(1)
            if any(c.isupper() for c in tok):
                all_raw_tokens[tok].append(loc)
        # Also collect 2-to-3 char letter+digit domain/construct tokens (e.g. N3 vs 3N)
        for m_s in re.finditer(r'\b([A-Z]\d|\d[A-Z])\b', clean):
            short_alnum_tokens[m_s.group(1)].append(loc)

    # Check if any prose strain/plasmid code with a table-inventoried prefix (>=5 table entries) is missing from the table
    for pfx, tbl_set in sorted(table_codes_by_prefix.items()):
        if len(tbl_set) >= 5 and pfx in prose_codes_by_prefix:
            for c_tok, plocs in sorted(prose_codes_by_prefix[pfx].items()):
                if c_tok.replace('-', '') not in tbl_set:
                    print(
                        f"  !! PROSE STRAIN/CONSTRUCT ID MISSING FROM TABLE INVENTORY: '{c_tok}' cited in prose at {plocs[:5]} "
                        f"does not appear anywhere in the {pfx}* table inventory ({len(tbl_set)} defined IDs, e.g. {sorted(tbl_set)[:5]}..{sorted(tbl_set)[-2:]})!"
                    )

    # Compare codes only when they are NOT merely different serial numbers of the same alphabetic prefix (suppresses O(N^2) serial strain-table noise)
    code_names = sorted(codes.keys())
    for i in range(len(code_names)):
        for j in range(i + 1, len(code_names)):
            c1, c2 = code_names[i], code_names[j]
            m1 = re.match(r'^([A-Za-z]+)(-?)(\d+[A-Za-z]?)$', c1)
            m2 = re.match(r'^([A-Za-z]+)(-?)(\d+[A-Za-z]?)$', c2)
            if m1 and m2:
                p1, h1, n1 = m1.groups()
                p2, h2, n2 = m2.groups()
                # If same prefix and different numbers, they are distinct serial numbers in a collection — skip!
                if p1 == p2 and n1 != n2:
                    continue
                # Flag if same number with hyphen mismatch (e.g. AB-001 vs AB001) or transposed prefix with same number
                if n1 == n2 and (p1 == p2 or edit_distance_le1(p1, p2)):
                    print(f"  !! Near-miss compound/strain/construct codes: '{c1}' ({len(codes[c1])}x at {codes[c1][:5]}) "
                          f"vs '{c2}' ({len(codes[c2])}x at {codes[c2][:5]})")
                continue
            if (c1.startswith('P') and '-' in c1 and '-' in c2 and c1.split('-')[1] == c2.split('-')[-1]) and edit_distance_le2(c1, c2):
                print(f"  !! Near-miss promoter/construct codes: '{c1}' ({len(codes[c1])}x at {codes[c1][:5]}) "
                      f"vs '{c2}' ({len(codes[c2])}x at {codes[c2][:5]})")

    reported_hyph = set()
    for ht, hlocs in sorted(hyph_tokens.items()):
        no_hyph = ht.replace('-', '')
        if no_hyph in all_raw_tokens and no_hyph not in reported_hyph:
            reported_hyph.add(no_hyph)
            u_locs = all_raw_tokens[no_hyph]
            print(f"  !! Hyphenated vs unhyphenated nomenclature mix: '{ht}' ({len(hlocs)}x, e.g. {hlocs[:3]}) "
                  f"vs '{no_hyph}' ({len(u_locs)}x at {u_locs[:4]})")

    mixed_toks = sorted(all_raw_tokens.keys())
    for i in range(len(mixed_toks)):
        for j in range(i + 1, len(mixed_toks)):
            t1, t2 = mixed_toks[i], mixed_toks[j]
            if re.match(r'^[A-Z][a-z]+$', t1) and re.match(r'^[A-Z][a-z]+$', t2):
                continue
            if len(t1) == len(t2) and len(t1) >= 3 and sorted(t1) == sorted(t2) and edit_distance_le1(t1, t2):
                if len(all_raw_tokens[t1]) == 1 or len(all_raw_tokens[t2]) == 1:
                    rare_t, freq_t = (t1, t2) if len(all_raw_tokens[t1]) <= len(all_raw_tokens[t2]) else (t2, t1)
                    print(f"  !! Suspected transposed acronym/code typo at {all_raw_tokens[rare_t][0]}: '{rare_t}' ({len(all_raw_tokens[rare_t])}x) "
                          f"vs '{freq_t}' ({len(all_raw_tokens[freq_t])}x at {all_raw_tokens[freq_t][:3]})")

    # Check 2-char transposed letter+digit tokens like '3N' (1x) vs 'N3' (>=2x)
    for st1, slocs1 in sorted(short_alnum_tokens.items()):
        rev_st = st1[::-1]
        if rev_st in short_alnum_tokens and st1 < rev_st:
            slocs2 = short_alnum_tokens[rev_st]
            if len(slocs1) == 1 or len(slocs2) == 1:
                rare_s, freq_s = (st1, rev_st) if len(slocs1) <= len(slocs2) else (rev_st, st1)
                print(f"  !! Suspected transposed short domain/code token at {short_alnum_tokens[rare_s][0]}: '{rare_s}' ({len(short_alnum_tokens[rare_s])}x) "
                      f"vs '{freq_s}' ({len(short_alnum_tokens[freq_s])}x at {short_alnum_tokens[freq_s][:4]})")

    # 2c. Taxonomic names before 'spp.' or 'sp.'
    genera = defaultdict(list)
    for loc, clean in body_lines:
        for m in re.finditer(r'\b([A-Z][a-z]{3,})\s+spp?\.', clean):
            g = m.group(1)
            genera[g].append(loc)
            if g.lower() == 'candidatus':
                print(f"  !! {loc}: Taxonomic rank misuse '{m.group(0)}' ('Candidatus' is a provisional status prefix, not a genus name)")
    gen_names = sorted(genera.keys())
    for i in range(len(gen_names)):
        for j in range(i + 1, len(gen_names)):
            g1, g2 = gen_names[i], gen_names[j]
            if edit_distance_le2(g1.lower(), g2.lower()):
                print(f"  !! Near-miss genus names: '{g1} spp.' ({genera[g1]}) vs '{g2} spp.' ({genera[g2]})")

    # 2d. Context-sensitive scientific homophones & autocorrect traps
    for loc, clean in body_lines:
        for pat, msg_tmpl in SCIENTIFIC_HOMOPHONE_PATTERNS:
            for m in pat.finditer(clean):
                g2 = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
                print(f"  !! {loc}: {msg_tmpl.format(match=m.group(0), g2=g2)}")

    # 2e. Bigram & singleton near-miss typo detector (with UK/US spelling mix separation)
    bigram_counts = Counter()
    bigram_locs = defaultdict(list)
    by_second_word = defaultdict(set)
    word_counts = Counter()
    word_locs = defaultdict(list)
    for loc, clean in body_lines:
        words = [w.lower() for w in re.findall(r'\b[A-Za-z][a-z]{4,}\b', clean)]
        for w in words:
            word_counts[w] += 1
            if len(word_locs[w]) < 3:
                word_locs[w].append(loc)
        for k in range(len(words) - 1):
            bg = (words[k], words[k + 1])
            bigram_counts[bg] += 1
            bigram_locs[bg].append(loc)
            by_second_word[words[k + 1]].add(words[k])

    reported_typo_words = set()
    uk_us_pairs = []
    for (w1, w2), cnt in sorted(bigram_counts.items()):
        if w1 in ignore_set or w2 in ignore_set:
            continue
        if cnt == 1 and w1 not in COMMON_STOP_FIRST_WORDS and w2 not in COMMON_STOP_FIRST_WORDS and w2 not in ('cells', 'cell'):
            for alt_w1 in by_second_word[w2]:
                if bigram_counts[(alt_w1, w2)] >= 5 and w1[:2] == alt_w1[:2] and edit_distance_le1(w1, alt_w1):
                    if _normalize_uk_us(w1) == _normalize_uk_us(alt_w1):
                        uk_us_pairs.append((w1, word_counts[w1], alt_w1, word_counts[alt_w1]))
                        continue
                    if is_antonym_pair(w1, alt_w1) or _is_inflection_pair(w1, alt_w1):
                        continue
                    reported_typo_words.add(w1)
                    print(f"  !! Suspected typo in bigram {bigram_locs[(w1, w2)][0]}: '{w1} {w2}' (1x) "
                          f"vs frequent '{alt_w1} {w2}' ({bigram_counts[(alt_w1, w2)]}x)")

    frequent_words = [w for w, c in word_counts.items() if (c >= 4 and len(w) >= 7) or (c >= 2 and len(w) >= 9)]
    for w, cnt in sorted(word_counts.items()):
        if cnt == 1 and len(w) >= 7 and w not in ignore_set and w not in reported_typo_words:
            for fw in frequent_words:
                if _normalize_uk_us(w) == _normalize_uk_us(fw):
                    uk_us_pairs.append((w, cnt, fw, word_counts[fw]))
                    break
                if (w[:3] == fw[:3] or (w[0] == fw[0] and w[-3:] == fw[-3:])) and edit_distance_le1(w, fw) and not is_antonym_pair(w, fw) and not _is_inflection_pair(w, fw):
                    print(f"  !! Suspected singleton spelling typo at {word_locs[w][0]}: '{w}' (1x in document) "
                          f"vs '{fw}' ({word_counts[fw]}x in document)")
                    break

    if uk_us_pairs:
        seen_uk = set()
        uk_summary = []
        for w_a, c_a, w_b, c_b in uk_us_pairs:
            pair_k = tuple(sorted((w_a, w_b)))
            if pair_k not in seen_uk:
                seen_uk.add(pair_k)
                uk_summary.append(f"'{w_a}' ({c_a}x) vs '{w_b}' ({c_b}x)")
        print(f"  [UK/US Spelling Mix] Mixed British/American spelling variants detected: {', '.join(uk_summary[:6])}")

    # 2f. Editorial placeholders & bare numeric repository accessions missing prefixes
    placeholder_pat = re.compile(
        r'(\(\s*if\s+this\s+paper[^)]*\)|\[\s*(?:insert|ref|citation|todo|tbd)[^\]]*\]|\b(?:TODO|FIXME|TBD|XXX)\b|\?\?\+)',
        re.I,
    )
    bare_acc_pat = re.compile(
        r'\b(GEO|Gene\s+Expression\s+Omnibus|BioProject|SRA|Sequence\s+Read\s+Archive|ProteomeXchange|ArrayExpress)\b'
        r'[^.]{0,55}?\b(?:accession(?:\s+number)?|number|id|under|:)\s+(\d{5,8})\b',
        re.I,
    )
    for loc, clean in body_lines:
        for m_ph in placeholder_pat.finditer(clean):
            print(f"  !! {loc}: Editorial / draft placeholder left in text: '{m_ph.group(0)}'")
        for m_ac in bare_acc_pat.finditer(clean):
            repo, num = m_ac.group(1), m_ac.group(2)
            expected_pfx = "GSE/GSM/GDS" if "geo" in repo.lower() or "omnibus" in repo.lower() else "PRJNA/PXD/E-MTAB"
            print(f"  !! {loc}: Bare numeric repository accession '{num}' after '{repo}' (missing official prefix such as {expected_pfx}{num})")

    print("\n== 3. UNIT, TIME ABBREVIATION & SPACING HYGIENE ==")
    unit_families = {
        "milliliter (mL vs ml)": [r'\b\d+(?:\.\d+)?\s*mL\b', r'\b\d+(?:\.\d+)?\s*ml\b'],
        "units per milliliter (U/mL vs U/ml)": [r'\b\d+(?:\.\d+)?\s*U/mL\b', r'\b\d+(?:\.\d+)?\s*U/ml\b'],
        "microliter (µL vs µl vs uL)": [r'\b\d+(?:\.\d+)?\s*[\u00b5\u03bc]L\b', r'\b\d+(?:\.\d+)?\s*[\u00b5\u03bc]l\b', r'\b\d+(?:\.\d+)?\s*u[Ll]\b'],
        "microgram (µg vs ug)": [r'\b\d+(?:\.\d+)?\s*[\u00b5\u03bc]g\b', r'\b\d+(?:\.\d+)?\s*ug\b'],
        "micrometer (µm vs um)": [r'\b\d+(?:\.\d+)?\s*[\u00b5\u03bc]m\b', r'\b\d+(?:\.\d+)?\s*um\b'],
        "seconds (s vs sec)": [r'\b\d+(?:\.\d+)?\s*s\b', r'\b\d+(?:\.\d+)?\s*sec(?:onds?)?\b'],
        "hours (spaced 'X h' vs attached 'Xh' vs 'hr/hrs')": [r'\b\d+(?:\.\d+)?\s+h\b', r'\b\d+h\b', r'\b\d+(?:\.\d+)?\s*hrs?\b'],
        "minutes (spaced 'X min' vs attached 'Xmin')": [r'\b\d+(?:\.\d+)?\s+min\b', r'\b\d+min\b'],
    }
    any_unit_mix = False
    full_body_str = '\n'.join(f"{l}: {c}" for l, c in body_lines)
    for fam_name, pats in unit_families.items():
        found_variants = {}
        for p in pats:
            matches = re.findall(p, full_body_str)
            if matches:
                sample = matches[0].strip()
                found_variants[sample] = len(matches)
        if len(found_variants) > 1:
            any_unit_mix = True
            summary = ", ".join(f"'{k}' ({v}x)" for k, v in found_variants.items())
            print(f"  !! Mixed {fam_name}: {summary}")
    for loc, clean in body_lines:
        for m_ct in re.finditer(r'\b\d+\s*min\s*\d+\s*s(?:ec)?\b', clean, re.I):
            if not re.match(r'^\d+\s+min\s+\d+\s+s(?:ec)?$', m_ct.group(0), re.I):
                any_unit_mix = True
                print(f"  !! {loc}: Unspaced compound time expression '{m_ct.group(0)}'")
        for m_deg in re.finditer(r'(?:^|(?<=\s)|(?<=-))\d+\s*oC\b', clean):
            any_unit_mix = True
            print(f"  !! {loc}: ASCII letter 'o' used instead of degree symbol '°C' in '{m_deg.group(0)}'")
        # Check table rows for European comma decimal separators mixed with period decimals (e.g. '4,3' vs '4.3')
        if loc.startswith(('[T', '[Row')) or 'Row ' in loc:
            for m_cdec in re.finditer(r'(?<![\d,])\b(\d{1,2},\d{1,2})\b(?![\d,])', clean):
                # Exclude integer lists like "1, 2" (space after comma is already excluded) or citation brackets
                pre_c = clean[max(0, m_cdec.start() - 5):m_cdec.start()]
                post_c = clean[m_cdec.end():m_cdec.end() + 5]
                if '[' not in pre_c and ']' not in post_c and '(' not in pre_c:
                    any_unit_mix = True
                    print(f"  !! {loc}: Comma decimal separator '{m_cdec.group(1)}' in table cell (verify whether '{m_cdec.group(1).replace(',', '.')}' was intended): \"{clean[:110]}\"")
        if not (loc.startswith(('[T', '[Row')) or 'Row ' in loc):
            # Only strip list markers '1)' or 'a)' at the start of a line or after '; ' / ': ' so '(see Box 1)' is never corrupted
            cleaned_parens = re.sub(r'(?:^\s*|\b(?<=[;:]\s))[0-9a-zA-Z]\)(?=\s)', '', clean)
            n_open = cleaned_parens.count('(')
            n_close = cleaned_parens.count(')')
            if n_open != n_close:
                any_unit_mix = True
                print(f"  !! {loc}: Unbalanced parentheses ({n_open} '(' vs {n_close} ')') in: \"{clean[:135]}...\"")
    if not any_unit_mix:
        print("  Unit abbreviations and spacing are uniform.")

    print("\n== 4. QUANTITATIVE, STATISTICAL & CROSS-SECTION/TABLE PARAMETER CONSISTENCY ==")
    # 4a. Same-sentence dilution / volume-percentage arithmetic check
    for loc, clean in body_lines:
        if loc.startswith(('[T', '[Row')) or 'Row ' in loc:
            continue
        for sent in re.split(r'(?<=[.?!])\s+', clean):
            vols = re.findall(r'(\d+(?:\.\d+)?)\s*(?:\u00b5|u|\u03bc)[Ll]\b', sent)
            pcts = re.findall(r'(\d+(?:\.\d+)?)\s*%', sent)
            if len(vols) >= 2 and pcts:
                print(f"  [Recipe check {loc}]: {sent.strip()}")
                v_floats = [float(v) for v in vols]
                total_v = max(v_floats)
                for v in v_floats:
                    if v < total_v:
                        calc_pct = (v / total_v) * 100.0
                        print(f"     -> Calculated: {v} µL / {total_v} µL = {calc_pct:.4g}% v/v (stated % in sentence: {pcts}%)")

    # 4b. Cross-table decimal value & unit anchor check (prevents false edits on table-backed numbers like '2.85 S' and flags prose-vs-table mismatches)
    table_decimals = defaultdict(list)
    for loc, clean in body_lines:
        if loc.startswith(('[T', '[Row')) or 'Row ' in loc:
            for m_d in re.finditer(r'\b(\d+\.\d+)\b', clean):
                val_s = m_d.group(1)
                if len(table_decimals[val_s]) < 3:
                    table_decimals[val_s].append((loc, clean[:120]))
    if table_decimals:
        anchored_notes = []
        for loc, clean in body_lines:
            if loc.startswith(('[T', '[Row')) or 'Row ' in loc:
                continue
            for m_pd in re.finditer(r'\b(\d+\.\d+)\s*([A-Za-z%\u00b5\u03bc/]+)?', clean):
                val_s = m_pd.group(1)
                unit_s = (m_pd.group(2) or '').strip()
                if val_s in table_decimals:
                    ctx_s = clean[max(0, m_pd.start() - 25):min(len(clean), m_pd.end() + 35)].strip()
                    t_loc, t_row = table_decimals[val_s][0]
                    anchored_notes.append(f"    - Prose {loc} '{val_s} {unit_s}' (\"...{ctx_s}...\") <-> Table {t_loc}: \"{t_row}\"")
        if anchored_notes:
            print("  [Table-Anchored Decimal Values in Prose (verify unit/column alignment before editing!)]:")
            for an in anchored_notes[:15]:
                print(an)

    # 4c. Statistical test vs replicate count (n = 2 pseudoreplication), hybrid test contradictions & boxplot IQR definition check
    stat_anom_pat_hybrid = re.compile(
        r'(?=.*\b(?:mann-whitney|wilcoxon|kruskal-wallis)\b)(?=.*\b(?:student|t-test|anova|tukey|dunnett)\b)',
        re.I,
    )
    iqr_75_pat = re.compile(r'\b75\s*%\s*(?:interquartile\s+range|iqr)\b|\b25\s*%\s*(?:and|to|\u2013|-)\s*75\s*%\s*(?:interquartile\s+range|iqr)\b', re.I)
    or_menu_pat = re.compile(r'\b(?:stained|labeled|treated|analyzed|fitted|quantified)\s+(?:with|using|by)\s+[^.;]{3,40}\s+or\s+[^.;]{3,30}(?:respectively)?\.', re.I)
    for loc, clean in body_lines:
        if re.search(r'\bn\s*=\s*2\b|\btwo\s+(?:biological|independent)\s+(?:replicates?|experiments?)\b', clean, re.I):
            if re.search(r'\b(?:t-test|student|anova|tukey|dunnett|sidak|\u0161id\u00e1k|mann-whitney|wilcoxon|p\s*[<=>]\s*0\.\d+)\b', clean, re.I):
                print(f"  !! [STATISTICAL / REPLICATE ALERT {loc}]: n = 2 replicates paired with inferential statistical test in: \"{clean[:150]}...\"")
        for sent in re.split(r'(?<=[.?!])\s+', clean):
            if stat_anom_pat_hybrid.search(sent):
                print(f"  !! [HYBRID PARAMETRIC/NON-PARAMETRIC STAT TEST DESCRIPTION {loc}]: \"{sent.strip()[:160]}\"")
            if iqr_75_pat.search(sent):
                print(f"  !! [BOXPLOT IQR DEFINITION ERROR {loc}]: '{iqr_75_pat.search(sent).group(0)}' (the interquartile range spans 50% of data between the 25th and 75th percentiles, not 75% IQR)")
            if or_menu_pat.search(sent):
                print(f"  [Ambiguous 'or'-menu protocol phrasing {loc}]: \"{sent.strip()[:150]}\" (verify whether specific figures/assays are mapped to each option)")

    # 4d. Cross-section threshold / cutoff & incubation parameter inventory
    thresholds = []
    dmso_pcts = []
    time_windows = []
    mois = []
    n_vals = []
    has_exp_section = False
    for loc, clean in body_lines:
        if re.match(r'^\s*(?:\d+\.?\s+)?(?:materials?\s+and\s+methods?|methods?|results?|experimental\s+procedures?)\s*$', clean, re.I):
            has_exp_section = True
        for m_th in re.finditer(r'(?:[<>\u2264\u2265]=?\s*0\.\d{2,4}|\b(?:threshold|cutoff|cut-off|ratio|score)\s+(?:of\s+)?[<>\u2264\u2265]?\s*\d+\.\d+)', clean, re.I):
            thresholds.append((loc, m_th.group(0), clean[max(0, m_th.start() - 25):min(len(clean), m_th.end() + 25)].strip()))
        for m in re.finditer(r'(\d+(?:\.\d+)?%\s*(?:of\s+)?DMSO)', clean, re.I):
            dmso_pcts.append((loc, m.group(1)))
        for m in re.finditer(r'(\b(?:between|from|at)\s+\d+(?::\d+|\.\d+)?\s*(?:to|and|\u2013|-)\s*\d+(?::\d+|\.\d+)?\s*hpi\b|\bbetween\s+\d+:\d+\s*hpi\b)', clean, re.I):
            time_windows.append((loc, m.group(1)))
        for m in re.finditer(r'\bMOI\s+(?:of\s+)?\d+(?:\s*(?:to|\u2013|-)\s*\d+)?', clean, re.I):
            mois.append((loc, m.group(0)))
        for m in re.finditer(r'\bn\s*=\s*\d+|\b\d+\s+(?:biological|independent)\s+replicates\b', clean, re.I):
            n_vals.append((loc, m.group(0)))

    if thresholds:
        print("  Stated numerical thresholds / cutoffs across text (check for precision drift like >0.55 vs >0.555):")
        for loc, th_v, th_ctx in thresholds[:15]:
            print(f"    - {loc}: '{th_v}' in \"...{th_ctx}...\"")
    if dmso_pcts:
        print(f"  Stated DMSO concentrations across text: {dmso_pcts}")
    if time_windows:
        print("  Stated hpi time windows across text:")
        for loc, tw in time_windows:
            print(f"    - {loc}: {tw}")
    if mois:
        print(f"  Stated MOIs across text: {mois}")
    if n_vals:
        print(f"  Stated replicate counts (n) across text: {n_vals}")
    elif has_exp_section:
        print("  !! WARNING: No explicit sample sizes ('n = ...' or 'X biological/independent replicates') detected anywhere in the text!")
    else:
        print("  (Review/non-experimental article structure — no 'n = ...' replicate counts expected.)")

    print("\n== 5. MANDATORY JOURNAL DECLARATION SECTIONS & AUTHORSHIP BYLINE CHECK ==")
    all_raw_lower = '\n'.join(raw_lines).lower()
    declarations = [
        ("Acknowledgments / Funding", r'\b(?:acknowledg(?:e)?ments?|funding)\b'),
        ("Author Contributions", r'\b(?:author\s+contributions?|credit\s+authorship)\b'),
        ("Competing Interests / Conflict of Interest", r'\b(?:competing\s+(?:financial\s+)?interests?|conflicts?\s+of\s+interest|declaration\s+of\s+interests?)\b'),
        ("Data / Code Availability", r'\b(?:data\s+(?:and\s+code\s+)?availability|resource\s+availability|availability\s+of\s+data)\b'),
    ]
    missing_decls = [label for label, pat in declarations if not re.search(pat, all_raw_lower)]
    if missing_decls:
        print(f"  [Note] Missing standard end-matter declaration sections: {', '.join(missing_decls)}")
    else:
        print("  All standard declaration sections (Acknowledgments/Funding, Author Contributions, Competing Interests, Data Availability) are present.")

    # Title-Page Author Byline vs Author Contributions Bidirectional Coverage Check
    byline_authors = []
    for loc, clean in body_lines[:6]:
        if re.match(r'^\[P[234]\]$', loc) and (',' in clean or ' and ' in clean):
            if any(k in clean.lower() for k in ('university', 'institute', 'department', 'correspondence', 'address', 'abstract', 'keywords', 'equal contribution')):
                continue
            parts = re.split(r'\s*,\s*|\s+and\s+', clean)
            for p in parts:
                p_clean = re.sub(r'[\d*#\u2020\u2021]+$', '', p.strip()).strip()
                words_p = [w for w in p_clean.split() if re.match(r'^[A-Z]', w)]
                if 2 <= len(words_p) <= 4:
                    surname = words_p[-1]
                    all_let_parts = [sub_w[0].upper() for wp in words_p for sub_w in wp.split('-') if sub_w]
                    init_full = ''.join(all_let_parts)
                    init_first_last = words_p[0][0].upper() + words_p[-1].split('-')[0][0].upper()
                    init_dotted = '.'.join(all_let_parts) + '.'
                    byline_authors.append((p_clean, {init_full, init_first_last, init_dotted}, surname, loc))
            if byline_authors:
                break

    in_contrib = False
    contrib_text = ""
    contrib_locs = []
    for loc, clean in body_lines:
        if re.match(r'^\s*(?:author\s+contributions?|credit\s+authorship)\b', clean, re.I):
            in_contrib = True
            contrib_locs.append(loc)
            contrib_text += " " + clean
            continue
        if in_contrib:
            if re.match(r'^\s*(?:competing\s+interests?|disclosure|conflict|acknowledg|funding|references|data\s+availability)\b', clean, re.I):
                in_contrib = False
            else:
                contrib_locs.append(loc)
                contrib_text += " " + clean

    if byline_authors and contrib_text.strip():
        for full_name, init_set, surname, bloc in byline_authors:
            found_in_contrib = bool(re.search(rf'\b{re.escape(surname)}\b', contrib_text, re.I))
            if not found_in_contrib:
                for iv in init_set:
                    if (iv.endswith('.') and iv in contrib_text) or (not iv.endswith('.') and re.search(rf'\b{re.escape(iv)}\b', contrib_text)):
                        found_in_contrib = True
                        break
            if not found_in_contrib:
                print(
                    f"  !! [MISSING AUTHOR IN AUTHOR CONTRIBUTIONS] Byline author '{full_name}' ({bloc}, initials {sorted(init_set)}) "
                    f"was not found in the Author Contributions section ({contrib_locs[0]})!"
                )

    # 5b. Conditional Submission-Readiness & Minimal Reporting Trigger Checks (scan manuscript prose/Methods, excluding References and antibody table rows)
    print("\n  5b. Conditional Submission-Readiness & Minimal Reporting Trigger Checks:")
    body_only_lower = "\n".join(clean for loc, clean in body_lines if not loc.startswith(('[T', '[Row'))).lower()
    # Exclude "rabbit anti-...", "mouse anti-...", "anti-mouse", "anti-rabbit" antibody reagent phrases from live animal triggers
    body_no_ab = re.sub(
        r'\b(?:(?:rabbit|mouse|mice|rat|goat|donkey|sheep|guinea\s+pig)\s+(?:anti-|polyclonal|monoclonal|igg|ig[gm]\b|sera|serum|secondary|primary)|anti-(?:mouse|rabbit|rat|goat|sheep|guinea\s+pig))',
        ' ',
        body_only_lower,
    )
    trigger_notes = []
    animal_hits = re.findall(r'\b(?:mice|mouse|murine|rats|zebrafish|guinea\s+pigs|xenograft|animal\s+experiments?|animal\s+models?|in\s+vivo\s+infection|balb/c|c57bl/6)\b', body_no_ab)
    if len(animal_hits) >= 2 and has_exp_section:
        if not re.search(r'\b(?:iacuc|ethics|ethical|institutional\s+animal|animal\s+welfare|animal\s+care\s+and\s+use|approval|approved\s+by|directive\s+2010)\b', all_raw_lower):
            trigger_notes.append("  !! [SUBMISSION TRIGGER — ANIMAL ETHICS]: Vertebrate animal terms detected in experimental manuscript, but no IACUC / Animal Welfare ethics approval statement was found.")
    human_hits = re.findall(r'\b(?:patients|clinical\s+samples?|human\s+donors?|healthy\s+volunteers|biopsies|patient\s+cohort)\b', body_only_lower)
    if len(human_hits) >= 2 and has_exp_section:
        if not re.search(r'\b(?:irb|informed\s+consent|ethics\s+committee|helsinki|institutional\s+review\s+board|ethical\s+approval)\b', all_raw_lower):
            trigger_notes.append("  !! [SUBMISSION TRIGGER — HUMAN SUBJECTS / IRB]: Human/clinical sample terms detected, but no IRB / Ethics Committee / Informed Consent statement was found.")
    cell_line_hits = set(re.findall(r'\b(?:hela|hek\s*293t?|a549|u2os|mcf-?7|mda-mb|nih-?3t3|cho-?k1|jurkat|raw\s*264\.7|thp-?1|vero|caco-?2|sh-sy5y|ht-?29|bhk-?21|cos-?7|l929)\b', body_only_lower))
    if cell_line_hits and has_exp_section:
        if not re.search(r'\bmycoplasma\b', all_raw_lower):
            trigger_notes.append(f"  [Submission Trigger — Cell Line Reporting]: Mammalian cell line(s) {sorted(cell_line_hits)} detected, but no 'mycoplasma' testing statement was found in Methods.")
    omics_struct_hits = set(re.findall(r'\b(?:rna-seq|scrna-seq|chip-seq|atac-seq|whole-genome\s+sequencing|lc-ms/ms|proteomics|shotgun\s+mass\s+spectrometry|cryo-em|x-ray\s+crystallography|crystal\s+structure)\b', body_only_lower))
    if omics_struct_hits and has_exp_section:
        has_repo_acc = bool(re.search(r'\b(?:gse\d{4,}|prjna\d{4,}|prjeb\d{4,}|erp\d{4,}|srp\d{4,}|pxd\d{5,}|msv\d{5,}|emd-\d{4,}|pdb\s*(?:id|code|entry)?\s*[:=]?\s*[0-9][a-z0-9]{3}|10\.5281/zenodo)\b', all_raw_lower))
        has_upon_req = bool(re.search(r'\bavailable\s+(?:from\s+the\s+corresponding\s+author\s+)?upon\s+(?:reasonable\s+)?request\b', all_raw_lower))
        if not has_repo_acc:
            extra_req = " (currently states 'available upon request')" if has_upon_req else ""
            trigger_notes.append(f"  !! [SUBMISSION TRIGGER — DATA DEPOSITION]: High-throughput/structural assay(s) {sorted(omics_struct_hits)} detected, but no standard public repository accession (GEO/SRA/PRIDE/PDB/EMDB/Zenodo) was found{extra_req}.")
    # Software version check
    sw_pat = re.compile(r'\b(GraphPad\s+Prism|ImageJ|Fiji|FlowJo|MaxQuant|Perseus|DESeq2|Seurat|ChimeraX|PyMOL|RELION|CryoSPARC|SPSS|MATLAB|OriginPro)\b', re.I)
    sw_no_ver = set()
    sw_with_ver = set()
    for _, clean in body_lines:
        for m_sw in sw_pat.finditer(clean):
            sw_name = m_sw.group(1)
            win = clean[max(0, m_sw.start() - 15):min(len(clean), m_sw.end() + 45)]
            if re.search(r'\b(?:v\.?\s*\d|version\s*\d|\d+\.\d+|20[12]\d[a-z]?)\b', win, re.I):
                sw_with_ver.add(sw_name.lower())
            else:
                sw_no_ver.add(sw_name)
    missing_ver = sorted(s for s in sw_no_ver if s.lower() not in sw_with_ver)
    if missing_ver and has_exp_section:
        trigger_notes.append(f"  [Submission Trigger — Software Versions]: Software mentioned without version number: {', '.join(missing_ver)}")
    if trigger_notes:
        for tn in trigger_notes:
            print(tn)
    else:
        print("    All conditional submission-readiness checks passed.")

    if reb_path and os.path.isfile(reb_path):
        print("\n== 6. REBUTTAL LETTER <-> MANUSCRIPT CROSS-AUDIT ==")
        reb_lines = open(reb_path, encoding='utf-8').read().splitlines()

        print("  6a. Line-number references in Rebuttal:")
        if empty_spacer_count > 0:
            print(f"    [Line-Drift Info] Manuscript has {empty_spacer_count} unsuppressed empty spacer paragraph(s) that shift Word line numbers relative to non-empty text lines.")
        for i, rl in enumerate(reb_lines, 1):
            ptag, clean = strip_ptag(rl)
            loc = ptag or f"Reb-L{i}"
            for m in re.finditer(r'\blines?\s+(\d+)(?:\s*[\u2013\u2014-]\s*(\d+))?', clean, re.I):
                l_start = int(m.group(1))
                l_end = int(m.group(2)) if m.group(2) else l_start
                print(f"    - {loc}: cites '{m.group(0)}' -> context: ...{clean[max(0, m.start()-30):m.end()+60]}...")
                if fmt_rows:
                    ne_hits = [f"[{pid}] ({prev[:55]}...)" for pid, ns, ne, ws, we, prev in fmt_rows if ns <= l_start <= ne or ns <= l_end <= ne]
                    wd_hits = [f"[{pid}] (drift +{ws-ns} empty lines: {prev[:55]}...)" for pid, ns, ne, ws, we, prev in fmt_rows if ws <= l_start <= we or ws <= l_end <= we]
                    if ne_hits:
                        print(f"        Non-empty L{l_start}..{l_end} maps to : {', '.join(ne_hits[:2])}")
                    if wd_hits:
                        print(f"        Word-with-empty L{l_start}..{l_end} maps to: {', '.join(wd_hits[:2])}")

        print("\n  6b. Future-tense / conditional AND past-tense revision commitments in Rebuttal (verify if fulfilled in Manuscript/Figures):")
        draft_pat = re.compile(
            r'\b(?:will\s+(?:change|rewrite|add|include|update|revise|modify|remove)|'
            r'can\s+re-?analyze|would\s+be\s+happy\s+to\s+include|we\s+plan\s+to)\b',
            re.I,
        )
        commit_pat = re.compile(
            r'\b(?:we\s+(?:have\s+)?(?:added|revised|adapted|included|modified|updated|replaced|removed|corrected|clarified)|'
            r'has\s+been\s+(?:added|revised|adapted|included|modified|updated|replaced|removed|corrected|clarified))\b[^.]{0,140}\.',
            re.I,
        )
        for i, rl in enumerate(reb_lines, 1):
            ptag, clean = strip_ptag(rl)
            loc = ptag or f"Reb-L{i}"
            for m in draft_pat.finditer(clean):
                print(f"    !! {loc}: UNFULFILLED DRAFT/FUTURE PHRASING '{m.group(0)}' in: {clean}")
            for m_c in commit_pat.finditer(clean):
                c_sent = m_c.group(0).strip()
                # Check if the commitment includes a quoted string and whether that quote appears in the manuscript
                quotes_in_c = re.findall(r'[\u201c"]([^\u201d"]{10,120})[\u201d"]', c_sent)
                q_note = ""
                if quotes_in_c:
                    missing_q = [q for q in quotes_in_c if q.lower() not in all_raw_lower]
                    if missing_q:
                        q_note = f" !! QUOTED REVISION TEXT NOT FOUND IN MANUSCRIPT: {missing_q}"
                    else:
                        q_note = " [quoted text verified in manuscript]"
                print(f"    - {loc} [Commitment to verify]: \"{c_sent}\"{q_note}")

        print("\n  6c. Location / scope claims in Rebuttal vs Manuscript:")
        loc_pat = re.compile(
            r'((?:limited|moved|transferred|restricted)\s+[^.]{1,60}?\s+to\s+the\s+Supplementary\s+Information|'
            r'change\s+the\s+title)',
            re.I,
        )
        for i, rl in enumerate(reb_lines, 1):
            ptag, clean = strip_ptag(rl)
            loc = ptag or f"Reb-L{i}"
            for m in loc_pat.finditer(clean):
                print(f"    !! {loc}: location/title claim: '{m.group(0)}'")

        print("\n  6d. Figure, Table & Video callouts in Rebuttal (side-by-side vs Manuscript Legends + Semantic Topic Check):")
        reb_stop = {'figure', 'figures', 'fig', 'figs', 'table', 'tables', 'video', 'videos', 'movie', 'movies', 'supplementary', 'supplemental', 'expanded', 'view', 'panel', 'panels', 'shown', 'shows', 'data', 'added', 'revised', 'new', 'now', 'see', 'also', 'with', 'from', 'that', 'this', 'were', 'have', 'been'}
        for i, rl in enumerate(reb_lines, 1):
            ptag, clean = strip_ptag(rl)
            loc = ptag or f"Reb-L{i}"
            for m in re.finditer(r'\b(?:(?:Supplementary|Supplemental|Expanded\s+View|Extended\s+Data)\s+)?(?:Figures?|Figs?\.?|Tables?|Videos?|Movies?)\s+(?:EV|ED|S)?\d+[A-Za-z]?\b', clean, re.I):
                cited_raw = m.group(0)
                norm_k = normalize_callout_key(cited_raw)
                ms_leg = legend_map.get(norm_k) if norm_k else None
                ctx_slice = clean[max(0, m.start() - 60):min(len(clean), m.end() + 80)]
                print(f"    - {loc}: cites '{cited_raw}' in: ...{clean[max(0, m.start()-25):m.end()+55]}...")
                if ms_leg:
                    print(f"        -> Manuscript legend: {ms_leg}")
                    # Semantic topic overlap check across same-kind display items (catches e.g. 'video S4' cited for a topic in 'Video S1')
                    ctx_words = {w.lower()[:6] for w in re.findall(r'\b[A-Za-z]{4,}\b', ctx_slice) if w.lower() not in reb_stop}
                    leg_words = {w.lower()[:6] for w in re.findall(r'\b[A-Za-z]{4,}\b', ms_leg) if w.lower() not in reb_stop}
                    if len(ctx_words) >= 2 and not (ctx_words & leg_words):
                        kind_prefix = norm_k.split()[0] + " "
                        best_alt_k, best_alt_ov = None, set()
                        for cand_k, cand_leg in legend_map.items():
                            if cand_k != norm_k and cand_k.startswith(kind_prefix):
                                cw = {w.lower()[:6] for w in re.findall(r'\b[A-Za-z]{4,}\b', cand_leg) if w.lower() not in reb_stop}
                                ov = ctx_words & cw
                                if len(ov) > len(best_alt_ov):
                                    best_alt_k, best_alt_ov = cand_k, ov
                        if best_alt_k and len(best_alt_ov) >= 2:
                            print(
                                f"        !! SEMANTIC TOPIC MISMATCH ON REBUTTAL CALLOUT: '{cited_raw}' context shares 0 topic stems with {norm_k}, "
                                f"but shares {sorted(best_alt_ov)} with {best_alt_k} ({legend_map[best_alt_k][:95]}...) — verify whether '{cited_raw}' is a wrong number!"
                            )
                elif norm_k and legend_map:
                    print(f"        !! WARNING: '{norm_k}' is NOT defined in manuscript legends (defined: {', '.join(sorted(legend_map.keys()))})")

        print("\n  6e. Rebuttal-Only Definitions / Clarifications Not Ported to Manuscript:")
        def_pat = re.compile(r'\b([A-Za-z0-9_-]{2,15})\s+(?:stands\s+for|refers\s+to|denotes|is\s+an\s+abbreviation\s+for|is\s+a\s+derivative\s+of)\s+([A-Za-z0-9\s,\-]{4,50}?)(?=[.,;)]|\band\b)', re.I)
        any_reb_def = False
        for i, rl in enumerate(reb_lines, 1):
            ptag, clean = strip_ptag(rl)
            loc = ptag or f"Reb-L{i}"
            for m_d in def_pat.finditer(clean):
                term_short, term_long = m_d.group(1).strip(), m_d.group(2).strip()
                if term_long.lower() not in all_raw_lower:
                    any_reb_def = True
                    print(f"    !! {loc}: Rebuttal defines '{term_short}' as '{term_long}', but '{term_long}' does NOT appear anywhere in the manuscript!")
        if not any_reb_def:
            print("    None detected.")

    if cov_path and os.path.isfile(cov_path):
        print("\n== 7. COVER LETTER <-> MANUSCRIPT CROSS-AUDIT ==")
        cov_lines = open(cov_path, encoding='utf-8').read().splitlines()
        ms_title_candidates = [c.strip() for _, c in body_lines[:5] if len(c.strip()) > 15]
        if ms_title_candidates:
            print(f"  Manuscript opening title line: \"{ms_title_candidates[0][:140]}\"")

        print("\n  7a. Figure, Table & Video callouts in Cover Letter vs Manuscript Legends:")
        any_cov_callouts = False
        for i, cl in enumerate(cov_lines, 1):
            ptag, clean = strip_ptag(cl)
            loc = ptag or f"Cov-L{i}"
            for m in re.finditer(r'\b(?:(?:Supplementary|Supplemental|Expanded\s+View|Extended\s+Data)\s+)?(?:Figures?|Figs?\.?|Tables?|Videos?|Movies?)\s+(?:EV|ED|S)?\d+[A-Za-z]?\b', clean, re.I):
                any_cov_callouts = True
                cited_raw = m.group(0)
                norm_k = normalize_callout_key(cited_raw)
                ms_leg = legend_map.get(norm_k) if norm_k else None
                print(f"    - {loc}: cites '{cited_raw}' in: ...{clean[max(0, m.start()-30):m.end()+60]}...")
                if ms_leg:
                    print(f"        -> Manuscript legend: {ms_leg}")
                elif norm_k:
                    print(f"        !! CRITICAL MISMATCH: Cover Letter cites '{norm_k}', which does NOT exist in Manuscript Legends!")
        if not any_cov_callouts:
            print("    None detected.")

        print("\n  7b. Quoted titles, nomenclature & quantitative claims in Cover Letter:")
        for i, cl in enumerate(cov_lines, 1):
            ptag, clean = strip_ptag(cl)
            loc = ptag or f"Cov-L{i}"
            for m_q in re.finditer(r'[\u201c"]([^\u201d"]{15,200})[\u201d"]', clean):
                q_str = m_q.group(1).strip()
                in_ms = q_str.lower() in all_raw_lower
                status = "MATCHES manuscript" if in_ms else "NOT FOUND verbatim in manuscript — verify title/quote!"
                print(f"    - {loc} quoted string: \"{q_str}\" -> [{status}]")

    print("\n== 8. ABBREVIATION LIFECYCLE AUDIT (FIRST-USE, RE-DEFINITIONS & SINGLE-USE ACRONYMS) ==")
    std_acronyms = {
        'DNA', 'RNA', 'MRNA', 'TRNA', 'RRNA', 'CDNA', 'SIRNA', 'SHRNA', 'GRNA', 'CRNA', 'SNRNA', 'MIRNA',
        'ATP', 'ADP', 'AMP', 'GTP', 'GDP', 'CTP', 'UTP', 'NAD', 'NADH', 'NADP', 'NADPH', 'FAD', 'COA',
        'PCR', 'QPCR', 'RT-PCR', 'PBS', 'PBST', 'TBS', 'TBST', 'HBSS', 'DMEM', 'RPMI', 'MEM', 'FBS', 'FCS',
        'BSA', 'DMSO', 'EDTA', 'EGTA', 'SDS', 'PAGE', 'TRIS', 'HEPES', 'MOPS', 'MES', 'PIPES', 'IPTG', 'DTT',
        'PMSF', 'TCEP', 'LB', 'SOB', 'SOC', 'YPD', 'OD', 'CFU', 'PFU', 'MOI', 'RPM', 'RCF', 'GFP', 'EGFP',
        'RFP', 'YFP', 'CFP', 'BF', 'DIC', 'DAPI', 'FITC', 'TRITC', 'CY3', 'CY5', 'HRP', 'ELISA', 'FACS',
        'MACS', 'HPLC', 'FPLC', 'LC', 'MS', 'GC', 'NMR', 'EPR', 'SPR', 'ITC', 'DLS', 'SEC', 'AUC', 'AFM',
        'TEM', 'SEM', 'STEM', 'CRYO-EM', 'XRD', 'SAXS', 'FRET', 'FRAP', 'FLIM', 'TIRF', 'SIM', 'STED', 'PALM',
        'STORM', 'ROI', 'FOV', 'PSF', 'LUT', 'SNR', 'SD', 'SE', 'CI', 'IQR', 'CV', 'ANOVA', 'ANCOVA', 'MANOVA',
        'PCA', 'TSNE', 'UMAP', 'FDR', 'ROC', 'OR', 'HR', 'RR', 'NS', 'NA', 'ND', 'WT', 'KO', 'KD', 'KI', 'OE',
        'IC50', 'EC50', 'LD50', 'MIC', 'MBC', 'KD', 'KM', 'KCAT', 'VMAX', 'PH', 'PKA', 'PI', 'MW', 'KDA', 'DA',
        'BP', 'KB', 'MB', 'GB', 'TB', 'NT', 'AA', 'ORF', 'UTR', 'CDS', 'TSS', 'TTS', 'SNP', 'SNV', 'INDEL',
        'CNV', 'QTL', 'GWAS', 'GO', 'KEGG', 'GSEA', 'DEG', 'TPM', 'FPKM', 'RPKM', 'CPM', 'UMI', 'BAM', 'SAM',
        'VCF', 'FASTA', 'FASTQ', 'GTF', 'GFF', 'BED', 'NCBI', 'NIH', 'NSF', 'ERC', 'EMBL', 'EBI', 'SIB', 'DDBJ',
        'GEO', 'SRA', 'ENA', 'PRIDE', 'PDB', 'EMDB', 'BMRB', 'UNIPROT', 'PFAM', 'INTERPRO', 'KEGG', 'COG',
        'ATCC', 'DSMZ', 'ECACC', 'ADDGENE', 'USA', 'UK', 'EU', 'WHO', 'FDA', 'EMA', 'CDC', 'EFSA', 'ISO',
        'DOI', 'URL', 'URI', 'ISBN', 'ISSN', 'PMID', 'PMCID', 'ORCID', 'PDF', 'HTML', 'XML', 'JSON', 'CSV',
        'TSV', 'XLSX', 'DOCX', 'PNG', 'JPG', 'JPEG', 'TIFF', 'TIF', 'SVG', 'EPS', 'MP4', 'AVI', 'MOV', 'ZIP',
        'RGB', 'CMYK', 'DPI', 'FPS', 'CPU', 'GPU', 'RAM', 'OS', 'API', 'CLI', 'GUI', 'IDE', 'SQL', 'IRB',
        'IACUC', 'SOP', 'GLP', 'GMP', 'QC', 'QA', 'ID', 'II', 'III', 'IV', 'VI', 'VII', 'VIII', 'IX', 'XI', 'XII',
        'FIG', 'FIGS', 'TABLE', 'BOX', 'SUPP', 'EV', 'ED', 'REF', 'REFS', 'EQ', 'EQS', 'VOL', 'NO', 'PP', 'ED',
    }
    prose_body = [
        (idx_b, loc, clean)
        for idx_b, (loc, clean) in enumerate(body_lines)
        if idx_b >= 4 and not loc.startswith(('[T', '[TABLE', '[Row')) and not re.match(r'^\s*(?:Figure|Fig\.?|Table|Supplementary|Expanded\s+View)\s+', clean, re.I)
    ]
    acr_defs = defaultdict(list)       # ACR -> list of (idx_b, loc, expansion_preview)
    acr_usages = defaultdict(list)     # ACR -> list of (idx_b, loc)
    def_paren_pat = re.compile(r'([A-Za-z][A-Za-z0-9\-\s/]{5,75}?)\s*\(\s*([A-Z][A-Za-z0-9\-]{1,7}s?)\s*\)')

    for idx_b, loc, clean in prose_body:
        for m_def in def_paren_pat.finditer(clean):
            pre_phrase = m_def.group(1).strip()
            acr_raw = m_def.group(2).strip()
            acr = acr_raw[:-1] if (acr_raw.endswith('s') and len(acr_raw) >= 4 and acr_raw[:-1].isupper()) else acr_raw
            if sum(1 for c in acr if c.isupper()) < 2 or len(acr) < 2 or len(acr) > 8:
                continue
            if acr.upper() in std_acronyms or acr.lower() in ignore_set:
                continue
            # Check whether preceding words share initial letters with the acronym (confirming a genuine expansion)
            pre_words = [w for w in re.findall(r'[A-Za-z0-9]+', pre_phrase) if w.lower() not in ('the', 'of', 'and', 'in', 'to', 'for', 'with', 'by', 'a', 'an', 'or', 'on', 'at', 'from')]
            tail_words = pre_words[-len(acr) - 2:] if pre_words else []
            acr_letters = [c.lower() for c in acr if c.isalpha()]
            if not tail_words or not acr_letters:
                continue
            tail_initials = [w[0].lower() for w in tail_words]
            tail_joined = " ".join(w.lower() for w in tail_words)
            if acr.lower() in [w.lower() for w in tail_words]:
                continue
            if acr_letters[0] in tail_initials and sum(1 for ch in acr_letters if ch in tail_joined) >= max(2, len(acr_letters) - 1):
                exp_preview = " ".join(tail_words[-min(len(tail_words), len(acr) + 1):])
                acr_defs[acr].append((idx_b, loc, exp_preview))

        for m_u in re.finditer(r'\b([A-Z][A-Za-z0-9\-]{1,7})s?\b', clean):
            tok = m_u.group(1)
            if tok.upper() in std_acronyms or tok.lower() in ignore_set or tok.isdigit():
                continue
            if sum(1 for c in tok if c.isupper()) >= 2:
                acr_usages[tok].append((idx_b, loc))

    acr_issues = 0
    for acr in sorted(acr_defs.keys()):
        defs = acr_defs[acr]
        usages = acr_usages.get(acr, [])
        first_def_idx, first_def_loc, first_exp = defs[0]
        early_uses = [u_loc for u_idx, u_loc in usages if u_idx < first_def_idx]
        if early_uses:
            acr_issues += 1
            print(f"  !! [USED BEFORE DEFINITION]: '{acr}' is used at {early_uses[:3]} before its first expansion '{first_exp} ({acr})' at {first_def_loc}!")
        if len(defs) >= 2:
            def_locs = [d_loc for _, d_loc, _ in defs]
            if len(set(def_locs)) >= 2:
                acr_issues += 1
                print(f"  [Redundant Acronym Re-Definition]: '{acr}' ('{first_exp}') is defined {len(defs)} times across the text: {def_locs}")
        later_uses = [u_loc for u_idx, u_loc in usages if u_idx > first_def_idx]
        if not later_uses and not early_uses:
            acr_issues += 1
            print(f"  [Single-Use Defined Acronym]: '{acr}' is defined at {first_def_loc} ('{first_exp} ({acr})') but never used again in the manuscript body.")
    if acr_issues == 0:
        print("  No abbreviation lifecycle anomalies detected.")

    print("\n== 9. EDITING-COLLISION, GRAMMAR & SCIENTIFIC NOTATION / SI HYGIENE SCANNER ==")
    gram_issues = 0
    collision_pat = re.compile(
        r'\b(has\s+have|have\s+has|was\s+were|were\s+was|is\s+are|are\s+is|can\s+could|will\s+would|'
        r'showed\s+demonstrated|demonstrated\s+showed|indicates?\s+suggests?|suggests?\s+indicates?)\b'
        r'|(?-i:\b(a\s+an|an\s+a|the\s+a|a\s+the)\b)(?!\.)',
        re.I,
    )
    a_before_vowel_pat = re.compile(
        r'\ba\s+(osmotic|hour\b|honest|upper|outer|inner|early|important|increase|increased|increasing|'
        r'assay|experiment|experimental|analysis|average|example|essential|effective|efficient|individual|'
        r'independent|initial|additional|alternative|overall|untreated|uninfected|unusual|8-|11-|18-)\b'
    )
    an_before_cons_pat = re.compile(
        r'\ban\s+(uniform|unique|universal|unilateral|ubiquitous|one-|european|typical|specific|significant|'
        r'significantly|single|similar|standard|small|large|high|higher|low|lower|new|novel|normal|natural|'
        r'negative|positive|major|minor|common|complete|control|clear|direct|different)\b',
        re.I,
    )
    double_comp_pat = re.compile(
        r'\b(more\s+(?:higher|lower|faster|slower|better|worse|larger|smaller|longer|shorter)|'
        r'most\s+(?:highest|lowest|largest|smallest|fastest|slowest)|'
        r'(?:approximately|about|around)\s*~|~\s*(?:approximately|about|around))\b',
        re.I,
    )
    stripped_sup_pat = re.compile(r'(?<![\^0-9.])\b(10[4-9])\s*(cells?|cfu|pfu|bacteria|particles|copies|virions|spores)\b', re.I)

    for loc, clean in body_lines:
        for m_c in collision_pat.finditer(clean):
            gram_issues += 1
            print(f"  !! [TRACK-CHANGE / AUXILIARY MERGE COLLISION {loc}]: '{m_c.group(0)}' in \"...{clean[max(0, m_c.start()-25):m_c.end()+35]}...\"")
        for m_av in a_before_vowel_pat.finditer(clean):
            gram_issues += 1
            print(f"  [Article 'a' vs 'an' mismatch {loc}]: '{m_av.group(0)}' -> should be 'an {m_av.group(1)}'")
        for m_ac in an_before_cons_pat.finditer(clean):
            gram_issues += 1
            print(f"  [Article 'an' vs 'a' mismatch {loc}]: '{m_ac.group(0)}' -> should be 'a {m_ac.group(1)}'")
        for m_dc in double_comp_pat.finditer(clean):
            gram_issues += 1
            print(f"  [Double comparative / redundant qualifier {loc}]: '{m_dc.group(0)}' in \"...{clean[max(0, m_dc.start()-20):m_dc.end()+25]}...\"")
        for m_ss in stripped_sup_pat.finditer(clean):
            gram_issues += 1
            p10 = m_ss.group(1)
            print(f"  !! [UNFORMATTED POWER-OF-10 SUPERSCRIPT {loc}]: '{m_ss.group(0)}' (likely stripped superscript '10^{p10[2:]} {m_ss.group(2)}')")

    # 9b. SI & Unit Formatting Hygiene (within-document mixing of uM/µM, mL/ml, h/hr/hrs, 37°C/37 °C)
    full_body_str = "\n".join(clean for _, clean in body_lines)
    ascii_u_hits = re.findall(r'\b\d+(?:\.\d+)?\s*(u[MmgLl])\b', full_body_str)
    micro_u_hits = re.findall(r'\b\d+(?:\.\d+)?\s*([\u00b5\u03bc][MmgLl])\b', full_body_str)
    if ascii_u_hits and micro_u_hits:
        gram_issues += 1
        print(f"  [Unit Symbol Inconsistency]: Mixes ASCII 'u' units ({Counter(ascii_u_hits).most_common(3)}) with micro 'µ' units ({Counter(micro_u_hits).most_common(3)}).")
    ml_upper = len(re.findall(r'\b\d+(?:\.\d+)?\s*(?:mL|[\u00b5\u03bc]L)\b', full_body_str))
    ml_lower = len(re.findall(r'\b\d+(?:\.\d+)?\s*(?:ml|[\u00b5\u03bc]l)\b', full_body_str))
    if ml_upper >= 2 and ml_lower >= 2:
        gram_issues += 1
        print(f"  [Liter Casing Inconsistency]: Mixes uppercase 'mL/µL' ({ml_upper}x) and lowercase 'ml/µl' ({ml_lower}x).")
    h_short = len(re.findall(r'\b\d+(?:\.\d+)?\s*h\b', full_body_str))
    h_long = len(re.findall(r'\b\d+(?:\.\d+)?\s*hrs?\b', full_body_str))
    if h_short >= 2 and h_long >= 2:
        gram_issues += 1
        print(f"  [Time Unit Inconsistency]: Mixes SI 'h' ({h_short}x) with non-SI 'hr/hrs' ({h_long}x).")
    deg_nospace = len(re.findall(r'\b\d+\u00b0C\b', full_body_str))
    deg_space = len(re.findall(r'\b\d+\s+\u00b0C\b', full_body_str))
    if deg_nospace >= 2 and deg_space >= 2:
        gram_issues += 1
        print(f"  [Temperature Spacing Inconsistency]: Mixes unspaced 'X°C' ({deg_nospace}x) and spaced 'X °C' ({deg_space}x).")
    if gram_issues == 0:
        print("  None detected.")

    print("\n== 10. WORD TABLE ([TABLE N]) INTERNAL ARITHMETIC, SUM & PERCENTAGE AUDIT ==")
    tbl_blocks = defaultdict(list)
    cur_t = None
    for raw in raw_lines:
        m_th = re.match(r'^\s*\[TABLE\s+(\d+)\]\s*$', raw)
        if m_th:
            cur_t = f"TABLE {m_th.group(1)}"
            continue
        if cur_t:
            if raw.startswith('[Row ') or '|' in raw:
                tbl_blocks[cur_t].append(raw)
            elif raw.strip() and not raw.startswith('['):
                pass
            elif raw.startswith('[P'):
                cur_t = None
    tbl_arith_issues = 0
    for tid, t_rows in tbl_blocks.items():
        # Check for Total N in header/rows and verify k (P%) cells
        t_text = "\n".join(t_rows)
        n_totals = [int(m.group(1)) for m in re.finditer(r'\b[Nn]\s*=\s*(\d{2,5})\b', t_text)]
        for r_line in t_rows:
            r_tag, r_clean = strip_ptag(r_line)
            for m_kp in re.finditer(r'\b(\d{1,4})\s*\(\s*(\d{1,3}(?:\.\d+)?)\s*%\s*\)', r_clean):
                k_val = int(m_kp.group(1))
                p_val = float(m_kp.group(2))
                if n_totals and 0 < k_val <= max(n_totals):
                    matches_any_n = False
                    for n_tot in n_totals:
                        if n_tot > 0:
                            calc_p = (k_val / n_tot) * 100.0
                            tol = 0.65 if '.' in m_kp.group(2) else 1.15
                            if abs(calc_p - p_val) <= tol:
                                matches_any_n = True
                                break
                    if not matches_any_n and len(set(n_totals)) == 1:
                        n0 = n_totals[0]
                        exp_p = (k_val / n0) * 100.0
                        tbl_arith_issues += 1
                        print(f"  !! [{tid} {r_tag} PERCENTAGE ARITHMETIC MISMATCH]: cell states '{m_kp.group(0)}', but {k_val}/{n0} = {exp_p:.1f}%!")
            if re.search(r'\b(?:TODO|TBD|FIXME|\?\?\?)\b|<insert\b', r_clean, re.I):
                tbl_arith_issues += 1
                print(f"  !! [{tid} {r_tag} UNFILLED TABLE PLACEHOLDER]: \"{r_clean[:120]}\"")
    if tbl_arith_issues == 0:
        print("  All inspected Word tables passed internal percentage and placeholder checks.")

    print("\n== 11. CROSS-SECTION CLAIM, NUMBER & POLARITY COMPARATOR (ABSTRACT <-> RESULTS <-> DISCUSSION) ==")
    sec_sents = {'Abstract': [], 'Results': [], 'Discussion': []}
    cur_sec_name = 'Abstract'
    for idx_b, (loc, clean) in enumerate(body_lines):
        if loc.startswith(('[T', '[TABLE', '[Row')) or len(clean) < 4:
            continue
        if len(clean) < 65 and not clean.endswith('.'):
            if re.search(r'\b(?:introduction|background)\b', clean, re.I):
                cur_sec_name = 'Intro'
                continue
            elif re.search(r'\bresults?\b', clean, re.I):
                cur_sec_name = 'Results'
                continue
            elif re.search(r'\b(?:discussion|conclusions?|concluding)\b', clean, re.I):
                cur_sec_name = 'Discussion'
                continue
            elif re.search(r'\b(?:materials?|methods?|experimental|references|acknowledg|figure\s+legends?)\b', clean, re.I):
                cur_sec_name = 'Other'
                continue
        if idx_b <= 12 and cur_sec_name == 'Abstract' and len(clean) >= 80:
            for s in re.split(r'(?<!\b[A-Z])(?<=[.?!])\s+(?=[A-Z0-9])', clean):
                if len(s) >= 35:
                    sec_sents['Abstract'].append((loc, s.strip()))
        elif cur_sec_name in ('Results', 'Discussion'):
            for s in re.split(r'(?<!\b[A-Z])(?<=[.?!])\s+(?=[A-Z0-9])', clean):
                if len(s) >= 35:
                    sec_sents[cur_sec_name].append((loc, s.strip()))

    neg_cue_re = re.compile(r'\b(?:not|no|never|neither|none|unaffected|independent|dispensable|failed\s+to|lacks?|lacking|absence\s+of|without)\b', re.I)
    quant_cue_re = re.compile(r'\b(\d+(?:\.\d+)?\s*(?:-fold|%)|\bn\s*=\s*\d+)\b', re.I)
    stop_cs = {
        'the', 'and', 'for', 'with', 'that', 'this', 'from', 'were', 'was', 'are', 'have', 'has', 'had', 'not',
        'but', 'also', 'shown', 'showed', 'observed', 'found', 'results', 'data', 'cells', 'protein', 'proteins',
        'using', 'used', 'between', 'under', 'after', 'during', 'within', 'into', 'both', 'each', 'more', 'than',
    }

    def _cs_tokens(s):
        return {w.lower() for w in re.findall(r'\b[A-Za-z][A-Za-z0-9\-]{3,}\b', s) if w.lower() not in stop_cs}

    cs_flags = 0
    if sec_sents['Results']:
        res_indexed = [(r_loc, r_s, _cs_tokens(r_s), quant_cue_re.findall(r_s), bool(neg_cue_re.search(r_s))) for r_loc, r_s in sec_sents['Results']]
        for target_sec in ('Abstract', 'Discussion'):
            for s_loc, s_txt in sec_sents[target_sec]:
                s_toks = _cs_tokens(s_txt)
                if len(s_toks) < 4:
                    continue
                s_quants = quant_cue_re.findall(s_txt)
                s_neg = bool(neg_cue_re.search(s_txt))
                best_match = None
                best_ov = 0
                for r_loc, r_s, r_toks, r_quants, r_neg in res_indexed:
                    ov = len(s_toks & r_toks)
                    if ov > best_ov and ov >= 4 and ov / max(1, min(len(s_toks), len(r_toks))) >= 0.45:
                        best_ov = ov
                        best_match = (r_loc, r_s, s_toks & r_toks, r_quants, r_neg)
                if best_match:
                    r_loc, r_s, shared_t, r_quants, r_neg = best_match
                    if s_quants and r_quants and set(q.lower().replace(' ', '') for q in s_quants) != set(q.lower().replace(' ', '') for q in r_quants):
                        cs_flags += 1
                        print(f"  !! [CROSS-SECTION QUANTITATIVE DRIFT ({target_sec} {s_loc} vs Results {r_loc})]:")
                        print(f"     {target_sec} ({s_loc}, states {s_quants}): \"{s_txt[:150]}\"")
                        print(f"     Results  ({r_loc}, states {r_quants}): \"{r_s[:150]}\"")
                    elif s_neg != r_neg and best_ov >= 5:
                        cs_flags += 1
                        print(f"  ? [CROSS-SECTION POLARITY / NEGATION CONTRAST ({target_sec} {s_loc} vs Results {r_loc}, shared={sorted(shared_t)[:6]})]:")
                        print(f"     {target_sec} ({s_loc}): \"{s_txt[:150]}\"")
                        print(f"     Results  ({r_loc}): \"{r_s[:150]}\"")
    if cs_flags == 0:
        print("  No cross-section quantitative or polarity conflicts detected between Abstract, Results, and Discussion.")


if __name__ == '__main__':
    main()
