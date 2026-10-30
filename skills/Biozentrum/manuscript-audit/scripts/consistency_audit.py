#!/usr/bin/env python3
"""Cross-manuscript nomenclature, typo, homophone, unit hygiene, quantitative recipe, and rebuttal audit.

Usage:
  consistency_audit.py TEXTFILE [--rebuttal REBUTTAL.txt] [--cover-letter COVER.txt] [--format-audit FORMAT_AUDIT.txt]

Works on plain or `--indexed` output (`[P12] ...`, `[T1] ...`) from `docx2txt.py`.

Reports:
  1. Consecutive duplicate words & duplicated multi-word clauses within the same sentence
  2. Nomenclature, scientific homophones & near-miss spelling anomalies:
     - Mutant / allele tokens (Δ/∆ symbol mixing, near-miss names like ΔgeneA vs ΔgeneA1)
     - Compound / drug codes (e.g. CMP-1060 vs CMP-1006)
     - Taxonomic genus names (e.g. Bacillus vs Baccillus, 'Candidatus spp.' rank misuse)
     - Context-sensitive scientific homophones ("commercialized dies" -> "dyes", "mutant stain" -> "strain", "respiratory track" -> "tract", "unclear invasion" -> "nuclear invasion", etc.)
     - Filtered high-frequency bigram near-misses (excluding scientific antonym prefixes and common prepositions)
  3. Unit, time abbreviation & spacing consistency (mL vs ml, µL vs uL, sec vs s, 17h vs 17 h)
  4. Quantitative & experimental parameter cross-check (dilution math, concentrations, hpi, MOIs, n)
  5. Journal declaration completeness check (Acknowledgments, Author Contributions, Competing Interests, Data Availability)
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
]


def strip_ptag(line):
    m = re.match(r'^\s*(\[(?:P\d+(?:\.\d+)?|T\d+|TABLE\s+\d+|Row\s+\d+)\])\s*(.*)', line)
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
    # Insertion / deletion of 1 character
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
    if not fmt_path:
        return 0, []
    txt = open(fmt_path, encoding='utf-8').read()
    m_emp = re.search(r'Empty spacer paragraphs without <w:suppressLineNumbers/>:\s*(\d+)', txt)
    empty_count = int(m_emp.group(1)) if m_emp else 0
    # Parse rows: [P12 ] | L45 -52  | Word L54 -63  | preview
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

    ignore_set = load_ignore_set()
    raw_lines = [l for l in open(path, encoding='utf-8').read().splitlines() if not l.startswith('# MANUSCRIPT-AUDIT EXTRACT')]
    legend_map = build_manuscript_legend_map(raw_lines)
    empty_spacer_count, fmt_rows = parse_format_audit_file(fmt_path)

    body_lines = []
    in_refs = False
    for line in raw_lines:
        ptag, clean = strip_ptag(line)
        if re.match(r'^\s*(?:\d+\.?\s+)?(?:references|bibliography|literature\s+cited)\s*$', clean, re.I):
            in_refs = True
            continue
        if in_refs and re.match(r'^\s*(?:(?:main\s+|supplementary\s+)?figure\s+legends?|figures?|tables?|video\s+legends?)\s*$', clean, re.I):
            in_refs = False
        if not in_refs:
            body_lines.append((ptag or 'body', clean))

    print("== 1. CONSECUTIVE DUPLICATE WORDS & GARBLED DUPLICATED CLAUSES ==")
    dup_found = False
    for loc, clean in body_lines:
        # 1a. Immediate duplicate word: "images images", "the the"
        #     Skip intentional case-distinct emphasis pairs like "locked on ON"
        for m in re.finditer(r'\b([A-Za-z]{2,})\s+\1\b', clean, re.I):
            toks = m.group(0).split()
            if len(toks) == 2 and (toks[0].isupper() != toks[1].isupper()):
                continue
            print(f"  !! {loc}: repeated word '{m.group(0)}' in: ...{clean[max(0, m.start()-25):m.end()+35]}...")
            dup_found = True

        # 1b. Duplicated 2-to-3-word phrase within the SAME unpunctuated clause (no comma/and/or/whereas between them)
        #     Catches garbled draft clauses like "the inner layer is surrounded by a thin layer surrounded by"
        #     while avoiding normal parallel clauses ("X was stained with A and Y was stained with B").
        if not loc.startswith(('[T', '[Row')):
            clauses = re.split(r'[.?!,;:]|\b(?:and|or|whereas|while|versus|vs|than|against|between|from|to)\b', clean, flags=re.I)
            for clause in clauses:
                words = re.findall(r'\b[a-zA-Z]{2,}\b', clause.lower())
                if len(words) < 5:
                    continue
                for n_len in (3, 2):
                    seen_ngrams = {}
                    found_in_clause = False
                    for k in range(len(words) - n_len + 1):
                        ng = tuple(words[k:k + n_len])
                        if all(len(w) <= 3 or w in ('that', 'this', 'with', 'from', 'were', 'have', 'been', 'the', 'for', 'are') for w in ng):
                            continue
                        if ng in seen_ngrams and (k - seen_ngrams[ng]) <= 6:
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
            # Skip pairs that co-occur in the same paragraph (distinct constructs being compared)
            if set(mutants[m1]) & set(mutants[m2]):
                continue
            if edit_distance_le2(m1, m2):
                print(f"  !! Near-miss mutant names: 'Δ{m1}' ({len(mutants[m1])}x at {mutants[m1][:5]}) "
                      f"vs 'Δ{m2}' ({len(mutants[m2])}x at {mutants[m2][:5]})")

    # 2b. Compound / strain / construct alphanumeric codes (e.g. CMP-1060 vs CMP-1006, STR1004 vs STR1007, PgeneA-GFP vs PgeneB-GFP)
    codes = defaultdict(list)
    for loc, clean in body_lines:
        for m in re.finditer(r'\b([A-Z]{2,5}-?\d{3,6}[A-Za-z]?|P[a-z]{3,4}[A-Z]-[A-Za-z0-9_-]+)\b', clean):
            codes[m.group(1)].append(loc)
    code_names = sorted(codes.keys())
    for i in range(len(code_names)):
        for j in range(i + 1, len(code_names)):
            c1, c2 = code_names[i], code_names[j]
            p1 = re.match(r'^[A-Za-z]+', c1).group(0)
            p2 = re.match(r'^[A-Za-z]+', c2).group(0)
            if (p1 == p2 or (c1.startswith('P') and '-' in c1 and c1.split('-')[1] == c2.split('-')[-1])) and edit_distance_le2(c1, c2):
                # Avoid flagging sequential numbered series (e.g. Strain1, Strain2, Strain3) unless one is rare and differs by 1 digit in a >=4-digit number
                if '-' not in c1 and abs(int(re.search(r'\d+', c1).group(0)) - int(re.search(r'\d+', c2).group(0))) == 1:
                    continue
                print(f"  !! Near-miss compound/strain/construct codes: '{c1}' ({len(codes[c1])}x at {codes[c1][:5]}) "
                      f"vs '{c2}' ({len(codes[c2])}x at {codes[c2][:5]})")

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

    # 2e. Bigram & singleton near-miss typo detector (tightened to edit distance 1 + shared prefix)
    bigram_counts = Counter()
    bigram_locs = defaultdict(list)
    by_second_word = defaultdict(set)
    word_counts = Counter()
    word_locs = defaultdict(list)
    for loc, clean in body_lines:
        words = re.findall(r'\b[a-z]{5,}\b', clean.lower())
        for w in words:
            word_counts[w] += 1
            if len(word_locs[w]) < 3:
                word_locs[w].append(loc)
        for k in range(len(words) - 1):
            bg = (words[k], words[k + 1])
            bigram_counts[bg] += 1
            bigram_locs[bg].append(loc)
            by_second_word[words[k + 1]].add(words[k])

    def _is_inflection_pair(a, b):
        return (
            a + 's' == b or b + 's' == a
            or a + 'd' == b or b + 'd' == a
            or a + 'ed' == b or b + 'ed' == a
            or a + 'es' == b or b + 'es' == a
        )

    reported_typo_words = set()
    for (w1, w2), cnt in sorted(bigram_counts.items()):
        if w1 in ignore_set or w2 in ignore_set:
            continue
        if cnt == 1 and w1 not in COMMON_STOP_FIRST_WORDS and w2 not in COMMON_STOP_FIRST_WORDS and w2 not in ('cells', 'cell'):
            for alt_w1 in by_second_word[w2]:
                if bigram_counts[(alt_w1, w2)] >= 5 and w1[:2] == alt_w1[:2] and edit_distance_le1(w1, alt_w1):
                    if is_antonym_pair(w1, alt_w1) or _is_inflection_pair(w1, alt_w1):
                        continue
                    reported_typo_words.add(w1)
                    print(f"  !! Suspected typo in bigram {bigram_locs[(w1, w2)][0]}: '{w1} {w2}' (1x) "
                          f"vs frequent '{alt_w1} {w2}' ({bigram_counts[(alt_w1, w2)]}x)")

    # Singleton word typo check: 1x word (>=7 chars) within edit_distance_le1 of a frequent word
    # sharing either a 3-char prefix or (1st char + 3-char suffix, catching missing-2nd-letter typos)
    frequent_words = [w for w, c in word_counts.items() if (c >= 4 and len(w) >= 7) or (c >= 2 and len(w) >= 9)]
    for w, cnt in sorted(word_counts.items()):
        if cnt == 1 and len(w) >= 7 and w not in ignore_set and w not in reported_typo_words:
            for fw in frequent_words:
                if (w[:3] == fw[:3] or (w[0] == fw[0] and w[-3:] == fw[-3:])) and edit_distance_le1(w, fw) and not is_antonym_pair(w, fw) and not _is_inflection_pair(w, fw):
                    print(f"  !! Suspected singleton spelling typo at {word_locs[w][0]}: '{w}' (1x in document) "
                          f"vs '{fw}' ({word_counts[fw]}x in document)")
                    break

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
    if not any_unit_mix:
        print("  Unit abbreviations and spacing are uniform.")

    print("\n== 4. QUANTITATIVE & DILUTION / CONCENTRATION ARITHMETIC CHECK ==")
    for loc, clean in body_lines:
        vols = re.findall(r'(\d+(?:\.\d+)?)\s*(?:\u00b5|u|\u03bc)L\b', clean)
        pcts = re.findall(r'(\d+(?:\.\d+)?)\s*%', clean)
        if len(vols) >= 2 and pcts:
            print(f"  [Recipe check {loc}]: {clean}")
            v_floats = [float(v) for v in vols]
            total_v = max(v_floats)
            for v in v_floats:
                if v < total_v:
                    calc_pct = (v / total_v) * 100.0
                    print(f"     -> Calculated: {v} µL / {total_v} µL = {calc_pct:.4g}% v/v (stated % in sentence: {pcts}%)")

    dmso_pcts = []
    time_windows = []
    mois = []
    n_vals = []
    for loc, clean in body_lines:
        for m in re.finditer(r'(\d+(?:\.\d+)?%\s*(?:of\s+)?DMSO)', clean, re.I):
            dmso_pcts.append((loc, m.group(1)))
        for m in re.finditer(r'(\b(?:between|from|at)\s+\d+(?::\d+|\.\d+)?\s*(?:to|and|\u2013|-)\s*\d+(?::\d+|\.\d+)?\s*hpi\b|\bbetween\s+\d+:\d+\s*hpi\b)', clean, re.I):
            time_windows.append((loc, m.group(1)))
        for m in re.finditer(r'\bMOI\s+(?:of\s+)?\d+(?:\s*(?:to|\u2013|-)\s*\d+)?', clean, re.I):
            mois.append((loc, m.group(0)))
        for m in re.finditer(r'\bn\s*=\s*\d+|\b\d+\s+(?:biological|independent)\s+replicates\b', clean, re.I):
            n_vals.append((loc, m.group(0)))

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
    else:
        print("  !! WARNING: No explicit sample sizes ('n = ...' or 'X biological/independent replicates') detected anywhere in the text!")

    print("\n== 5. MANDATORY JOURNAL DECLARATION SECTIONS CHECK ==")
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

    if reb_path:
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

        print("\n  6b. Future-tense / conditional / draft promises in Rebuttal (verify if fulfilled):")
        draft_pat = re.compile(
            r'\b(?:will\s+(?:change|rewrite|add|include|update|revise|modify|remove)|'
            r'can\s+re-?analyze|would\s+be\s+happy\s+to\s+include|we\s+plan\s+to)\b',
            re.I,
        )
        for i, rl in enumerate(reb_lines, 1):
            ptag, clean = strip_ptag(rl)
            loc = ptag or f"Reb-L{i}"
            for m in draft_pat.finditer(clean):
                print(f"    !! {loc}: draft/future phrasing '{m.group(0)}' in: {clean}")

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

        print("\n  6d. Figure, Table & Video callouts in Rebuttal (side-by-side vs Manuscript Legends):")
        for i, rl in enumerate(reb_lines, 1):
            ptag, clean = strip_ptag(rl)
            loc = ptag or f"Reb-L{i}"
            for m in re.finditer(r'\b(?:(?:Supplementary|Supplemental|Expanded\s+View|Extended\s+Data)\s+)?(?:Figures?|Figs?\.?|Tables?|Videos?|Movies?)\s+(?:EV|ED|S)?\d+[A-Za-z]?\b', clean, re.I):
                cited_raw = m.group(0)
                norm_k = normalize_callout_key(cited_raw)
                ms_leg = legend_map.get(norm_k) if norm_k else None
                print(f"    - {loc}: cites '{cited_raw}' in: ...{clean[max(0, m.start()-25):m.end()+55]}...")
                if ms_leg:
                    print(f"        -> Manuscript legend: {ms_leg}")
                elif norm_k and legend_map:
                    print(f"        !! WARNING: '{norm_k}' is NOT defined in manuscript legends (defined: {', '.join(sorted(legend_map.keys()))})")

    if cov_path:
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


if __name__ == '__main__':
    main()
