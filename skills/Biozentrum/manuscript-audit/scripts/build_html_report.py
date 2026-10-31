#!/usr/bin/env python3
"""Compile a structured JSON audit specification into a self-contained HTML report.

Designed so that any LLM agent (including smaller/local models in OpenCode, Aider, etc.)
can produce a valid, responsive, self-contained HTML audit report without having to write
raw HTML/CSS/JS or manually base64-encode images.

Key Guardrails for Smaller Models:
  - Automatic Verbatim Quote Lookup: If a finding sets `"quote_para": "P38"` (or
    `"Rebuttal:P7"`), this script automatically fetches the exact verbatim paragraph
    from `--manuscript-txt` or `--rebuttal-txt`, guaranteeing 0 quote verification errors!
  - Automatic Base64 Image Embedding: Any file path in `"images"` is automatically
    encoded as a `data:image/...;base64,...` URI.

Usage:
  build_html_report.py --init-template report_spec.json
  build_html_report.py report_spec.json manuscript_review_report.html [--manuscript-txt manuscript_indexed.txt] [--rebuttal-txt rebuttal_indexed.txt]
"""
import sys
import os
import re
import html
import json
import base64


TEMPLATE_JSON = {
    "title": "Pre-Submission / Resubmission Manuscript Audit Report",
    "subtitle": "<<REPLACE: Manuscript Title>>",
    "mode": "<<REPLACE: Resubmission Mode or First Submission Mode>>",
    "files_audited": [
        "<<REPLACE: List actual manuscript .docx, .pdf, .xlsx, and video files audited>>"
    ],
    "summary_table": [
        {
            "area": "1. Bibliography & In-Text Citations",
            "status": "ISSUES FOUND",
            "summary": "<<REPLACE: Brief summary of reference/citation issues found.>>"
        },
        {
            "area": "2. Figure, Table, Video & Methods Callouts",
            "status": "ISSUES FOUND",
            "summary": "<<REPLACE: Brief summary of callout order, missing callouts, or orphan Methods.>>"
        },
        {
            "area": "3. Figure Legends vs. Visual Content & Quantitative Data",
            "status": "CRITICAL ISSUES",
            "summary": "<<REPLACE: Brief summary of figure/graph/scale-bar/sample-size findings.>>"
        }
    ],
    "sections": [
        {
            "id": "sec-1",
            "title": "1. Bibliography & In-Text Citations Audit",
            "intro": "Audit of numbered bibliography entries, in-text citation sequence, and topical accuracy.",
            "findings": [
                {
                    "id": "F1.1",
                    "severity": "CRITICAL",
                    "category": "Mismatched Citation",
                    "title": "<<REPLACE: Short title of the finding>>",
                    "location": "Main Text [P13], Introduction",
                    "quote_para": "P13",
                    "verbatim_quote": "",
                    "issue": "<<REPLACE: Detailed explanation of the discrepancy.>>",
                    "fix": "<<REPLACE: Exact drop-in replacement text or action required.>>",
                    "images": []
                }
            ]
        },
        {
            "id": "sec-2",
            "title": "2. Panel-by-Panel Figure & Quantitative Audit",
            "intro": "Visual and quantitative verification of every main and supplementary figure panel.",
            "findings": [
                {
                    "id": "F2.1",
                    "severity": "CRITICAL",
                    "category": "Data / Bar Graph Swap",
                    "title": "<<REPLACE: Example figure finding with embedded cropped image>>",
                    "location": "Figure 5C & Main Text [P38]",
                    "quote_para": "P38",
                    "issue": "<<REPLACE: Explanation of visual/quantitative discrepancy.>>",
                    "fix": "<<REPLACE: Concrete instruction to fix the figure and/or legend.>>",
                    "images": []
                }
            ]
        }
    ],
    "verified_items": [
        "<<REPLACE: List verified and consistent items across the manuscript>>"
    ]
}


