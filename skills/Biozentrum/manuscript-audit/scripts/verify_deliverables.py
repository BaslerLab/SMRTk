#!/usr/bin/env python3
"""Mandatory final-step on-disk deliverable manifest & structural verifier.

Prevents false completion claims by verifying on disk that:
  1. `MANUSCRIPT_AUDIT_REPORT.html` exists, is non-empty, passes `validate_html.py`
     (balanced HTML tags, valid base64/file `<img>` tags, zero `TODO`/`PLACEHOLDER` tokens),
     and (when `--source-text` is supplied) passes `verify_quotes.py`.
  2. Every `*_AUDITED.docx` exists on disk, is a valid ZIP/OOXML package with well-formed XML,
     and contains native Word tracked changes (`<w:del>` / `<w:ins>`) and/or margin comments (`<w:comment>`).
  3. Every `*_AUDITED.pdf` exists on disk, has a valid `%%EOF` trailer, and contains native PDF
     `/Subtype /Square` and/or `/Subtype /Text` annotations.
  4. When `--dir WORKSPACE` is passed, checks that every source `.docx` and figure `.pdf` in `WORKSPACE`
     has a corresponding `*_AUDITED.*` deliverable on disk.

Zero external dependencies (uses Python 3 standard library only).

Usage:
  verify_deliverables.py --dir . [--original-dir ../Original] [--source-text scratch_audit/manuscript_indexed.txt] [--no-counterpart Cover_letter.docx]
  verify_deliverables.py --html MANUSCRIPT_AUDIT_REPORT.html --docx Main_AUDITED.docx --pdf Figs_AUDITED.pdf [--source-text indexed.txt]
"""
import sys
import os
import re
import hashlib
import zipfile
import subprocess
from datetime import datetime
import xml.etree.ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def fmt_mtime(path):
    ts = os.path.getmtime(path)
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def check_html_deliverable(html_path, source_texts):
    """Verify HTML report exists, is non-empty, passes validate_html.py, and passes verify_quotes.py."""
    if not os.path.isfile(html_path):
        return False, "MISSING ON DISK", {}
    size = os.path.getsize(html_path)
    if size < 512:
        return False, f"EMPTY/TRUNCATED ({size} B)", {}

    val_script = os.path.join(SCRIPT_DIR, "validate_html.py")
    p_val = subprocess.run([sys.executable, val_script, html_path], capture_output=True, text=True)
    if p_val.returncode != 0:
        err_msg = (p_val.stdout + "\n" + p_val.stderr).strip().splitlines()[-1]
        return False, f"HTML VALIDATION FAILED: {err_msg}", {"size": size}

    # Count embedded images and quote blocks
    txt = open(html_path, encoding="utf-8", errors="replace").read()
    n_imgs = len(re.findall(r"<img\b", txt, re.I))
    n_quotes = len(re.findall(r"<blockquote\b|class=[\"']quote[\"']", txt, re.I))

    if source_texts:
        vq_script = os.path.join(SCRIPT_DIR, "verify_quotes.py")
        p_vq = subprocess.run([sys.executable, vq_script, html_path] + source_texts, capture_output=True, text=True)
        if p_vq.returncode != 0:
            err_msg = (p_vq.stdout + "\n" + p_vq.stderr).strip().splitlines()[-1]
            return False, f"QUOTE VERIFICATION FAILED: {err_msg}", {"size": size, "imgs": n_imgs, "quotes": n_quotes}

    detail = f"OK ({size:,} B, {n_imgs} <img>, {n_quotes} quote block(s), mtime={fmt_mtime(html_path)})"
    return True, detail, {"size": size, "imgs": n_imgs, "quotes": n_quotes}


