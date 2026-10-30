#!/usr/bin/env python3
"""Preflight validator for DOCX edit specs, PDF annotation specs, and HTML report specs.

Runs in <1 second BEFORE generating deliverables to catch:
  1. DOCX `para` + `target` misses (using `annotate_docx.py`'s exact `<w:sdt>`-aware matcher),
     automatically reporting the true `[P#]` if a target is in a neighboring paragraph
     (and updating `EDITS.json` in place when `--autofix-para` is passed).
  2. PDF annotation schema or page-bound errors (`ANNS.json` vs `INPUT.pdf`).
  3. HTML `report_spec.json` errors: unresolved `quote_para` IDs, non-verbatim `verbatim_quote`
     strings (preventing mid-audit note drift), and missing `images[].path` crop files.

Zero external dependencies (uses Python 3 standard library only).

Usage:
  preflight_edits.py \\
    [--docx Text.docx:scratch_audit/text_edits.json] \\
    [--pdf Main-figure.pdf:scratch_audit/main_fig_anns.json] \\
    [--report scratch_audit/report_spec.json] \\
    [--manuscript-txt scratch_audit/manuscript_indexed.txt] \\
    [--rebuttal-txt scratch_audit/rebuttal_indexed.txt] \\
    [--autofix-para]
"""
import sys
import os
import re
import json
import zipfile
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from annotate_docx import (
    wtag,
    iter_body_blocks,
    get_para_full_text,
    get_para_subline_ranges,
    _find_target_span,
    validate_and_normalize_edits,
    _best_fuzzy_hint,
    norm_typo,
)
from annotate_pdf import (
    parse_pdf_structure,
    validate_and_normalize_pdf_anns,
)
from build_html_report import (
    load_indexed_paragraphs,
    normalize_report_spec,
)


def collect_flag_values(flag):
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


def preflight_docx_pair(docx_path, edits_json_path, autofix=False):
    print(f"=== PREFLIGHT DOCX EDITS: {os.path.basename(edits_json_path)} -> {os.path.basename(docx_path)} ===")
    if not os.path.isfile(docx_path):
        print(f"  [FAIL] DOCX file not found: {docx_path}")
        return False
    if not os.path.isfile(edits_json_path):
        print(f"  [FAIL] Edits JSON file not found: {edits_json_path}")
        return False

    raw_edits = json.load(open(edits_json_path, encoding="utf-8"))
    edits = validate_and_normalize_edits(raw_edits, edits_json_path)

    with zipfile.ZipFile(docx_path, "r") as zin:
        doc_root = ET.fromstring(zin.read("word/document.xml"))
    body = doc_root.find(wtag("body"))

    top_paras = {}
    tbl_paras = {}
    all_paras = []
    p_idx = 0
    t_idx = 0
    for child in iter_body_blocks(body):
        if child.tag == wtag("p"):
            p_idx += 1
            top_paras[p_idx] = child
            all_paras.append((f"[P{p_idx}]", p_idx, child))
        elif child.tag == wtag("tbl"):
            t_idx += 1
            tbl_paras[t_idx] = []
            for p_cell in child.findall(f".//{wtag('p')}"):
                tbl_paras[t_idx].append(p_cell)
                all_paras.append((f"[TABLE {t_idx}]", None, p_cell))

    all_paras_2tuple = [(lbl, el) for lbl, _, el in all_paras]

    exact_hits = 0
    relocated_hits = 0
    misses = 0
    n_track_changes = 0
    n_comment_only = 0
    modified_json = False

    for idx, edit in enumerate(edits, 1):
        target = edit["target"]
        repl = edit.get("replacement")
        if repl is not None and norm_typo(repl) != norm_typo(target):
            n_track_changes += 1
        else:
            n_comment_only += 1
        target_p = edit.get("para")
        target_sub = edit.get("para_sub")
        target_tbl = edit.get("para_tbl")

        if target_p is not None and target_p in top_paras:
            p_el = top_paras[target_p]
            p_txt = get_para_full_text(p_el)
            if target_sub is not None and target_sub >= 1:
                sub_ranges = get_para_subline_ranges(p_el)
                if target_sub <= len(sub_ranges):
                    s_lo, s_hi = sub_ranges[target_sub - 1]
                    if _find_target_span(p_txt[s_lo:s_hi], target) is not None:
                        exact_hits += 1
                        continue
            if _find_target_span(p_txt, target) is not None:
                exact_hits += 1
                continue

        if target_tbl is not None and target_tbl in tbl_paras:
            if any(_find_target_span(get_para_full_text(c), target) is not None for c in tbl_paras[target_tbl]):
                exact_hits += 1
                continue

        # Search across all paragraphs in document
        found_locs = []
        for lbl, pid, p_el in all_paras:
            p_txt = get_para_full_text(p_el)
            if _find_target_span(p_txt, target) is not None:
                found_locs.append((lbl, pid))

        if found_locs:
            first_lbl, first_pid = found_locs[0]
            all_lbls = ", ".join(l for l, _ in found_locs[:5])
            if target_p is None and first_lbl.startswith("[TABLE"):
                exact_hits += 1
                print(f"  [TABLE HIT] Item #{idx}: matched in {first_lbl}: '{target[:65]}'")
            else:
                relocated_hits += 1
                print(f"  [RELOCATED] Item #{idx}: spec had para={target_p}, but target matched in {all_lbls}: '{target[:65]}'")
                if autofix and first_pid is not None and idx - 1 < len(raw_edits):
                    raw_edits[idx - 1]["para"] = first_pid
                    modified_json = True
        else:
            misses += 1
            hint = _best_fuzzy_hint(target, target_p, top_paras, all_paras_2tuple)
            print(f"  [MISS]      Item #{idx} (para={target_p}): '{target[:75]}'")
            print(f"              -> Hint: {hint}")

    if modified_json:
        with open(edits_json_path, "w", encoding="utf-8") as f:
            json.dump(raw_edits, f, indent=2, ensure_ascii=False)
        print(f"  [AUTOFIX] Updated relocated 'para' indices in {edits_json_path}")

    print(
        f"  Summary: {exact_hits} exact [P#]/[TABLE] match(es), {relocated_hits} relocated match(es) "
        f"(still applied by annotate_docx.py), {misses} miss(es) out of {len(edits)} item(s) "
        f"({n_track_changes} Track Change replacement(s), {n_comment_only} comment-only item(s))."
    )
    if len(edits) > 0 and n_track_changes == 0:
        print(
            "  [WARN] 0 items have a 'replacement' value != 'target' (all items are comment-only). "
            "If you intended Track Changes (<w:del>/<w:ins>), provide 'replacement' on text-fix items."
        )
    return misses == 0


