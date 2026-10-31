#!/usr/bin/env python3
"""
run_phase1.py — Parallel Phase 1 Pre-Audit Orchestrator for Manuscript Audit
=============================================================================
Runs the entire deterministic Phase 1 triage pipeline in a single command
(cutting 15–20 sequential tool turns down to 1 turn and ~3–8 seconds wall-clock):

  1. Discovers all `.docx`, `.pdf`, `.xlsx`, standalone figure images (`.png`/`.jpg`/`.tif`),
     and supplementary video files / `.zip` movie archives in the target directory.
  2. Extracts indexed text (`manuscript_indexed.txt`, `rebuttal_indexed.txt`,
     `cover_indexed.txt`, `supp_*.txt`), formatting audits (`--audit-format`),
     and embedded Mendeley/Zotero CSL-JSON (`--extract-csl`) via `docx2txt.py`.
  3. Runs all deterministic audit scripts in parallel via `ThreadPoolExecutor`:
       - `ref_audit.py`
       - `deep_ref_audit.py --auto`
       - `callout_audit.py`
       - `consistency_audit.py`
       - `xlsx_audit.py --compare-text` (including XFA PDF Reporting Summaries)
       - `render_figs.py page-text --compare-text` (across all figure PDFs)
       - `render_figs.py video --compare-text` (if videos or movie `.zip` files exist)
  4. Optionally wraps standalone figure images (`wrap-images`) if no figure PDF exists,
     and renders 200 DPI `--grid` PNGs (`--render-grids`) into `scratch_audit/figs/`.
  5. Writes individual logs to `scratch_audit/*.log` AND a consolidated
     `scratch_audit/phase1_triage_summary.txt`, printing all high-priority `!!`
     and `[FAIL]` alerts directly to stdout.

Usage:
  python3 run_phase1.py <input_dir> [--out-dir scratch_audit] [--dpi 200] [--render-grids] [--offline]
"""
import os
import re
import sys
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def _natural_sort_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _classify_docx_files(docx_paths):
    """Classify .docx files in input_dir into (main_ms, rebuttal, cover, supp_list)."""
    main_ms = None
    rebuttal = None
    cover = None
    supp_list = []

    scored_main = []
    for p in docx_paths:
        bn = os.path.basename(p).lower()
        if "_audited" in bn or bn.startswith("~$"):
            continue
        if any(k in bn for k in ("rebuttal", "response", "reviewer", "point-by-point", "letter_to_rev")):
            if rebuttal is None:
                rebuttal = p
            else:
                supp_list.append(p)
        elif "cover" in bn and "letter" in bn:
            if cover is None:
                cover = p
            else:
                supp_list.append(p)
        elif any(k in bn for k in ("reagent", "table", "supp", "movie", "video", "legend", "si_", "appendix")):
            supp_list.append(p)
        else:
            scored_main.append((os.path.getsize(p), p))

    if scored_main:
        scored_main.sort(key=lambda x: -x[0])
        main_ms = scored_main[0][1]
        for _, extra_p in scored_main[1:]:
            supp_list.append(extra_p)
    elif supp_list:
        supp_list.sort(key=lambda p: -os.path.getsize(p))
        main_ms = supp_list.pop(0)

    return main_ms, rebuttal, cover, supp_list