CSS_STYLES = """
:root {
  --bg: #f8fafc;
  --card: #ffffff;
  --text: #0f172a;
  --muted: #475569;
  --border: #e2e8f0;
  --crit-bg: #fef2f2; --crit-bd: #dc2626; --crit-tx: #991b1b;
  --maj-bg: #fffbeb;  --maj-bd: #d97706;  --maj-tx: #92400e;
  --mod-bg: #eff6ff;  --mod-bd: #2563eb;  --mod-tx: #1e40af;
  --min-bg: #f1f5f9;  --min-bd: #64748b;  --min-tx: #334155;
  --ok-bg: #f0fdf4;   --ok-bd: #16a34a;   --ok-tx: #166534;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); margin: 0; padding: 24px; line-height: 1.55;
}
.container { max-width: 1180px; margin: 0 auto; }
header.hero {
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
  color: #fff; padding: 28px 32px; border-radius: 12px; margin-bottom: 24px;
}
header.hero h1 { margin: 0 0 8px 0; font-size: 1.65rem; }
header.hero p.sub { margin: 0 0 14px 0; color: #cbd5e1; font-size: 1.02rem; }
.meta-pills { display: flex; flex-wrap: wrap; gap: 8px; }
.meta-pill {
  background: rgba(255,255,255,0.12); padding: 4px 10px; border-radius: 999px; font-size: 0.82rem;
}
.kpi-row {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px;
}
.kpi-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px;
  text-align: center; box-shadow: 0 1px 2px rgba(0,0,0,0.03);
}
.kpi-num { font-size: 1.75rem; font-weight: 700; }
.kpi-lbl { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
.toc-bar, .filter-bar {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 16px; margin-bottom: 20px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
}
.toc-bar a {
  text-decoration: none; color: #1e40af; background: #eff6ff; padding: 5px 11px;
  border-radius: 6px; font-size: 0.86rem; font-weight: 500;
}
.toc-bar a:hover { background: #dbeafe; }
.filter-btn {
  border: 1px solid var(--border); background: #fff; padding: 6px 12px; border-radius: 6px;
  cursor: pointer; font-size: 0.85rem; font-weight: 600;
}
.filter-btn.active { background: #0f172a; color: #fff; border-color: #0f172a; }
table.summary-tbl {
  width: 100%; border-collapse: collapse; background: var(--card); border-radius: 10px;
  overflow: hidden; border: 1px solid var(--border); margin-bottom: 28px;
}
table.summary-tbl th, table.summary-tbl td {
  padding: 11px 14px; border-bottom: 1px solid var(--border); text-align: left; font-size: 0.92rem;
}
table.summary-tbl th { background: #f1f5f9; font-weight: 600; }
section.audit-sec { margin-bottom: 34px; }
section.audit-sec h2 {
  font-size: 1.3rem; border-bottom: 2px solid #cbd5e1; padding-bottom: 8px; margin-bottom: 12px;
}
.finding-card {
  background: var(--card); border: 1px solid var(--border); border-left: 5px solid var(--mod-bd);
  border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.finding-card[data-sev="CRITICAL"] { border-left-color: var(--crit-bd); }
.finding-card[data-sev="MAJOR"]    { border-left-color: var(--maj-bd); }
.finding-card[data-sev="MODERATE"] { border-left-color: var(--mod-bd); }
.finding-card[data-sev="MINOR"]    { border-left-color: var(--min-bd); }
.finding-card[data-sev="VERIFIED"] { border-left-color: var(--ok-bd); }
.f-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
.badge {
  display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 0.75rem; font-weight: 700;
  text-transform: uppercase;
}
.badge.CRITICAL { background: var(--crit-bg); color: var(--crit-tx); border: 1px solid var(--crit-bd); }
.badge.MAJOR    { background: var(--maj-bg);  color: var(--maj-tx);  border: 1px solid var(--maj-bd); }
.badge.MODERATE { background: var(--mod-bg);  color: var(--mod-tx);  border: 1px solid var(--mod-bd); }
.badge.MINOR    { background: var(--min-bg);  color: var(--min-tx);  border: 1px solid var(--min-bd); }
.badge.VERIFIED { background: var(--ok-bg);   color: var(--ok-tx);   border: 1px solid var(--ok-bd); }
.f-title { font-weight: 700; font-size: 1.05rem; }
.f-loc { margin-left: auto; font-size: 0.83rem; color: var(--muted); font-family: monospace; }
blockquote.verbatim {
  margin: 10px 0; padding: 10px 14px; background: #f8fafc; border-left: 3px solid #94a3b8;
  font-size: 0.9rem; color: #334155; border-radius: 0 6px 6px 0;
}
.f-fix {
  margin-top: 10px; padding: 10px 14px; background: var(--ok-bg); border-left: 3px solid var(--ok-bd);
  border-radius: 0 6px 6px 0; font-size: 0.92rem;
}
.img-grid {
  display: flex; flex-wrap: wrap; gap: 14px; margin-top: 12px;
}
.img-card {
  border: 1px solid var(--border); border-radius: 8px; padding: 8px; background: #fff; max-width: 100%;
}
.img-card img { max-width: 100%; height: auto; display: block; border-radius: 4px; }
.img-cap { font-size: 0.82rem; color: var(--muted); margin-top: 6px; }
ul.verified-list li { margin-bottom: 6px; }
"""