def preflight_pdf_pair(pdf_path, anns_json_path):
    print(f"\n=== PREFLIGHT PDF ANNOTATIONS: {os.path.basename(anns_json_path)} -> {os.path.basename(pdf_path)} ===")
    if not os.path.isfile(pdf_path):
        print(f"  [FAIL] PDF file not found: {pdf_path}")
        return False
    if not os.path.isfile(anns_json_path):
        print(f"  [FAIL] Annotations JSON file not found: {anns_json_path}")
        return False

    info = parse_pdf_structure(open(pdf_path, "rb").read())
    raw_anns = json.load(open(anns_json_path, encoding="utf-8"))
    norm_anns = validate_and_normalize_pdf_anns(
        raw_anns, len(info["page_obj_nums"]), info["page_mediaboxes"], "px", 200.0, anns_json_path
    )
    n_boxed = sum(1 for a in norm_anns if a.get("box") is not None)
    n_page = sum(1 for a in norm_anns if a.get("box") is None)
    print(f"  Summary: PASS — {len(norm_anns)} valid annotation(s) ({n_boxed} box-anchored, {n_page} page-level) across {len(info['page_obj_nums'])} PDF page(s).")
    if len(norm_anns) > 0 and n_boxed == 0:
        print(
            "  [WARN] All PDF annotations are page-level sticky notes (0 bounding boxes). "
            "To draw colored highlight rectangles around figure panels, provide \"box\": [x0, y0, x1, y1]."
        )
    return True


