#!/usr/bin/env python3
"""Quote & number fidelity check for a generated HTML audit report.

Usage:
  verify_quotes.py REPORT.html SRC1.txt [SRC2.txt ...]

For every <blockquote ...> in the report, finds maximal word-run matches against
the concatenated source texts and reports any gaps (words inside blockquotes that
do NOT appear in the sources). Gaps must be your own editorial bracketed notes or
marked elisions — a gap containing source-looking prose means a mis-quote.

Also lists distinctive multi-digit or decimal numbers in the report that do not
appear in any source text (derived values you computed are expected; anything else
indicates a transcription error).
"""
import sys
import re
import html as _html


def norm(t):
    t = _html.unescape(t)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = (
        t.replace('\u2019', "'").replace('\u2018', "'")
        .replace('\u201c', '"').replace('\u201d', '"')
        .replace('\u2206', 'delta').replace('\u0394', 'delta')
        .replace('\u03bc', 'u').replace('\u00b5', 'u')
        .replace('\u00a0', ' ')
    )
    t = re.sub(r'[^a-z0-9 ]', ' ', t.lower())
    return re.sub(r'\s+', ' ', t).strip()


def load_clean_src(path):
    lines = []
    for ln in open(path, encoding='utf-8', errors='replace'):
        if ln.startswith('# MANUSCRIPT-AUDIT EXTRACT'):
            continue
        ln = re.sub(r'^\s*\[(?:P\d+(?:\.\d+)?|T\d+|TABLE\s+\d+|Row\s+\d+)\]\s*', '', ln)
        lines.append(ln)
    return norm(' '.join(lines))


def main():
    if len(sys.argv) < 3 or sys.argv[1] in ('-h', '--help'):
        sys.exit(__doc__)

    rep_raw = open(sys.argv[1], encoding='utf-8').read()
    src = ' '.join(load_clean_src(p) for p in sys.argv[2:])
    MIN = 4  # words needed to anchor a run as coming from the source

    print("== 1. BLOCKQUOTE FIDELITY (gaps = words inside quotes not found in sources) ==")
    clean = re.sub(r'<(?:div|span)\s+class="(?:src|quote-loc)"[^>]*>.*?</(?:div|span)>', ' ', rep_raw, flags=re.S | re.I)
    blocks = re.findall(r'<blockquote[^>]*>(.*?)</blockquote>', clean, flags=re.S | re.I)
    if not blocks:
        print("  (no <blockquote> elements found — nothing checked)")
    ok_runs = 0
    gap_count = 0
    for idx, b in enumerate(blocks, 1):
        words = norm(b).split()
        i, gaps = 0, []
        while i < len(words):
            j, best = i, i - 1
            while j < len(words) and ' '.join(words[i:j + 1]) in src:
                best, j = j, j + 1
            if best - i + 1 >= MIN:
                ok_runs += 1
                i = best + 1
            else:
                k = i + 1
                while k < len(words) and not any(
                    ' '.join(words[k:k + L]) in src for L in (MIN, MIN - 1)
                ):
                    k += 1
                gap = ' '.join(words[i:k])
                if len(gap.split()) >= 3:
                    gaps.append(gap)
                i = max(k, i + 1)
        if gaps:
            gap_count += len(gaps)
            print(f"  [Quote #{idx}] GAP: {' | '.join(gaps)[:220]}")
    print(f"  Summary: {len(blocks)} blockquotes checked, {ok_runs} verified source runs, {gap_count} gaps flagged.")
    print("  (Editorial notes like [Cited in P22] or Reviewer labels are OK; verify any prose gaps.)")

    print("\n== 2. DISTINCTIVE NUMBERS IN REPORT NOT FOUND IN SOURCES (INFO ONLY — non-blocking) ==")
    body = re.sub(r'<style.*?</style>|<head.*?</head>|<script.*?</script>', ' ', clean, flags=re.S | re.I)
    body = re.sub(r'<[^>]*>', ' ', body)
    body = _html.unescape(body)
    # Strip explicitly marked derived/meta numbers (e.g. ~250, ≈600, lines 120-135, [P127], 200 dpi, 1400 px)
    body_filtered = re.sub(
        r'(?:[~≈]\s*\d+(?:[.,]\d+)?|'
        r'\b(?:lines?|para|p|ref|refs|page|pages|step)\s*\d+(?:\s*[\u2013\u2014-]\s*\d+)?|'
        r'\b\d+\s*(?:px|pt|dpi)\b)',
        ' ',
        body,
        flags=re.I,
    )
    norm_texty = norm(body_filtered)
    nums = set(re.findall(r'\b\d+(?:[.,]\d+)+\b|\b\d{3,}\b', norm_texty))
    untraced = []
    for n in nums:
        if re.fullmatch(r'(?:19|20)\d\d', n):
            continue
        flat = n.replace(',', '')
        if n in src or flat in src or flat.replace('.', '') in src:
            continue
        untraced.append(n)
    print("  " + (', '.join(sorted(untraced)) if untraced else "none"))
    print("  (Computed sums, physics/scale calculations, or line numbers are expected; prefix derived values with '~' or '≈'.)")

    if gap_count == 0:
        print("\nQUOTE VERIFICATION: PASS (0 verbatim blockquote gaps)")
    else:
        print(f"\nQUOTE VERIFICATION: REVIEW NEEDED ({gap_count} blockquote gap(s) listed above)")


if __name__ == '__main__':
    main()