def _run_cmd(label, cmd_list, log_path=None):
    t0 = time.time()
    p = subprocess.run(cmd_list, capture_output=True, text=True, errors="replace")
    out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    dt = time.time() - t0
    if log_path:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(out)
    return label, p.returncode, out, dt


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)

    inp_dir = sys.argv[1]
    if not os.path.isdir(inp_dir):
        sys.exit(f"ERROR: Input directory not found: {inp_dir}")

    out_dir = opt("--out-dir", "scratch_audit")
    dpi = int(opt("--dpi", "200"))
    render_grids = "--render-grids" in sys.argv
    offline = "--offline" in sys.argv
    os.makedirs(out_dir, exist_ok=True)

    all_files = [
        os.path.join(inp_dir, fn)
        for fn in sorted(os.listdir(inp_dir), key=_natural_sort_key)
        if os.path.isfile(os.path.join(inp_dir, fn)) and not fn.startswith("~$") and "_audited" not in fn.lower()
    ]

    docx_files = [p for p in all_files if p.lower().endswith(".docx")]
    pdf_files = [p for p in all_files if p.lower().endswith(".pdf")]
    xlsx_files = [p for p in all_files if p.lower().endswith(".xlsx")]
    img_files = [p for p in all_files if os.path.splitext(p)[1].lower() in (".png", ".jpg", ".jpeg", ".tif", ".tiff")]
    vid_or_zip = [
        p for p in all_files
        if os.path.splitext(p)[1].lower() in (".mp4", ".mov", ".avi", ".mkv", ".webm")
        or (p.lower().endswith(".zip") and any(k in os.path.basename(p).lower() for k in ("movie", "video", "supp", "ev", "sv")))
    ]

    main_docx, reb_docx, cov_docx, supp_docxs = _classify_docx_files(docx_files)
    if not main_docx:
        sys.exit(f"ERROR: No manuscript .docx file found in {inp_dir}")

    print(f"=== PHASE 1 PRE-AUDIT ORCHESTRATOR: {inp_dir} -> {out_dir} ===")
    print(f"  Main DOCX       : {os.path.basename(main_docx)}")
    if reb_docx:
        print(f"  Rebuttal DOCX   : {os.path.basename(reb_docx)}")
    if cov_docx:
        print(f"  Cover DOCX      : {os.path.basename(cov_docx)}")
    if supp_docxs:
        print(f"  Companion DOCXs : {', '.join(os.path.basename(p) for p in supp_docxs)}")
    print(f"  PDFs ({len(pdf_files)})       : {', '.join(os.path.basename(p) for p in pdf_files) or 'none'}")
    print(f"  Excel (.xlsx)   : {len(xlsx_files)} file(s)")
    print(f"  Images / Videos : {len(img_files)} image(s), {len(vid_or_zip)} video/zip file(s)")

    # Step 1: Extract indexed texts, embedded media, and CSL-JSON
    ms_txt = os.path.join(out_dir, "manuscript_indexed.txt")
    csl_json = os.path.join(out_dir, "extracted_refs.csl.json")
    ms_media_dir = os.path.join(out_dir, "ms_media")
    docx2txt_py = os.path.join(SCRIPT_DIR, "docx2txt.py")

    p_ms = subprocess.run(
        [sys.executable, docx2txt_py, main_docx, "--indexed", "--extract-csl", csl_json, "--media", "--outdir", ms_media_dir],
        capture_output=True,
        text=True,
        errors="replace",
    )
    with open(ms_txt, "w", encoding="utf-8") as f:
        f.write(p_ms.stdout)

    reb_txt = None
    if reb_docx:
        reb_txt = os.path.join(out_dir, "rebuttal_indexed.txt")
        reb_media_dir = os.path.join(out_dir, "rebuttal_media")
        p_reb = subprocess.run(
            [sys.executable, docx2txt_py, reb_docx, "--indexed", "--media", "--outdir", reb_media_dir],
            capture_output=True,
            text=True,
            errors="replace",
        )
        with open(reb_txt, "w", encoding="utf-8") as f:
            f.write(p_reb.stdout)

    cov_txt = None
    if cov_docx:
        cov_txt = os.path.join(out_dir, "cover_indexed.txt")
        p_cov = subprocess.run([sys.executable, docx2txt_py, cov_docx, "--indexed"], capture_output=True, text=True, errors="replace")
        with open(cov_txt, "w", encoding="utf-8") as f:
            f.write(p_cov.stdout)

    supp_txts = []
    supp_csls = {}
    for idx_s, s_docx in enumerate(supp_docxs, 1):
        stem_s = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(s_docx))[0])
        st_path = os.path.join(out_dir, f"supp_{idx_s}_{stem_s}_indexed.txt")
        scsl_path = os.path.join(out_dir, f"supp_{idx_s}_{stem_s}_refs.csl.json")
        p_s = subprocess.run(
            [sys.executable, docx2txt_py, s_docx, "--indexed", "--extract-csl", scsl_path],
            capture_output=True,
            text=True,
            errors="replace",
        )
        with open(st_path, "w", encoding="utf-8") as f:
            f.write(p_s.stdout)
        supp_txts.append(st_path)
        if os.path.isfile(scsl_path):
            supp_csls[st_path] = scsl_path

    # Also extract indexed text from any PDF-formatted Supplementary Information (e.g., Supplementary_Information.pdf)
    # when it contains prose, supplementary legends, tables, or supplementary references
    render_figs_py = os.path.join(SCRIPT_DIR, "render_figs.py")
    supp_docx_stems = {re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(p))[0].lower()) for p in supp_docxs}
    for idx_sp, sp_pdf in enumerate(pdf_files, 1):
        bn_low = os.path.basename(sp_pdf).lower()
        if any(k in bn_low for k in ("reporting-summary", "reporting_summary", "checklist")):
            continue
        if any(k in bn_low for k in ("supp", "si_", "si.", "extended", "appendix")):
            pdf_stem_clean = re.sub(r"[^a-z0-9]", "", os.path.splitext(bn_low)[0])
            if pdf_stem_clean in supp_docx_stems:
                continue
            stem_sp = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(sp_pdf))[0])
            sp_txt_path = os.path.join(out_dir, f"supp_pdf_{idx_sp}_{stem_sp}_indexed.txt")
            p_sp = subprocess.run(
                [sys.executable, render_figs_py, "pdf-to-indexed-txt", sp_pdf, "--out", sp_txt_path],
                capture_output=True,
                text=True,
                errors="replace",
            )
            if os.path.isfile(sp_txt_path):
                sp_content = open(sp_txt_path, encoding="utf-8", errors="replace").read()
                if len(sp_content.split()) >= 80 and re.search(
                    r"\b(?:Supplementary\s+(?:Figure|Fig\.?|Table|Methods?|Note|Text|References?)|Figure\s+S\d+|Table\s+S\d+)\b",
                    sp_content,
                    re.I,
                ):
                    supp_txts.append(sp_txt_path)
                else:
                    try:
                        os.remove(sp_txt_path)
                    except OSError:
                        pass

    # Step 2: Run docx_formatting first (<0.2s) so consistency_audit can consume its line-drift report
    fmt_log = os.path.join(out_dir, "01_docx_formatting.log")
    _, fmt_rc, fmt_out, fmt_dt = _run_cmd(
        "docx_formatting",
        [sys.executable, docx2txt_py, main_docx, "--audit-format"],
        fmt_log,
    )

    # Launch parallel Phase 1 audit tasks
    tasks = []
    tasks.append((
        "ref_audit",
        [sys.executable, os.path.join(SCRIPT_DIR, "ref_audit.py"), ms_txt, "--docx", main_docx],
        os.path.join(out_dir, "02_ref_audit.log"),
    ))
    deep_ref_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "deep_ref_audit.py"), ms_txt, "--auto", "--docx", main_docx]
    if os.path.isfile(csl_json):
        deep_ref_cmd.extend(["--csl", csl_json])
    if offline:
        deep_ref_cmd.append("--offline")
    tasks.append((
        "deep_ref_audit",
        deep_ref_cmd,
        os.path.join(out_dir, "03_deep_ref_audit.log"),
    ))

    # Also run ref_audit and deep_ref_audit on any companion .docx that has its own References section
    for idx_s, st_path in enumerate(supp_txts, 1):
        st_content = open(st_path, encoding="utf-8", errors="replace").read()
        if re.search(r"^\s*\[P\d+\]\s*(?:\*\*)?(?:Supplementary\s+|Supplemental\s+)?References\b", st_content, re.I | re.M):
            tasks.append((
                f"supp_{idx_s}_ref_audit",
                [sys.executable, os.path.join(SCRIPT_DIR, "ref_audit.py"), st_path],
                os.path.join(out_dir, f"02b_supp_{idx_s}_ref_audit.log"),
            ))
            s_deep_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "deep_ref_audit.py"), st_path, "--auto"]
            if st_path in supp_csls:
                s_deep_cmd.extend(["--csl", supp_csls[st_path]])
            if offline:
                s_deep_cmd.append("--offline")
            tasks.append((
                f"supp_{idx_s}_deep_ref_audit",
                s_deep_cmd,
                os.path.join(out_dir, f"03b_supp_{idx_s}_deep_ref_audit.log"),
            ))

    callout_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "callout_audit.py"), ms_txt] + supp_txts
    callout_cmd.extend(["--supp-files", inp_dir])
    tasks.append((
        "callout_audit",
        callout_cmd,
        os.path.join(out_dir, "04_callout_audit.log"),
    ))

    cons_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "consistency_audit.py"), ms_txt] + supp_txts
    if reb_txt:
        cons_cmd.extend(["--rebuttal", reb_txt])
    if cov_txt:
        cons_cmd.extend(["--cover-letter", cov_txt])
    if os.path.isfile(fmt_log):
        cons_cmd.extend(["--format-audit", fmt_log])
    tasks.append((
        "consistency_audit",
        cons_cmd,
        os.path.join(out_dir, "05_consistency_audit.log"),
    ))

    tsv_out_dir = os.path.join(out_dir, "xlsx_tsv")
    tasks.append((
        "xlsx_and_xfa_audit",
        [sys.executable, os.path.join(SCRIPT_DIR, "xlsx_audit.py"), inp_dir, "--manuscript-txt", ms_txt, "--out-tsv", tsv_out_dir],
        os.path.join(out_dir, "06_xlsx_and_xfa_audit.log"),
    ))

    render_figs_py = os.path.join(SCRIPT_DIR, "render_figs.py")
    fig_pdfs = [
        p for p in pdf_files
        if "reporting-summary" not in os.path.basename(p).lower() and "checklist" not in os.path.basename(p).lower()
    ]
    if not fig_pdfs:
        if img_files:
            wrapped_pdf = os.path.join(out_dir, "Wrapped_Figures.pdf")
            subprocess.run([sys.executable, render_figs_py, "wrap-images"] + img_files + [wrapped_pdf, "--dpi", str(dpi)], capture_output=True)
            if os.path.isfile(wrapped_pdf):
                fig_pdfs.append(wrapped_pdf)
        elif os.path.isdir(ms_media_dir):
            emb_imgs = [
                os.path.join(ms_media_dir, fn)
                for fn in sorted(os.listdir(ms_media_dir), key=_natural_sort_key)
                if os.path.splitext(fn)[1].lower() in (".png", ".jpg", ".jpeg", ".tif", ".tiff")
            ]
            if emb_imgs:
                wrapped_pdf = os.path.join(out_dir, "Wrapped_Figures.pdf")
                subprocess.run([sys.executable, render_figs_py, "wrap-images", ms_media_dir, wrapped_pdf, "--dpi", str(dpi)], capture_output=True)
                if os.path.isfile(wrapped_pdf):
                    fig_pdfs.append(wrapped_pdf)

    for idx_fp, fp in enumerate(fig_pdfs, 1):
        pt_cmd = [sys.executable, render_figs_py, "page-text", fp, "--compare-text", ms_txt]
        for st in supp_txts:
            pt_cmd.extend(["--extra-txt", st])
        tasks.append((
            f"pdf_page_text_{idx_fp}_{os.path.basename(fp)}",
            pt_cmd,
            os.path.join(out_dir, f"07_pdf_page_text_{idx_fp}.log"),
        ))

    if vid_or_zip:
        tasks.append((
            "video_audit",
            [sys.executable, render_figs_py, "video", inp_dir, os.path.join(out_dir, "videos"), "--manuscript-txt", ms_txt],
            os.path.join(out_dir, "08_video_audit.log"),
        ))

    if render_grids and fig_pdfs:
        for idx_fp, fp in enumerate(fig_pdfs, 1):
            stem_fp = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(fp))[0])
            tasks.append((
                f"render_grids_{stem_fp}",
                [sys.executable, render_figs_py, "render", fp, os.path.join(out_dir, "figs", stem_fp), "--dpi", str(dpi), "--grid"],
                os.path.join(out_dir, f"09_render_grids_{idx_fp}.log"),
            ))

    results = {}
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
        futs = {pool.submit(_run_cmd, lbl, cmd, lp): lbl for lbl, cmd, lp in tasks}
        for fut in as_completed(futs):
            lbl, rc, out, dt = fut.result()
            results[lbl] = (rc, out, dt)

    total_dt = time.time() - t_start
    summary_path = os.path.join(out_dir, "phase1_triage_summary.txt")
    high_priority_lines = []

    # Check Case 3D: manuscript cites Figure 1..N or defines Figure legends, but 0 figure PDFs or images exist
    ms_text_content = open(ms_txt, encoding="utf-8", errors="replace").read() if os.path.isfile(ms_txt) else ""
    if not fig_pdfs and re.search(r"\b(?:Figure|Fig\.)\s*1\b", ms_text_content, re.I):
        pkg_msg = (
            "!! [PACKAGE COMPLETENESS GAP — CASE 3D: NO FIGURE ARTWORK FILES FOUND] "
            "The manuscript text cites/defines Figures, but 0 figure PDFs, 0 standalone image files, "
            "and 0 embedded DOCX images exist in the submission package! Report this missing figure artwork "
            "as a CRITICAL package-completeness finding and skip visual Step 3/4 rendering."
        )
        high_priority_lines.append(("package_check", pkg_msg))

    seen_flags = set()
    with open(summary_path, "w", encoding="utf-8") as sf:
        sf.write(f"=== CONSOLIDATED PHASE 1 TRIAGE SUMMARY ({inp_dir}, completed in {total_dt:.2f}s) ===\n\n")
        if high_priority_lines:
            for lbl, hln in high_priority_lines:
                sf.write(f"[{lbl}] {hln}\n\n")
        for lbl, _, _ in [("docx_formatting", None, fmt_log)] + tasks:
            rc, out, dt = (fmt_rc, fmt_out, fmt_dt) if lbl == "docx_formatting" else results.get(lbl, (-1, "", 0.0))
            sf.write(f"\n{'=' * 78}\n## TASK: {lbl} (rc={rc}, {dt:.2f}s)\n{'=' * 78}\n")
            sf.write(out.strip() + "\n")
            for ln in out.splitlines():
                s_ln = ln.strip()
                if ("!!" in s_ln or "[FAIL]" in s_ln or "[WARN]" in s_ln or "[CONFLICT]" in s_ln) and s_ln not in seen_flags:
                    seen_flags.add(s_ln)
                    high_priority_lines.append((lbl, s_ln))

    print(f"\n=== PHASE 1 COMPLETED IN {total_dt:.2f}s — {len(high_priority_lines)} HIGH-PRIORITY FLAG(S) ===")
    for lbl, hln in high_priority_lines[:80]:
        print(f"  [{lbl}] {hln}")
    if len(high_priority_lines) > 80:
        print(f"  ... (+{len(high_priority_lines) - 80} more flag(s) in {summary_path})")
    print(f"\nFull consolidated triage log saved to: {summary_path}")


if __name__ == "__main__":
    main()