def preflight_report_spec(spec_path, ms_txt_path, reb_txt_path, extra_txt_paths=None):
    print(f"\n=== PREFLIGHT HTML REPORT SPEC: {os.path.basename(spec_path)} ===")
    if not os.path.isfile(spec_path):
        print(f"  [FAIL] Report spec file not found: {spec_path}")
        return False

    if not reb_txt_path:
        for cand in ("scratch_audit/rebuttal_indexed.txt", "rebuttal_indexed.txt"):
            if os.path.isfile(cand):
                reb_txt_path = cand
                break

    ms_paras = load_indexed_paragraphs(ms_txt_path)
    reb_paras = load_indexed_paragraphs(reb_txt_path)
    extra_texts = []
    for ep in (extra_txt_paths or []):
        if os.path.isfile(ep):
            ep_paras = load_indexed_paragraphs(ep)
            if ep_paras:
                extra_texts.append(" ".join(ep_paras.values()))
            else:
                extra_texts.append(open(ep, encoding="utf-8", errors="replace").read())
    ms_full_norm = norm_typo(" ".join(ms_paras.values())).lower() if ms_paras else ""
    reb_full_norm = norm_typo(" ".join(reb_paras.values())).lower() if reb_paras else ""
    extra_full_norm = norm_typo(" ".join(extra_texts)).lower() if extra_texts else ""

    raw_spec = json.load(open(spec_path, encoding="utf-8"))
    spec = normalize_report_spec(raw_spec)

    errors = 0
    skipped = 0
    total_f = 0
    for sec in spec.get("sections", []):
        sec_title_low = (sec.get("title") or "").lower()
        for f in sec.get("findings", []):
            total_f += 1
            fid = f.get("id", f"#{total_f}")
            qpara = f.get("quote_para", "")
            vquote = f.get("verbatim_quote", "").strip()
            loc_low = (f.get("location") or "").lower()
            vq_low = vquote.lower()
            is_reb_ref = (
                qpara.lower().startswith("rebuttal:")
                or any(k in loc_low or k in sec_title_low for k in ("rebuttal", "reviewer", "response to"))
            )
            is_external_non_ms = (
                qpara.lower().startswith("cover:")
                or any(
                    k in loc_low or k in sec_title_low or k in vq_low
                    for k in (
                        "cover letter", ".xlsx", "excel", "checklist", "source_data", "source data",
                        "table ev", "supplementary table", "movie", "video", ".pdf", "panel ",
                        "axis", "label", "scale bar", "legend file",
                    )
                )
            )

            if qpara:
                if qpara.lower().startswith("rebuttal:"):
                    pid = qpara.split(":", 1)[1].strip()
                    if not reb_paras:
                        skipped += 1
                        print(f"  [SKIP] Finding {fid}: quote_para '{qpara}' skipped (--rebuttal-txt not provided)")
                    elif pid not in reb_paras:
                        errors += 1
                        print(f"  [FAIL] Finding {fid}: quote_para '{qpara}' not found in {reb_txt_path}")
                else:
                    if ms_paras and qpara not in ms_paras:
                        errors += 1
                        print(f"  [FAIL] Finding {fid}: quote_para '{qpara}' not found in {ms_txt_path}")

            if vquote and (ms_full_norm or reb_full_norm or extra_full_norm):
                vq_clean = re.sub(r"\s+", " ", norm_typo(vquote)).strip().lower()
                ms_space = re.sub(r"\s+", " ", ms_full_norm)
                reb_space = re.sub(r"\s+", " ", reb_full_norm)
                ext_space = re.sub(r"\s+", " ", extra_full_norm)
                if vq_clean not in ms_space and vq_clean not in reb_space and vq_clean not in ext_space:
                    if is_external_non_ms or (is_reb_ref and not reb_paras):
                        skipped += 1
                        print(f"  [SKIP] Finding {fid}: external/figure/table quote skipped: \"{vquote[:65]}...\"")
                    else:
                        errors += 1
                        print(
                            f"  [FAIL] Finding {fid}: verbatim_quote does not match ground-truth text "
                            f"(use 'quote_para' instead of manual quotes to prevent note drift!): \"{vquote[:80]}...\""
                        )

            for img in f.get("images", []):
                ipath = img.get("path", "")
                if not ipath or not os.path.isfile(ipath):
                    errors += 1
                    print(f"  [FAIL] Finding {fid}: image file not found on disk: '{ipath}'")

    if errors == 0:
        skip_note = f" ({skipped} external/rebuttal quote(s) skipped)" if skipped else ""
        print(f"  Summary: PASS — {total_f} finding(s), all quote_para/verbatim_quote/image references verified{skip_note}.")
        return True
    print(f"  Summary: FAIL — {errors} error(s) in {spec_path}.")
    return False


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)

    docx_pairs = collect_flag_values("--docx")
    pdf_pairs = collect_flag_values("--pdf")
    reports = collect_flag_values("--report")
    ms_txts = collect_flag_values("--manuscript-txt")
    reb_txts = collect_flag_values("--rebuttal-txt")
    extra_txts = collect_flag_values("--extra-txt")
    autofix = "--autofix-para" in sys.argv

    ms_txt = ms_txts[0] if ms_txts else None
    reb_txt = reb_txts[0] if reb_txts else None

    all_ok = True
    for pair in docx_pairs:
        if ":" not in pair:
            sys.exit(f"ERROR: --docx argument must be INPUT.docx:EDITS.json, got '{pair}'")
        d_path, j_path = pair.split(":", 1)
        if not preflight_docx_pair(d_path, j_path, autofix=autofix):
            all_ok = False

    for pair in pdf_pairs:
        if ":" not in pair:
            sys.exit(f"ERROR: --pdf argument must be INPUT.pdf:ANNS.json, got '{pair}'")
        p_path, j_path = pair.split(":", 1)
        if not preflight_pdf_pair(p_path, j_path):
            all_ok = False

    for r_path in reports:
        if not preflight_report_spec(r_path, ms_txt, reb_txt, extra_txts):
            all_ok = False

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
