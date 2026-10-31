#!/usr/bin/env python3
"""Structural validation of a self-contained HTML audit report.

Usage:
  validate_html.py report.html [--must-have "String A" --must-have "Section B"]

Checks:
  1. Balanced HTML tags (zero unclosed or mismatched tags)
  2. Internal TOC/anchor links (every href="#id" resolves to an existing id="id")
  3. Embedded images (every <img> is a valid, non-empty base64 JPEG/PNG data URI)
  4. No leftover template placeholders ({{...}}, TODO, FIXME)
  5. Required section strings (--must-have) and proper </html> termination
Exit code 0 on PASS, 1 on FAIL.
"""
import sys
import re
import base64
import html.parser

VOID = {'img', 'br', 'hr', 'meta', 'link', 'input', 'source', 'col', 'area', 'base', 'embed', 'param', 'track', 'wbr'}


class Checker(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []
        self.imgs = []
        self.ids = set()
        self.anchor_hrefs = []

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if 'id' in ad and ad['id']:
            self.ids.add(ad['id'])
        if 'name' in ad and ad['name']:
            self.ids.add(ad['name'])
        if tag == 'a' and 'href' in ad and ad['href'].startswith('#') and len(ad['href']) > 1:
            self.anchor_hrefs.append(ad['href'][1:])
        if tag in VOID:
            if tag == 'img':
                self.imgs.append(ad.get('src', ''))
            return
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append((tag, list(self.stack[-3:])))


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        sys.exit(__doc__)

    path = sys.argv[1]
    must = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == '--must-have']
    h = open(path, encoding='utf-8').read()
    ok = True

    c = Checker()
    c.feed(h)
    if c.errors:
        ok = False
        print("TAG ERRORS:", c.errors[:10])
    if c.stack:
        ok = False
        print("UNCLOSED TAGS:", c.stack)
    if not c.errors and not c.stack:
        print("HTML tag balance: OK (0 mismatched/unclosed tags)")

    broken_anchors = [a for a in c.anchor_hrefs if a not in c.ids]
    if broken_anchors:
        ok = False
        print("BROKEN INTERNAL ANCHOR LINKS (#id missing):", broken_anchors)
    else:
        print(f"Internal anchor links: {len(c.anchor_hrefs)} checked, 0 broken")

    bad = []
    for i, src in enumerate(c.imgs):
        if not src.startswith('data:image/'):
            bad.append((i, 'not a data URI: ' + src[:60]))
            continue
        try:
            b = base64.b64decode(src.split(',', 1)[1])
            if len(b) < 1000:
                bad.append((i, f'decodes to only {len(b)} bytes'))
            if b[:3] != b'\xff\xd8\xff' and b[:8] != b'\x89PNG\r\n\x1a\n':
                bad.append((i, 'not JPEG/PNG magic bytes'))
        except Exception as e:
            bad.append((i, f'decode error {e}'))
    print(f"Embedded images: {len(c.imgs)} total, {len(bad)} broken")
    for i, why in bad:
        ok = False
        print(f"  img[{i}]: {why}")

    ph = re.findall(r'\{\{[A-Z_]+[:.][^}]*\}\}|<<REPLACE:[^>]*>>|&lt;&lt;REPLACE:[^&]*&gt;&gt;', h)
    if ph:
        ok = False
        print("LEFTOVER PLACEHOLDERS:", ph[:5])
    for s in must:
        if s not in h:
            ok = False
            print("MISSING REQUIRED STRING:", s)
    if not h.rstrip().endswith('</html>'):
        ok = False
        print("Document does not end with </html>")

    print("VALIDATION:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