JS_SCRIPT = """
function filterFindings(sev, btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.finding-card').forEach(card => {
    if (sev === 'ALL' || card.getAttribute('data-sev') === sev) {
      card.style.display = 'block';
    } else {
      card.style.display = 'none';
    }
  });
}
"""


def load_indexed_paragraphs(txt_path):
    """Parse `[P12] text...`, `[P1.2] text...`, or `[Row 1] text...` lines into a dict `{'P12': 'text...'}`."""
    paras = {}
    if not txt_path or not os.path.isfile(txt_path):
        return paras
    cur_tbl = None
    seen_parent_sub = set()
    with open(txt_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            raw_ln = line.rstrip("\r\n")
            s = raw_ln.strip()
            if s.startswith("# MANUSCRIPT-AUDIT EXTRACT"):
                continue
            m_tbl = re.match(r"^\[TABLE\s+(\d+)\]$", raw_ln)
            if m_tbl:
                cur_tbl = f"T{m_tbl.group(1)}"
                continue
            # Require [P#] / [T#] at column 0, or [Row #] with <=2 leading spaces (never indented log echoes)
            m = re.match(r"^(?:\[(P\d+(?:\.\d+)?|T\d+)\]|\s{0,2}\[(Row\s+\d+)\])\s*(.*)$", raw_ln)
            if m:
                key = m.group(1) or m.group(2)
                val = re.sub(r"\[\[IMAGE:[^\]]+\]\]\s*", "", m.group(3)).strip()
                is_first_for_key = key not in paras
                paras.setdefault(key, val)
                # If key is a soft-break sub-anchor like P1.2, also accumulate parent P1 on first pass
                if is_first_for_key and "." in key and key.startswith("P"):
                    parent_p = key.split(".", 1)[0]
                    if parent_p in seen_parent_sub:
                        paras[parent_p] += " " + val
                    else:
                        paras.setdefault(parent_p, val)
                        seen_parent_sub.add(parent_p)
                elif key.startswith("Row") and cur_tbl:
                    rnum = key.split()[1]
                    for rk in (f"{cur_tbl}.R{rnum}", f"{cur_tbl}R{rnum}", f"{cur_tbl}:R{rnum}", f"TABLE {cur_tbl[1:]} R{rnum}"):
                        paras.setdefault(rk, val)
                    for tk in (cur_tbl, f"TABLE {cur_tbl[1:]}"):
                        if tk not in paras:
                            paras[tk] = val
                        elif len(paras[tk]) < 600:
                            paras[tk] += " | " + val
    return paras


def encode_image_data_uri(img_path):
    if not os.path.isfile(img_path):
        sys.exit(f"ERROR: Image file not found for HTML embedding: {img_path}")
    ext = os.path.splitext(img_path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    b64 = base64.b64encode(open(img_path, "rb").read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


_SAFE_TAG_RE = re.compile(
    r"&lt;(/?(?:b|strong|i|em|code|sub|sup|del|ins|u|mark|br\s*/?))&gt;",
    re.I,
)


def format_rich_text(text):
    """HTML-escape `text` while preserving safe inline emphasis tags (<em>, <b>, <code>, <sub>, <sup>)
    and converting Markdown **bold** and `code` spans."""
    if not text:
        return ""
    s = html.escape(str(text))
    # Restore safe inline HTML tags
    s = _SAFE_TAG_RE.sub(lambda m: f"<{m.group(1).lower()}>", s)
    # Convert Markdown `code` and **bold**
    s = re.sub(r"`([^`<>]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*<>]+)\*\*", r"<strong>\1</strong>", s)
    return s


_SEV_ALIASES = {
    "HIGH": "MAJOR",
    "MEDIUM": "MODERATE",
    "MED": "MODERATE",
    "LOW": "MINOR",
    "INFO": "MINOR",
    "NIT": "MINOR",
    "COSMETIC": "MINOR",
    "BLOCKER": "CRITICAL",
    "FATAL": "CRITICAL",
    "SEVERE": "CRITICAL",
}


def normalize_finding(f, idx_fallback=""):
    """Normalize field aliases on a finding dict so both template field names and common shorthand work."""
    if not isinstance(f, dict):
        return {}
    out = dict(f)
    out["id"] = str(f.get("id") or idx_fallback or "")
    raw_sev = str(f.get("severity") or f.get("sev") or f.get("level") or "MODERATE").upper().strip()
    out["severity"] = _SEV_ALIASES.get(raw_sev, raw_sev)
    out["category"] = str(f.get("category") or f.get("cat") or f.get("kind") or f.get("type") or "")
    out["title"] = str(f.get("title") or f.get("heading") or f.get("name") or "Audit Finding")
    out["location"] = str(f.get("location") or f.get("loc") or f.get("where") or "")

    qp = f.get("quote_para") if "quote_para" in f else (f.get("para_id") or f.get("pid") or f.get("para") or "")
    if isinstance(qp, (int, float)):
        qp = f"P{qp}"
    elif isinstance(qp, str):
        qp = qp.strip()
        if qp.startswith("[") and qp.endswith("]"):
            qp = qp[1:-1].strip()
        elif re.match(r"^\d+(?:\.\d+)?$", qp):
            qp = f"P{qp}"
    out["quote_para"] = qp

    out["verbatim_quote"] = str(f.get("verbatim_quote") or f.get("quote") or f.get("excerpt") or "")
    out["issue"] = str(
        f.get("issue") or f.get("explanation") or f.get("description")
        or f.get("problem") or f.get("detail") or f.get("details") or ""
    )
    out["fix"] = str(
        f.get("fix") or f.get("suggested_fix") or f.get("recommendation")
        or f.get("replacement") or f.get("action") or f.get("verdict") or ""
    )

    raw_imgs = f.get("images") or ([] if not f.get("image") else [f.get("image")])
    if not raw_imgs and f.get("img"):
        raw_imgs = [{"path": f.get("img"), "caption": f.get("img_caption") or f.get("caption") or ""}]
    norm_imgs = []
    for im in raw_imgs:
        if isinstance(im, str) and im.strip():
            norm_imgs.append({"path": im.strip(), "caption": str(f.get("img_caption") or "")})
        elif isinstance(im, dict):
            p = im.get("path") or im.get("src") or im.get("file") or im.get("img") or ""
            c = im.get("caption") or im.get("img_caption") or im.get("label") or im.get("title") or ""
            if p:
                norm_imgs.append({"path": p, "caption": c})
    out["images"] = norm_imgs
    return out


def normalize_report_spec(spec):
    """Normalize top-level report_spec.json structure and field aliases."""
    if not isinstance(spec, dict):
        sys.exit("ERROR: Root of report_spec.json must be a JSON object {...}.")
    out = dict(spec)

    # Support flat top-level `findings` if `sections` is missing or empty
    sections = out.get("sections") or []
    if not sections and out.get("findings"):
        sections = [{
            "id": "sec-1",
            "title": "1. Manuscript Audit Findings",
            "intro": "",
            "findings": out["findings"],
        }]

    norm_sections = []
    for s_idx, sec in enumerate(sections, 1):
        if not isinstance(sec, dict):
            continue
        nsec = dict(sec)
        nsec["id"] = str(sec.get("id") or f"sec-{s_idx}")
        nsec["title"] = str(sec.get("title") or sec.get("name") or f"Section {s_idx}")
        nsec["intro"] = str(sec.get("intro") or sec.get("description") or "")
        nsec["findings"] = [
            normalize_finding(f, f"F{s_idx}.{f_i}")
            for f_i, f in enumerate(sec.get("findings", []), 1)
        ]
        norm_sections.append(nsec)
    out["sections"] = norm_sections

    # Normalize verified_items (accept strings or dicts like {"kind": ..., "verdict": ...})
    raw_ver = (
        out.get("verified_items")
        or out.get("verified")
        or out.get("what_held_up")
        or out.get("checks_passed")
        or []
    )
    norm_ver = []
    for v in raw_ver:
        if isinstance(v, str):
            norm_ver.append(v)
        elif isinstance(v, dict):
            head = v.get("category") or v.get("kind") or v.get("title") or v.get("area") or ""
            body = v.get("detail") or v.get("verdict") or v.get("summary") or v.get("text") or ""
            if head and body:
                norm_ver.append(f"**{head}:** {body}")
            elif head or body:
                norm_ver.append(head or body)
    out["verified_items"] = norm_ver
    return out


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def _opt_all(name):
    out = []
    args = sys.argv[1:]
    for idx, a in enumerate(args):
        if a == name and idx + 1 < len(args):
            out.append(args[idx + 1])
    return out


def load_extra_para_maps(cover_txt=None, extra_txts=None):
    """Load prefix -> paragraph dict map for `Cover:P3`, `Supp:P12`, etc."""
    prefix_map = {}
    if not cover_txt:
        for cand in ("scratch_audit/cover_indexed.txt", "cover_indexed.txt"):
            if os.path.isfile(cand):
                cover_txt = cand
                break
    if cover_txt and os.path.isfile(cover_txt):
        prefix_map["cover"] = load_indexed_paragraphs(cover_txt)
        prefix_map["cover_letter"] = prefix_map["cover"]

    # Auto-discover supplementary and companion *_indexed.txt files in scratch_audit/
    if os.path.isdir("scratch_audit"):
        for fn in sorted(os.listdir("scratch_audit")):
            if fn.endswith("_indexed.txt") and fn not in ("manuscript_indexed.txt", "rebuttal_indexed.txt", "cover_indexed.txt"):
                full_p = os.path.join("scratch_audit", fn)
                stem = re.sub(r"_indexed\.txt$", "", fn.lower())
                pmap = load_indexed_paragraphs(full_p)
                prefix_map.setdefault(stem, pmap)
                if stem.startswith("supp"):
                    prefix_map.setdefault("supp", pmap)
                    prefix_map.setdefault("supplementary", pmap)

    for item in (extra_txts or []):
        if "=" in item:
            pfx, path = item.split("=", 1)
            if os.path.isfile(path):
                prefix_map[pfx.strip().lower()] = load_indexed_paragraphs(path)
        elif os.path.isfile(item):
            stem = re.sub(r"_indexed$", "", os.path.splitext(os.path.basename(item))[0].lower())
            prefix_map[stem] = load_indexed_paragraphs(item)
    return prefix_map


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)

    if sys.argv[1] == "--init-template":
        out_json = sys.argv[2]
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(TEMPLATE_JSON, f, indent=2, ensure_ascii=False)
        print(f"Wrote starter JSON template to {out_json}")
        return

    spec_path = opt("--spec")
    out_html = opt("--out")
    if not spec_path or not out_html:
        pos_args = []
        skip_next = False
        for a in sys.argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if a in ("--spec", "--out", "--manuscript-txt", "--rebuttal-txt", "--cover-txt", "--extra-txt"):
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            pos_args.append(a)
        if not spec_path and len(pos_args) >= 1:
            spec_path = pos_args[0]
        if not out_html and len(pos_args) >= 2:
            out_html = pos_args[1]
    if not spec_path or not out_html:
        sys.exit(__doc__)

    ms_txt_p = opt("--manuscript-txt")
    if not ms_txt_p and os.path.isfile("scratch_audit/manuscript_indexed.txt"):
        ms_txt_p = "scratch_audit/manuscript_indexed.txt"
    reb_txt_p = opt("--rebuttal-txt")
    if not reb_txt_p and os.path.isfile("scratch_audit/rebuttal_indexed.txt"):
        reb_txt_p = "scratch_audit/rebuttal_indexed.txt"

    ms_paras = load_indexed_paragraphs(ms_txt_p)
    reb_paras = load_indexed_paragraphs(reb_txt_p)
    extra_para_maps = load_extra_para_maps(opt("--cover-txt"), _opt_all("--extra-txt"))

    raw_spec = json.load(open(spec_path, encoding="utf-8"))
    spec = normalize_report_spec(raw_spec)

    # Count findings by severity
    counts = {"CRITICAL": 0, "MAJOR": 0, "MODERATE": 0, "MINOR": 0}
    for sec in spec.get("sections", []):
        for f in sec.get("findings", []):
            sev = f.get("severity", "MODERATE").upper()
            counts[sev] = counts.get(sev, 0) + 1
    total_findings = sum(counts.values())

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(spec.get('title', 'Manuscript Audit Report'))}</title>",
        f"<style>{CSS_STYLES}</style>",
        "</head>",
        "<body>",
        '<div class="container">',
        '<header class="hero">',
        f"<h1>{html.escape(spec.get('title', 'Manuscript Audit Report'))}</h1>",
        f'<p class="sub">{html.escape(spec.get("subtitle", ""))}</p>',
        '<div class="meta-pills">',
        f'<span class="meta-pill">Mode: {html.escape(spec.get("mode", "Audit"))}</span>',
    ]
    for fa in spec.get("files_audited", []):
        parts.append(f'<span class="meta-pill">{html.escape(fa)}</span>')
    parts.append("</div></header>")

    # KPI Row
    parts.extend([
        '<div class="kpi-row">',
        f'<div class="kpi-card"><div class="kpi-num">{total_findings}</div><div class="kpi-lbl">Total Findings</div></div>',
        f'<div class="kpi-card"><div class="kpi-num" style="color:#dc2626">{counts.get("CRITICAL", 0)}</div><div class="kpi-lbl">Critical</div></div>',
        f'<div class="kpi-card"><div class="kpi-num" style="color:#d97706">{counts.get("MAJOR", 0)}</div><div class="kpi-lbl">Major</div></div>',
        f'<div class="kpi-card"><div class="kpi-num" style="color:#2563eb">{counts.get("MODERATE", 0)}</div><div class="kpi-lbl">Moderate</div></div>',
        f'<div class="kpi-card"><div class="kpi-num" style="color:#64748b">{counts.get("MINOR", 0)}</div><div class="kpi-lbl">Minor</div></div>',
        "</div>",
    ])

    # TOC Bar
    parts.append('<nav class="toc-bar"><strong>Sections:</strong>')
    for sec in spec.get("sections", []):
        sid = html.escape(sec["id"])
        n_sec_f = len(sec.get("findings", []))
        stitle = html.escape(f"{sec['title']} ({n_sec_f})")
        parts.append(f'<a href="#{sid}">{stitle}</a>')
    if spec.get("verified_items"):
        parts.append('<a href="#sec-verified">Verified &amp; Consistent Items</a>')
    parts.append("</nav>")

    # Filter Bar
    parts.extend([
        '<div class="filter-bar"><strong>Filter by Severity:</strong>',
        f'<button class="filter-btn active" onclick="filterFindings(\'ALL\', this)">All ({total_findings})</button>',
        f'<button class="filter-btn" onclick="filterFindings(\'CRITICAL\', this)">Critical ({counts.get("CRITICAL", 0)})</button>',
        f'<button class="filter-btn" onclick="filterFindings(\'MAJOR\', this)">Major ({counts.get("MAJOR", 0)})</button>',
        f'<button class="filter-btn" onclick="filterFindings(\'MODERATE\', this)">Moderate ({counts.get("MODERATE", 0)})</button>',
        f'<button class="filter-btn" onclick="filterFindings(\'MINOR\', this)">Minor ({counts.get("MINOR", 0)})</button>',
        "</div>",
    ])

    # Summary Table (auto-generated from sections if omitted from spec)
    summary_rows = spec.get("summary_table") or []
    if not summary_rows and spec.get("sections"):
        for sec in spec["sections"]:
            s_findings = sec.get("findings", [])
            s_counts = {}
            for sf in s_findings:
                sv = sf.get("severity", "MODERATE").upper()
                s_counts[sv] = s_counts.get(sv, 0) + 1
            status_str = (
                ", ".join(f"{s_counts[k]} {k.title()}" for k in ("CRITICAL", "MAJOR", "MODERATE", "MINOR") if s_counts.get(k))
                or "0 Findings (Pass)"
            )
            takeaway = "; ".join(sf.get("title", "") for sf in s_findings[:4])
            if len(s_findings) > 4:
                takeaway += f" (+{len(s_findings) - 4} more)"
            summary_rows.append({
                "area": sec.get("title", ""),
                "status": f"{len(s_findings)} total ({status_str})",
                "summary": takeaway or sec.get("intro", "No issues detected."),
            })

    if summary_rows:
        parts.extend([
            '<table class="summary-tbl">',
            "<thead><tr><th>Audit Dimension</th><th>Status</th><th>Key Takeaways</th></tr></thead><tbody>",
        ])
        for row in summary_rows:
            parts.append(
                f"<tr><td><strong>{format_rich_text(row.get('area', ''))}</strong></td>"
                f"<td>{format_rich_text(row.get('status', ''))}</td>"
                f"<td>{format_rich_text(row.get('summary', ''))}</td></tr>"
            )
        parts.append("</tbody></table>")

    # Sections & Findings
    for sec in spec.get("sections", []):
        sid = html.escape(sec["id"])
        n_sec_f = len(sec.get("findings", []))
        parts.append(f'<section class="audit-sec" id="{sid}">')
        parts.append(f"<h2>{html.escape(sec['title'])} <span style=\"font-size:0.9rem;color:#64748b;font-weight:500\">({n_sec_f} finding{'s' if n_sec_f != 1 else ''})</span></h2>")
        if sec.get("intro"):
            parts.append(f"<p>{format_rich_text(sec['intro'])}</p>")

        for f in sec.get("findings", []):
            sev = f.get("severity", "MODERATE").upper()
            fid = f.get("id", "")
            cat = f.get("category", "")
            title = f.get("title", "")
            loc = f.get("location", "")

            # Resolve verbatim quote automatically if quote_para is given
            quote_text = f.get("verbatim_quote", "").strip()
            qpara = f.get("quote_para", "").strip()
            if qpara and not quote_text:
                if ":" in qpara:
                    pfx, pid = qpara.split(":", 1)
                    pfx_low = pfx.strip().lower()
                    pid = pid.strip()
                    if pfx_low in ("rebuttal", "reb", "response"):
                        quote_text = reb_paras.get(pid, "")
                    elif pfx_low in extra_para_maps:
                        quote_text = extra_para_maps[pfx_low].get(pid, "")
                    else:
                        quote_text = ms_paras.get(pid, "")
                else:
                    quote_text = ms_paras.get(qpara, "")

            parts.append(f'<div class="finding-card" data-sev="{html.escape(sev)}">')
            parts.append('<div class="f-head">')
            parts.append(f'<span class="badge {html.escape(sev)}">{html.escape(sev)}</span>')
            if cat:
                parts.append(f'<span class="badge MINOR">{html.escape(cat)}</span>')
            parts.append(f'<span class="f-title">{html.escape(fid + (" — " if fid else "") + title)}</span>')
            if loc:
                parts.append(f'<span class="f-loc">{html.escape(loc)}</span>')
            parts.append("</div>")

            if quote_text:
                parts.append(f'<blockquote class="verbatim">{html.escape(quote_text)}</blockquote>')

            if f.get("issue"):
                parts.append(f'<p><strong>Issue:</strong> {format_rich_text(f["issue"])}</p>')
            if f.get("fix"):
                parts.append(f'<div class="f-fix"><strong>Suggested Fix:</strong> {format_rich_text(f["fix"])}</div>')

            if f.get("images"):
                parts.append('<div class="img-grid">')
                for img in f["images"]:
                    uri = encode_image_data_uri(img["path"])
                    cap = img.get("caption", "")
                    parts.append('<div class="img-card">')
                    parts.append(f'<img src="{uri}" alt="{html.escape(cap)}">')
                    if cap:
                        parts.append(f'<div class="img-cap">{format_rich_text(cap)}</div>')
                    parts.append("</div>")
                parts.append("</div>")

            parts.append("</div>")
        parts.append("</section>")

    # Verified Items
    if spec.get("verified_items"):
        parts.append('<section class="audit-sec" id="sec-verified">')
        parts.append("<h2>Verified &amp; Consistent Items</h2>")
        parts.append('<ul class="verified-list">')
        for item in spec["verified_items"]:
            parts.append(f"<li>{format_rich_text(item)}</li>")
        parts.append("</ul></section>")

    parts.append(f"<script>{JS_SCRIPT}</script>")
    parts.append("</div></body></html>")

    os.makedirs(os.path.dirname(os.path.abspath(out_html)), exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"Compiled self-contained HTML report: {out_html} ({total_findings} findings)")


if __name__ == "__main__":
    main()