def check_docx_deliverable(docx_path):
    """Verify *_AUDITED.docx exists, has valid OOXML, and contains tracked changes or comments."""
    if not os.path.isfile(docx_path):
        return False, "MISSING ON DISK", {}
    size = os.path.getsize(docx_path)
    if size < 1024:
        return False, f"EMPTY/TRUNCATED ({size} B)", {}

    try:
        with zipfile.ZipFile(docx_path, "r") as zf:
            doc_xml = zf.read("word/document.xml")
            root = ET.fromstring(doc_xml)
            n_del = len(list(root.iter(f"{{{W_NS}}}del")))
            n_ins = len(list(root.iter(f"{{{W_NS}}}ins")))
            n_comments = 0
            if "word/comments.xml" in zf.namelist():
                c_root = ET.fromstring(zf.read("word/comments.xml"))
                n_comments = len(list(c_root.iter(f"{{{W_NS}}}comment")))
    except Exception as e:
        return False, f"CORRUPT DOCX/XML: {e}", {}

    if n_del == 0 and n_ins == 0 and n_comments == 0:
        return False, "ZERO ANNOTATIONS (0 <w:del>, 0 <w:ins>, 0 <w:comment>)", {}

    detail = (
        f"OK ({size:,} B, {n_del} <w:del>, {n_ins} <w:ins>, {n_comments} <w:comment>, "
        f"mtime={fmt_mtime(docx_path)})"
    )
    return True, detail, {"size": size, "del": n_del, "ins": n_ins, "comments": n_comments}


def check_pdf_deliverable(pdf_path):
    """Verify *_AUDITED.pdf exists, has valid %%EOF, and contains native PDF annotations."""
    if not os.path.isfile(pdf_path):
        return False, "MISSING ON DISK", {}
    size = os.path.getsize(pdf_path)
    if size < 512:
        return False, f"EMPTY/TRUNCATED ({size} B)", {}

    data = open(pdf_path, "rb").read()
    if b"%%EOF" not in data[-1024:]:
        return False, "INVALID PDF TRAILER (missing %%EOF)", {}

    n_square = len(re.findall(rb"/Subtype\s*/Square\b", data))
    n_text = len(re.findall(rb"/Subtype\s*/Text\b", data))
    n_popup = len(re.findall(rb"/Subtype\s*/Popup\b", data))
    if n_square == 0 and n_text == 0:
        return False, "ZERO PDF ANNOTATIONS (0 /Square, 0 /Text)", {}

    detail = (
        f"OK ({size:,} B, {n_square} /Square box(es), {n_text} /Text sticky note(s), "
        f"mtime={fmt_mtime(pdf_path)})"
    )
    return True, detail, {"size": size, "square": n_square, "text": n_text, "popup": n_popup}


def collect_flag_values(flag):
    """Collect one or more positional arguments following `flag` until the next `--` flag."""
    vals = []
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == flag:
            i += 1
            while i < len(sys.argv) and not sys.argv[i].startswith("--"):
                vals.append(sys.argv[i])
                i += 1
        else:
            i += 1
    return vals


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)

    dirs = collect_flag_values("--dir")
    orig_dirs = collect_flag_values("--original-dir")
    html_files = collect_flag_values("--html")
    docx_files = collect_flag_values("--docx")
    pdf_files = collect_flag_values("--pdf")
    source_texts = collect_flag_values("--source-text")
    no_counterpart = {os.path.basename(x).lower() for x in collect_flag_values("--no-counterpart")}
    no_figs_in_pkg = "--no-figures-in-package" in sys.argv
    skip_ws_scan = "--skip-workspace-scan" in sys.argv

    # Always infer workspace directory `d` even when `--dir` is omitted (e.g. when `--html` / `--docx` are passed)
    # so missing counterpart deliverables (e.g. Rebuttal_Letter_AUDITED.docx or Figure_EV3_AUDITED.pdf) are still caught!
    if not dirs and not skip_ws_scan:
        ref_file = (html_files or docx_files or pdf_files or [None])[0]
        inferred_d = os.path.dirname(os.path.abspath(ref_file)) if ref_file else os.path.abspath(".")
        if os.path.isdir(inferred_d):
            dirs = [inferred_d]

    missing_counterparts = []
    provenance_warnings = []

    if dirs:
        d = dirs[0]
        all_dir_files = sorted(os.listdir(d))
        if not html_files:
            html_cands = [
                os.path.join(d, fn)
                for fn in all_dir_files
                if fn.lower().endswith(".html") and (
                    fn.lower().startswith("manuscript_audit_report")
                    or "audit" in fn.lower()
                    or "report" in fn.lower()
                )
            ]
            if html_cands:
                # If multiple versioned reports exist, pick the most recently modified one
                html_cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                html_files.append(html_cands[0])
            else:
                html_files.append(os.path.join(d, "MANUSCRIPT_AUDIT_REPORT.html"))
        # Auto-discover scratch_audit/*_indexed.txt if --source-text was not passed
        scratch_dir = os.path.join(d, "scratch_audit")
        if not source_texts and os.path.isdir(scratch_dir):
            for sfn in sorted(os.listdir(scratch_dir)):
                if sfn.endswith("_indexed.txt") or sfn in ("indexed.txt", "manuscript.txt", "rebuttal.txt"):
                    source_texts.append(os.path.join(scratch_dir, sfn))
        # Verify provenance headers on source_texts against workspace files
        for st_path in source_texts:
            if not os.path.isfile(st_path):
                continue
            first_ln = open(st_path, encoding="utf-8", errors="replace").readline().strip()
            m_prov = re.match(r"^#\s*MANUSCRIPT-AUDIT EXTRACT\s*\|\s*file=([^|]+?)\s*\|\s*sha256=([0-9a-fA-F]+)", first_ln)
            if m_prov:
                src_name, expected_sha = m_prov.group(1).strip(), m_prov.group(2).strip().lower()
                cand_src = os.path.join(d, src_name)
                if os.path.isfile(cand_src):
                    actual_sha = hashlib.sha256(open(cand_src, "rb").read()).hexdigest()[:len(expected_sha)].lower()
                    if actual_sha != expected_sha:
                        provenance_warnings.append(
                            f"PROVENANCE MISMATCH: {st_path} was extracted from {src_name} (sha256={expected_sha}), "
                            f"but {cand_src} has sha256={actual_sha}!"
                        )

        explicit_docx = bool(docx_files)
        explicit_pdf = bool(pdf_files)
        audited_docx_in_dir = [
            os.path.join(d, fn) for fn in all_dir_files
            if os.path.isfile(os.path.join(d, fn)) and fn.lower().endswith(".docx") and "_audited" in fn.lower()
        ]
        audited_pdf_in_dir = [
            os.path.join(d, fn) for fn in all_dir_files
            if os.path.isfile(os.path.join(d, fn)) and fn.lower().endswith(".pdf") and "_audited" in fn.lower()
        ]

        # Determine source directory for counterpart parity checking (--original-dir, or d if d has source files, or ../Original)
        src_check_dir = orig_dirs[0] if orig_dirs else d
        has_un_audited_in_d = any(
            fn.lower().endswith((".docx", ".pdf")) and "_audited" not in fn.lower() and not fn.startswith("~$")
            for fn in all_dir_files
        )
        if not orig_dirs and not has_un_audited_in_d:
            sib_orig = os.path.join(os.path.dirname(os.path.abspath(d)), "Original")
            if os.path.isdir(sib_orig):
                src_check_dir = sib_orig

        for fn in all_dir_files:
            full = os.path.join(d, fn)
            if not os.path.isfile(full):
                continue
            low = fn.lower()
            if low.endswith(".docx") and "_audited" in low:
                if not explicit_docx and full not in docx_files:
                    docx_files.append(full)
            elif low.endswith(".pdf") and "_audited" in low:
                if not explicit_pdf and full not in pdf_files:
                    pdf_files.append(full)

        def _norm_stem(s):
            return re.sub(r"[\s_-]+", "_", s.lower()).strip("_")

        if os.path.isdir(src_check_dir):
            for fn in sorted(os.listdir(src_check_dir)):
                full = os.path.join(src_check_dir, fn)
                if not os.path.isfile(full) or fn.startswith("~$"):
                    continue
                low = fn.lower()
                if "_audited" in low or low in no_counterpart:
                    continue
                if low.endswith(".docx"):
                    stem_norm = _norm_stem(os.path.splitext(low)[0])
                    matching_aud = [
                        p for p in audited_docx_in_dir
                        if _norm_stem(os.path.splitext(os.path.basename(p))[0]).startswith(stem_norm + "_audited")
                    ]
                    if not matching_aud:
                        expected_name = os.path.splitext(fn)[0] + "_AUDITED.docx"
                        missing_counterparts.append(("Audited DOCX", fn, expected_name))
                    elif not explicit_docx:
                        for mp in matching_aud:
                            if mp not in docx_files:
                                docx_files.append(mp)
                elif low.endswith(".pdf") and not no_figs_in_pkg:
                    # Skip non-figure checklist/summary PDFs unless they were explicitly annotated
                    if any(k in low for k in ("reporting_summary", "checklist", "wrapped_figures", "guideline", "certificate")):
                        continue
                    stem_norm = _norm_stem(os.path.splitext(low)[0])
                    matching_aud_pdf = [
                        p for p in audited_pdf_in_dir
                        if _norm_stem(os.path.splitext(os.path.basename(p))[0]).startswith(stem_norm + "_audited")
                    ]
                    if not matching_aud_pdf:
                        expected_pdf = os.path.splitext(fn)[0] + "_AUDITED.pdf"
                        missing_counterparts.append(("Audited PDF", fn, expected_pdf))
                    elif not explicit_pdf:
                        for mp in matching_aud_pdf:
                            if mp not in pdf_files:
                                pdf_files.append(mp)

        # Cross-check report_spec.json for Rebuttal / Cover findings that lack an *_AUDITED.docx counterpart
        spec_cand = os.path.join(scratch_dir, "report_spec.json")
        if os.path.isfile(spec_cand):
            try:
                import json as _json
                spec_txt = open(spec_cand, encoding="utf-8", errors="replace").read()
                n_reb_q = len(re.findall(r'"quote_para"\s*:\s*"(?:Rebuttal|Reb|Response):', spec_txt, re.I))
                if n_reb_q > 0 and not any(
                    any(k in os.path.basename(p).lower() for k in ("rebuttal", "response", "reviewer", "point"))
                    for p in audited_docx_in_dir
                ):
                    provenance_warnings.append(
                        f"MISSING REBUTTAL DELIVERABLE: report_spec.json contains {n_reb_q} 'Rebuttal:P#' finding(s), "
                        f"but no Rebuttal *_AUDITED.docx exists on disk!"
                    )
            except Exception:
                pass

    rows = []
    all_passed = True

    for h in html_files:
        ok, msg, _ = check_html_deliverable(h, source_texts)
        rows.append(("HTML Report", os.path.basename(h), "PASS" if ok else "FAIL", msg))
        if not ok:
            all_passed = False

    for dx in docx_files:
        ok, msg, _ = check_docx_deliverable(dx)
        rows.append(("Audited DOCX", os.path.basename(dx), "PASS" if ok else "FAIL", msg))
        if not ok:
            all_passed = False

    for pf in pdf_files:
        ok, msg, _ = check_pdf_deliverable(pf)
        rows.append(("Audited PDF", os.path.basename(pf), "PASS" if ok else "FAIL", msg))
        if not ok:
            all_passed = False

    for dtype_c, src_fn, exp_fn in missing_counterparts:
        rows.append(
            (dtype_c, exp_fn, "FAIL", f"MISSING COUNTERPART for '{src_fn}' (pass --no-counterpart {src_fn} if intentionally unedited)")
        )
        all_passed = False

    for pwarn in provenance_warnings:
        rows.append(("Provenance", "scratch_audit", "FAIL", pwarn))
        all_passed = False

    if not rows:
        sys.exit("ERROR: No deliverables specified or discovered. Pass `--dir .` or `--html / --docx / --pdf`.")

    print("=== FINAL DELIVERABLES ON-DISK MANIFEST ===")
    print(f"{'Type':<14} | {'File':<36} | {'Status':<6} | Details")
    print("-" * 105)
    for dtype, fname, status, detail in rows:
        print(f"{dtype:<14} | {fname:<36} | {status:<6} | {detail}")
    print("-" * 105)

    if all_passed:
        print(f"OVERALL STATUS: PASS — All {len(rows)} deliverable(s) verified on disk.")
        sys.exit(0)
    else:
        n_fail = sum(1 for r in rows if r[2] == "FAIL")
        print(f"OVERALL STATUS: FAIL — {n_fail} of {len(rows)} deliverable(s) failed on-disk verification!")
        sys.exit(1)


if __name__ == "__main__":
    main()
