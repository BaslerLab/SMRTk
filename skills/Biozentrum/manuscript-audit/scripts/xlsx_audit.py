#!/usr/bin/env python3
"""Pure-Python .xlsx Workbook Reader, XFA Reporting Summary Extractor & Source-Data Statistical Auditor.

Zero external dependencies (uses Python 3 standard library `zipfile`, `zlib` + `xml.etree.ElementTree` only —
works on minimal hosts where `openpyxl` and `pandas` are not installed).

Usage:
  xlsx_audit.py FILE_OR_DIR [FILE2.xlsx ...] [--manuscript-txt indexed.txt] [--dump-rows 8] [--out-tsv DIR]

What it does:
  1. WORKBOOK & SHEET INVENTORY:
     Reads all `.xlsx` files (or scans a directory), resolves `sharedStrings.xml` and worksheet relationships,
     reports sheet dimensions (rows x cols), and previews headers/data rows. Optionally exports `.tsv` files
     when `--out-tsv DIR` is provided.
  2. TEMPLATE PLACEHOLDERS, FORMULA ERRORS & NON-NUMERIC TOKENS:
     Scans every cell for unfilled template strings (`<insert here>`, `TODO`, `TBD`), Excel formula errors
     (`#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, `#NUM!`), and non-numeric tokens embedded inside numeric columns.
  3. SOURCE-DATA STATISTICAL SUMMARY (`n`, `mean`, `SD`, `SEM`, `median`):
     Automatically detects numeric data columns (and labeled numeric rows) across each sheet and computes
     exact `n`, `mean`, sample `SD` (ddof=1), `SEM`, and `median`.
  4. MANUSCRIPT vs. SOURCE-DATA CROSS-CHECK (when `--manuscript-txt` is supplied):
     Extracts `X ± Y` (`mean ± SD/SEM`) claims from the manuscript text, matches them against the `.xlsx`
     series, verifies exact matches, and flags rounding/calculation discrepancies or `n` mismatches.
  5. ADOBE XFA PDF FORM / REPORTING SUMMARY EXTRACTION:
     If an Adobe XFA form PDF (e.g. a journal Reporting Summary whose fields are stored in compressed XML
     streams rather than standard PDF page text) is present, extracts and prints all filled form fields so
     the model can review them against the manuscript.
"""
import sys
import os
import re
import math
import zlib
import zipfile
import statistics
import xml.etree.ElementTree as ET

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

PLACEHOLDER_PAT = re.compile(
    r"(<\s*insert[^>]*>|\[\s*insert[^\]]*\]|\bexample\s+for\s+demonstration\b|"
    r"\benter\s+[a-z0-9_\s]{1,25}\s+here\b|\b(?:TODO|TBD|FIXME)\b)",
    re.I,
)
EXCEL_ERR_PAT = re.compile(r"^(?:#REF!|#DIV/0!|#VALUE!|#NAME\?|#NUM!|#NULL!)$")


def _col_letters_to_idx(col_str):
    """Convert Excel column letters ('A', 'B', ..., 'AA') to 0-based column index."""
    idx = 0
    for ch in col_str:
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def _idx_to_col_letters(idx):
    """Convert 0-based column index to Excel column letters ('A', 'B', ..., 'AA')."""
    out = []
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(ord("A") + rem))
    return "".join(reversed(out))


def read_xlsx_workbook(xlsx_path):
    """Parse an .xlsx file into a list of (sheet_name, grid_rows) using pure Python stdlib."""
    sheets_out = []
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        namelist = set(zf.namelist())
        shared_strings = []
        if "xl/sharedStrings.xml" in namelist:
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in ss_root.findall(f"{{{MAIN_NS}}}si"):
                parts = [t.text or "" for t in si.iter(f"{{{MAIN_NS}}}t")]
                shared_strings.append("".join(parts))

        rid_to_target = {}
        if "xl/_rels/workbook.xml.rels" in namelist:
            rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rels_root:
                rid = rel.attrib.get("Id", "")
                target = rel.attrib.get("Target", "").lstrip("/")
                if target and not target.startswith("xl/"):
                    target = "xl/" + target
                rid_to_target[rid] = target

        sheet_entries = []
        if "xl/workbook.xml" in namelist:
            wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
            for sh in wb_root.iter(f"{{{MAIN_NS}}}sheet"):
                name = sh.attrib.get("name", "Sheet")
                rid = sh.attrib.get(f"{{{REL_NS}}}id", "")
                target = rid_to_target.get(rid, "")
                if target in namelist:
                    sheet_entries.append((name, target))

        if not sheet_entries:
            for nm in sorted(namelist):
                if nm.startswith("xl/worksheets/sheet") and nm.endswith(".xml"):
                    sheet_entries.append((os.path.splitext(os.path.basename(nm))[0], nm))

        for sh_name, sh_target in sheet_entries:
            sh_root = ET.fromstring(zf.read(sh_target))
            row_map = {}
            max_col = -1
            max_row = -1
            for row_el in sh_root.iter(f"{{{MAIN_NS}}}row"):
                r_attr = row_el.attrib.get("r")
                r_idx = (int(r_attr) - 1) if (r_attr and r_attr.isdigit()) else (max_row + 1)
                max_row = max(max_row, r_idx)
                col_cursor = 0
                for c_el in row_el.findall(f"{{{MAIN_NS}}}c"):
                    ref = c_el.attrib.get("r", "")
                    m_col = re.match(r"^([A-Z]+)", ref)
                    c_idx = _col_letters_to_idx(m_col.group(1)) if m_col else col_cursor
                    col_cursor = c_idx + 1
                    max_col = max(max_col, c_idx)

                    c_type = c_el.attrib.get("t", "")
                    v_el = c_el.find(f"{{{MAIN_NS}}}v")
                    is_el = c_el.find(f"{{{MAIN_NS}}}is")
                    val_str = ""
                    if c_type == "s" and v_el is not None and v_el.text is not None:
                        try:
                            s_idx = int(v_el.text.strip())
                            val_str = shared_strings[s_idx] if 0 <= s_idx < len(shared_strings) else v_el.text
                        except ValueError:
                            val_str = v_el.text
                    elif c_type == "inlineStr" and is_el is not None:
                        val_str = "".join(t.text or "" for t in is_el.iter(f"{{{MAIN_NS}}}t"))
                    elif v_el is not None and v_el.text is not None:
                        val_str = v_el.text.strip()
                    if val_str != "":
                        row_map.setdefault(r_idx, {})[c_idx] = val_str.strip()

            grid = []
            if max_row >= 0 and max_col >= 0:
                for r_i in range(max_row + 1):
                    r_dict = row_map.get(r_i, {})
                    row_vals = [r_dict.get(c_i, "") for c_i in range(max_col + 1)]
                    grid.append(row_vals)
            sheets_out.append((sh_name, grid))
    return sheets_out


def _try_float(val):
    """Parse a clean numeric cell string as float, or return None."""
    if not val or not isinstance(val, str):
        return None
    s = val.strip().replace(",", "")
    if not re.match(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$", s):
        return None
    try:
        v = float(s)
        return v if math.isfinite(v) else None
    except ValueError:
        return None


def _is_sequential_index(nums):
    """Return True if nums is just a sequential integer counter 1, 2, 3... or 0, 1, 2..."""
    if len(nums) < 3:
        return False
    if all(abs(x - round(x)) < 1e-9 for x in nums):
        ints = [int(round(x)) for x in nums]
        if ints == list(range(ints[0], ints[0] + len(ints))) and ints[0] in (0, 1):
            return True
    return False


def compute_sheet_series_stats(file_label, sheet_name, grid):
    """Extract numeric series (by column, paired adjacent column difference, and by row) and compute n, mean, SD, SEM, median."""
    series_list = []
    if not grid:
        return series_list
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)

    # 1. Column-wise numeric series
    col_row_vals = {}
    col_labels = {}
    for c_i in range(n_cols):
        col_letter = _idx_to_col_letters(c_i)
        header_parts = []
        nums = []
        r_map = {}
        for r_i in range(n_rows):
            cell = grid[r_i][c_i] if c_i < len(grid[r_i]) else ""
            fv = _try_float(cell)
            if fv is not None:
                nums.append(fv)
                r_map[r_i] = fv
            elif cell and len(nums) == 0 and len(header_parts) < 3:
                header_parts.append(re.sub(r"\s+", " ", cell)[:50])
        if len(nums) >= 2 and not _is_sequential_index(nums):
            label = " / ".join(header_parts) if header_parts else f"Col {col_letter}"
            col_row_vals[c_i] = r_map
            col_labels[c_i] = (col_letter, label)
            mean_v = statistics.mean(nums)
            sd_v = statistics.stdev(nums) if len(nums) >= 2 else 0.0
            sem_v = sd_v / math.sqrt(len(nums))
            med_v = statistics.median(nums)
            series_list.append({
                "file": file_label,
                "sheet": sheet_name,
                "axis": f"Col {col_letter}",
                "label": label,
                "n": len(nums),
                "mean": mean_v,
                "sd": sd_v,
                "sem": sem_v,
                "median": med_v,
                "min": min(nums),
                "max": max(nums),
                "is_paired_diff": False,
            })

    # 1b. Paired differences between adjacent numeric columns of matching rows (e.g. Post - Pre or After - Before)
    sorted_cols = sorted(col_row_vals.keys())
    for idx_c in range(len(sorted_cols) - 1):
        c1, c2 = sorted_cols[idx_c], sorted_cols[idx_c + 1]
        if c2 - c1 > 2:
            continue
        shared_r = sorted(set(col_row_vals[c1].keys()) & set(col_row_vals[c2].keys()))
        if len(shared_r) >= 3:
            diffs_2_1 = [col_row_vals[c2][r] - col_row_vals[c1][r] for r in shared_r]
            if any(abs(d) > 1e-9 for d in diffs_2_1) and len(set(round(d, 6) for d in diffs_2_1)) >= 2:
                let1, lbl1 = col_labels[c1]
                let2, lbl2 = col_labels[c2]
                for d_vec, d_axis, d_lbl in (
                    (diffs_2_1, f"Paired({let2}-{let1})", f"{lbl2} minus {lbl1}"),
                    ([-d for d in diffs_2_1], f"Paired({let1}-{let2})", f"{lbl1} minus {lbl2}"),
                ):
                    m_d = statistics.mean(d_vec)
                    if m_d > 0:
                        sd_d = statistics.stdev(d_vec)
                        sem_d = sd_d / math.sqrt(len(d_vec))
                        series_list.append({
                            "file": file_label,
                            "sheet": sheet_name,
                            "axis": d_axis,
                            "label": d_lbl[:55],
                            "n": len(d_vec),
                            "mean": m_d,
                            "sd": sd_d,
                            "sem": sem_d,
                            "median": statistics.median(d_vec),
                            "min": min(d_vec),
                            "max": max(d_vec),
                            "is_paired_diff": True,
                        })

    # 2. Row-wise numeric series (for horizontally oriented source tables)
    if len([s for s in series_list if not s.get("is_paired_diff")]) < 2 and n_cols >= 4:
        for r_i, row in enumerate(grid):
            row_label = ""
            nums = []
            for cell in row:
                fv = _try_float(cell)
                if fv is not None:
                    nums.append(fv)
                elif cell and not row_label:
                    row_label = re.sub(r"\s+", " ", cell)[:50]
            if len(nums) >= 3 and row_label and not _is_sequential_index(nums):
                mean_v = statistics.mean(nums)
                sd_v = statistics.stdev(nums)
                sem_v = sd_v / math.sqrt(len(nums))
                med_v = statistics.median(nums)
                series_list.append({
                    "file": file_label,
                    "sheet": sheet_name,
                    "axis": f"Row {r_i + 1}",
                    "label": row_label,
                    "n": len(nums),
                    "mean": mean_v,
                    "sd": sd_v,
                    "sem": sem_v,
                    "median": med_v,
                    "min": min(nums),
                    "max": max(nums),
                    "is_paired_diff": False,
                })

    return series_list


def audit_sheet_placeholders_and_errors(file_label, sheet_name, grid):
    """Scan a worksheet grid for template placeholders, formula errors, and annotated numeric cells (e.g. '0*')."""
    findings = []
    # Find if there is a checklist table header row ("Panel | Provided?") so template instructions above it are not flagged
    checklist_hdr_row = None
    for r_i, row in enumerate(grid[:15]):
        row_join = " ".join(c.lower() for c in row if c)
        if "panel" in row_join and "provided" in row_join:
            checklist_hdr_row = r_i
            break

    asterisk_cells = []
    for r_i, row in enumerate(grid):
        for c_i, cell in enumerate(row):
            if not cell:
                continue
            coord = f"{_idx_to_col_letters(c_i)}{r_i + 1}"
            m_ph = PLACEHOLDER_PAT.search(cell)
            if m_ph:
                if checklist_hdr_row is not None and r_i < checklist_hdr_row and "example for demonstration" in m_ph.group(0).lower():
                    continue
                findings.append(
                    f"  !! [{file_label} :: {sheet_name}!{coord}] Unfilled template placeholder '{m_ph.group(0)}' in cell: {cell[:90]!r}"
                )
            if EXCEL_ERR_PAT.match(cell.strip()):
                findings.append(
                    f"  !! [{file_label} :: {sheet_name}!{coord}] Excel formula error '{cell.strip()}'"
                )
            elif re.match(r"^[-+]?\d+(?:\.\d+)?\*+$", cell.strip()):
                asterisk_cells.append(f"{coord}={cell.strip()!r}")
    if asterisk_cells:
        findings.append(
            f"  [Note] [{file_label} :: {sheet_name}] {len(asterisk_cells)} asterisk-annotated numeric cell(s) "
            f"(e.g. {', '.join(asterisk_cells[:5])}{'...' if len(asterisk_cells) > 5 else ''}; excluded from numeric column stats unless parsed)"
        )
    return findings


def _series_matches_fig_hint(sr, fig_refs):
    """Return True if `sr['sheet']` or `sr['file']` matches any figure number/panel in `fig_refs`.
    Also normalizes supplementary prefixes (EV1C <-> S1C <-> ED1C) so sheet 'Fig S1C' matches text 'Fig EV1C'.
    """
    if not fig_refs:
        return False
    sh_low = (sr["sheet"] + " " + sr["file"]).lower()
    sh_norm = re.sub(r"\b(?:ev|ed|s)(\d+)", r"s\1", sh_low)
    for fr in fig_refs:
        m = re.search(r"((?:ev|ed|s)?\d+)([a-z])?(?:[\u2013\u2014-]([a-z]))?", fr.lower())
        if not m:
            continue
        fnum = m.group(1)
        fnum_norm = re.sub(r"^(?:ev|ed|s)", "s", fnum)
        p1, p2 = m.group(2), m.group(3)
        for cand_f, cand_sh in ((fnum, sh_low), (fnum_norm, sh_norm)):
            if p1 and p2 and p1 <= p2:
                for c in range(ord(p1), ord(p2) + 1):
                    if re.search(rf"\b{re.escape(cand_f)}{chr(c)}\b", cand_sh):
                        return True
            if p1 and re.search(rf"\b{re.escape(cand_f)}{re.escape(p1)}\b", cand_sh):
                return True
            if re.search(rf"\b(?:fig(?:ure)?\.?\s*)?{re.escape(cand_f)}[a-z]?\b", cand_sh):
                return True
    return False


def cross_check_manuscript_claims(ms_txt_path, all_series):
    """Compare 'X ± Y' (mean ± SD/SEM) claims in manuscript text against computed .xlsx series."""
    if not ms_txt_path or not os.path.isfile(ms_txt_path) or not all_series:
        return
    print("\n== 4. MANUSCRIPT 'MEAN ± SD/SEM' vs. EXCEL SOURCE-DATA CROSS-CHECK ==")
    lines = open(ms_txt_path, encoding="utf-8").read().splitlines()
    pm_pat = re.compile(r"(?<![A-Za-z0-9_])(\d+\.\d+|\d+)\s*(?:\u00b1|\+/-)\s*(\d+\.\d+|\d+)(?![A-Za-z0-9_])")
    fig_ctx_pat = re.compile(r"\b(?:Fig(?:ure)?s?\.?|Tables?)\s*(?:EV|ED|S)?\d+[A-Za-z]?(?:[\u2013\u2014-][A-Za-z])?\b", re.I)

    fig_declared_err = {}
    for ln in lines:
        m_leg = re.match(
            r"^\s*(?:\[[^\]]+\]\s*)?(?:\[\[IMAGE:[^\]]+\]\]\s*)*(?:Supplementary\s+|Expanded\s+View\s+)?(?:Figure|Fig\.?)\s+((?:EV|ED|S)?\d+)\s*[.:–—-]\s*(.*)",
            ln,
            re.I,
        )
        if m_leg:
            f_key = m_leg.group(1).lower()
            l_txt = m_leg.group(2)
            has_sd = bool(re.search(r"\b(?:s\.?d\.?|standard\s+deviation)\b", l_txt, re.I))
            has_sem = bool(re.search(r"\b(?:s\.?e\.?m\.?|standard\s+error)\b", l_txt, re.I))
            if has_sd and not has_sem:
                fig_declared_err[f_key] = "SD"
            elif has_sem and not has_sd:
                fig_declared_err[f_key] = "SEM"

    verified_cnt = 0
    discrepancy_cnt = 0

    for line in lines:
        if line.startswith("# MANUSCRIPT-AUDIT EXTRACT"):
            continue
        m_p = re.match(r"^\s*(\[[^\]]+\])\s*(.*)", line)
        ptag = m_p.group(1) if m_p else "body"
        clean = m_p.group(2) if m_p else line

        for m_pm in pm_pat.finditer(clean):
            m_str, s_str = m_pm.group(1), m_pm.group(2)
            m_val, s_val = float(m_str), float(s_str)
            if m_val == 0 and s_val == 0:
                continue
            m_dec = len(m_str.split(".")[1]) if "." in m_str else 0
            s_dec = len(s_str.split(".")[1]) if "." in s_str else 0
            m_tol = 0.55 * (10 ** (-m_dec))
            s_tol = 0.55 * (10 ** (-s_dec))

            win_start = max(0, m_pm.start() - 100)
            win_end = min(len(clean), m_pm.end() + 100)
            snippet = clean[win_start:win_end].strip()
            fig_refs = [f.group(0) for f in fig_ctx_pat.finditer(snippet)]
            if not fig_refs:
                fig_refs = [f.group(0) for f in fig_ctx_pat.finditer(clean)]
            fig_hint = f" (near {', '.join(fig_refs[:3])})" if fig_refs else ""

            declared_err = None
            if re.search(r"\b(?:s\.?d\.?|standard\s+deviation)\b", snippet, re.I):
                declared_err = "SD"
            elif re.search(r"\b(?:s\.?e\.?m\.?|standard\s+error)\b", snippet, re.I):
                declared_err = "SEM"
            elif fig_refs:
                for fr in fig_refs:
                    m_fn = re.search(r"((?:ev|ed|s)?\d+)", fr.lower())
                    if m_fn and m_fn.group(1) in fig_declared_err:
                        declared_err = fig_declared_err[m_fn.group(1)]
                        break

            exact_matches = []
            near_misses = []
            for sr in all_series:
                mean_diff = abs(sr["mean"] - m_val)
                sd_diff = abs(sr["sd"] - s_val)
                sem_diff = abs(sr["sem"] - s_val)
                fig_match = _series_matches_fig_hint(sr, fig_refs)
                paired_pen = 0.01 if sr.get("is_paired_diff") else 0.0
                if mean_diff <= m_tol and (sd_diff <= s_tol or sem_diff <= s_tol):
                    stat_kind = "SD" if sd_diff <= sem_diff else "SEM"
                    exact_matches.append((0 if fig_match else 1, mean_diff + min(sd_diff, sem_diff) + paired_pen, sr, stat_kind))
                elif (
                    (fig_match and mean_diff <= max(m_tol * 2.5, abs(m_val) * 0.03))
                    or (
                        m_dec >= 1
                        and s_dec >= 1
                        and mean_diff <= max(m_tol * 2.8, abs(m_val) * 0.06)
                        and min(sd_diff, sem_diff) <= max(s_tol * 2.8, abs(s_val) * 0.15)
                    )
                ):
                    stat_kind = "SD" if sd_diff <= sem_diff else "SEM"
                    near_misses.append((0 if fig_match else 1, mean_diff + min(sd_diff, sem_diff) + paired_pen, sr, stat_kind))

            fig_near = [x for x in near_misses if x[0] == 0]
            fig_exact = [x for x in exact_matches if x[0] == 0]

            if fig_exact or (exact_matches and not fig_near):
                chosen_list = sorted(fig_exact if fig_exact else exact_matches, key=lambda x: (x[0], x[1]))
                _, _, sr, sk = chosen_list[0]
                if declared_err and declared_err != sk and abs(sr["sd"] - sr["sem"]) > s_tol:
                    discrepancy_cnt += 1
                    print(
                        f"  !! [SD vs SEM MISMATCH] {ptag}{fig_hint}: Manuscript/legend states {declared_err}, "
                        f"but '{m_str} ± {s_str}' matches {sk}={sr[sk.lower()]:.4g} (whereas {declared_err}={sr[declared_err.lower()]:.4g}) "
                        f"in [{sr['file']} :: {sr['sheet']} :: {sr['axis']} '{sr['label']}'] (n={sr['n']})"
                    )
                else:
                    verified_cnt += 1
                    print(
                        f"  [VERIFIED] {ptag}{fig_hint}: '{m_str} ± {s_str}' matches "
                        f"[{sr['file']} :: {sr['sheet']} :: {sr['axis']} '{sr['label']}'] "
                        f"(mean={sr['mean']:.4g}, {sk}={sr[sk.lower()]:.4g}, n={sr['n']})"
                    )
            elif near_misses:
                near_sorted = sorted(fig_near if fig_near else near_misses, key=lambda x: (x[0], x[1]))
                _, _, sr, sk = near_sorted[0]
                discrepancy_cnt += 1
                err_note = f" (declared={declared_err}, closest={sk})" if declared_err and declared_err != sk else ""
                print(
                    f"  !! [SOURCE-DATA DISCREPANCY CANDIDATE] {ptag}{fig_hint}: Manuscript states '{m_str} ± {s_str}'{err_note}, "
                    f"but closest Excel series [{sr['file']} :: {sr['sheet']} :: {sr['axis']} '{sr['label']}'] "
                    f"computes mean={sr['mean']:.4g}, SD={sr['sd']:.4g}, SEM={sr['sem']:.4g} (n={sr['n']})"
                )
                print(f"     Context: ...{snippet}...")
            else:
                # Check for Mean-as-SD conflation: did the author write 'mean(Col A) ± mean(Col B)' for two series on the same sheet?
                non_paired = [sr for sr in all_series if not sr.get("is_paired_diff")]
                conflated = None
                for sr1 in non_paired:
                    if abs(sr1["mean"] - m_val) <= m_tol:
                        for sr2 in non_paired:
                            if sr1 is not sr2 and sr1["file"] == sr2["file"] and sr1["sheet"] == sr2["sheet"]:
                                if abs(sr2["mean"] - s_val) <= s_tol:
                                    f_match = _series_matches_fig_hint(sr1, fig_refs)
                                    conflated = (0 if f_match else 1, sr1, sr2)
                                    if f_match:
                                        break
                    if conflated and conflated[0] == 0:
                        break
                if conflated:
                    _, sr1, sr2 = conflated
                    discrepancy_cnt += 1
                    print(
                        f"  !! [CRITICAL MEAN-AS-SD CONFLATION] {ptag}{fig_hint}: Manuscript writes '{m_str} ± {s_str}' as if it were mean ± SD/SEM, "
                        f"but '{m_str}' matches the MEAN of [{sr1['file']} :: {sr1['sheet']} :: {sr1['axis']} '{sr1['label']}'] (mean={sr1['mean']:.4g}, SD={sr1['sd']:.4g}) "
                        f"and '{s_str}' matches the MEAN of adjacent [{sr2['sheet']} :: {sr2['axis']} '{sr2['label']}'] (mean={sr2['mean']:.4g}, SD={sr2['sd']:.4g})!"
                    )
                    print(f"     Context: ...{snippet}...")

    if verified_cnt == 0 and discrepancy_cnt == 0:
        print("  No direct 'mean ± SD/SEM' matches or near-misses found against numeric columns.")
    else:
        print(f"  Summary: {verified_cnt} exact/rounded match(es), {discrepancy_cnt} discrepancy candidate(s).")


def extract_xfa_pdf_fields(pdf_path):
    """If pdf_path is an Adobe XFA dynamic form PDF (even when /XFA is inside a compressed /ObjStm),
    extract and return a list of (field_tag, text_value).
    """
    try:
        raw = open(pdf_path, "rb").read()
    except Exception:
        return []
    fields = []
    seen_pairs = set()
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        chunk = m.group(1)
        dec = None
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                dec = zlib.decompress(chunk.strip(b"\r\n"), wbits)
                break
            except Exception:
                continue
        if dec is None:
            continue
        if b"<xfa:datasets" not in dec and b"<xfa:data" not in dec and b"<form" not in dec:
            continue
        # Prioritize <xfa:datasets> / <xfa:data> when present so template labels from <template> are not mixed with filled values
        if b"<xfa:datasets" in dec or b"<xfa:data" in dec:
            try:
                root = ET.fromstring(dec.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                val = (elem.text or "").strip()
                if val and len(list(elem)) == 0:
                    clean_v = re.sub(r"\s+", " ", val)
                    if (tag, clean_v) not in seen_pairs:
                        seen_pairs.add((tag, clean_v))
                        fields.append((tag, clean_v))
    return fields


def cross_check_xfa_with_manuscript(pdf_name, fields, ms_txt_path):
    """Cross-check extracted Reporting Summary XFA fields (exclusions, descstats, Dataavail, etc.) against manuscript text."""
    if not ms_txt_path or not os.path.isfile(ms_txt_path) or not fields:
        return
    ms_text = open(ms_txt_path, encoding="utf-8", errors="replace").read()
    ms_low = ms_text.lower()
    print(f"\n  [Reporting Summary <-> Manuscript Cross-Check ({pdf_name})]:")
    notes = 0
    for tag, val in fields:
        t_low = tag.lower()
        v_low = val.lower()
        # 1. Data exclusions check
        if "exclusion" in t_low and ("no data were excluded" in v_low or "none" == v_low):
            for m_ex in re.finditer(r"(\[P\d+\][^\n]*?\b(?:excluded|omitted|discarded|removed\s+from\s+analysis)\b[^\n]*)", ms_text, re.I):
                notes += 1
                print(
                    f"    !! [EXCLUSION CONTRADICTION]: Reporting Summary [{tag}] states \"{val[:100]}\", "
                    f"whereas manuscript states: {m_ex.group(1)[:150]}"
                )
        # 2. Accession codes in Data availability
        if "data" in t_low or "avail" in t_low or "code" in t_low:
            for m_acc in re.finditer(r"\b(PXD\d{5,8}|GSE\d{4,8}|PRJNA\d{4,8}|PRJEB\d{4,8}|EMD-\d{4,6}|[0-9][A-Za-z0-9]{3})\b", val):
                acc = m_acc.group(1)
                if len(acc) >= 6 and acc.lower() not in ms_low:
                    notes += 1
                    print(
                        f"    !! [ACCESSION MISMATCH]: Reporting Summary [{tag}] lists accession '{acc}', which is NOT found in manuscript text!"
                    )
    if notes == 0:
        print("    All checked Reporting Summary fields are consistent with manuscript text (review full field dump above for sample sizes/stats).")


def _parse_delimited_text_to_grid(text_str, sep=None):
    """Parse a CSV or TSV string into a 2D list of cell strings."""
    import csv
    import io
    lines = [ln for ln in text_str.splitlines() if ln.strip()]
    if not lines:
        return []
    if sep is None:
        sep = "\t" if lines[0].count("\t") > lines[0].count(",") else ","
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=sep)
    return [[c.strip() for c in row] for row in reader]


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)

    ms_txt = opt("--manuscript-txt", None) or opt("--compare-text", None)
    dump_rows = int(opt("--dump-rows", "8"))
    default_tsv = "scratch_audit/xlsx_tsv" if os.path.isdir("scratch_audit") else None
    out_tsv_dir = opt("--out-tsv", default_tsv)

    targets = []
    csv_targets = []
    zip_targets = []
    xfa_pdfs = []
    skip_next = False
    for a in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a in ("--manuscript-txt", "--compare-text", "--dump-rows", "--out-tsv"):
            skip_next = True
            continue
        if a.startswith("-"):
            continue
        if os.path.isdir(a):
            for root_d, _, files in os.walk(a):
                if "scratch_audit" in root_d or "audited" in root_d.lower():
                    continue
                for fn in sorted(files):
                    full_p = os.path.join(root_d, fn)
                    low_fn = fn.lower()
                    if low_fn.endswith(".xlsx") and not fn.startswith("~$") and "audited" not in low_fn:
                        targets.append(full_p)
                    elif low_fn.endswith((".csv", ".tsv")) and not fn.startswith("~$"):
                        csv_targets.append(full_p)
                    elif low_fn.endswith(".zip"):
                        zip_targets.append(full_p)
                    elif low_fn.endswith(".pdf") and "audited" not in low_fn:
                        xfa_pdfs.append(full_p)
        elif os.path.isfile(a):
            low_a = a.lower()
            if low_a.endswith(".xlsx"):
                targets.append(a)
            elif low_a.endswith((".csv", ".tsv")):
                csv_targets.append(a)
            elif low_a.endswith(".zip"):
                zip_targets.append(a)
            elif low_a.endswith(".pdf"):
                xfa_pdfs.append(a)

    if not targets and not csv_targets and not zip_targets and not xfa_pdfs:
        print("No .xlsx, .csv/.tsv, .zip, or XFA .pdf files found in the specified path(s) (skipping Excel/form audit).")
        return

    all_findings = []
    all_series = []

    if targets or csv_targets or zip_targets:
        print(f"== 1. EXCEL / CSV / ARCHIVE SOURCE-DATA INVENTORY ({len(targets)} .xlsx, {len(csv_targets)} .csv/.tsv, {len(zip_targets)} .zip) ==")

        for xp in targets:
            f_lbl = os.path.basename(xp)
            try:
                sheets = read_xlsx_workbook(xp)
            except Exception as e:
                print(f"\nWorkbook: {f_lbl} — ERROR reading .xlsx archive: {e}")
                continue

            print(f"\nWorkbook: {f_lbl} ({len(sheets)} sheet(s))")
            for sh_name, grid in sheets:
                non_empty_rows = [r for r in grid if any(c != "" for c in r)]
                n_r = len(grid)
                n_c = max((len(r) for r in grid), default=0)
                print(f"  Sheet '{sh_name}': {n_r} row(s) x {n_c} col(s) ({len(non_empty_rows)} non-empty row(s))")
                for r_i, row in enumerate(grid[:dump_rows]):
                    trimmed = [c for c in row]
                    while trimmed and trimmed[-1] == "":
                        trimmed.pop()
                    if trimmed:
                        preview = " | ".join(re.sub(r"\s+", " ", c)[:42] for c in trimmed[:10])
                        print(f"    [R{r_i + 1}] {preview}")

                if out_tsv_dir:
                    os.makedirs(out_tsv_dir, exist_ok=True)
                    safe_sh = re.sub(r"[^A-Za-z0-9._-]+", "_", sh_name)
                    tsv_path = os.path.join(out_tsv_dir, f"{os.path.splitext(f_lbl)[0]}__{safe_sh}.tsv")
                    with open(tsv_path, "w", encoding="utf-8") as tf:
                        for row in grid:
                            tf.write("\t".join(re.sub(r"[\t\r\n]+", " ", c) for c in row) + "\n")

                all_findings.extend(audit_sheet_placeholders_and_errors(f_lbl, sh_name, grid))
                all_series.extend(compute_sheet_series_stats(f_lbl, sh_name, grid))

        for cp in csv_targets:
            c_lbl = os.path.basename(cp)
            try:
                raw_c = open(cp, encoding="utf-8", errors="replace").read()
                grid = _parse_delimited_text_to_grid(raw_c)
                print(f"\nCSV/TSV File: {c_lbl} ({len(grid)} row(s))")
                for r_i, row in enumerate(grid[:dump_rows]):
                    if any(row):
                        print(f"    [R{r_i + 1}] {' | '.join(c[:42] for c in row[:10])}")
                all_findings.extend(audit_sheet_placeholders_and_errors(c_lbl, "CSV", grid))
                all_series.extend(compute_sheet_series_stats(c_lbl, "CSV", grid))
            except Exception as e:
                print(f"\nCSV/TSV File: {c_lbl} — ERROR: {e}")

        # Inspect .zip archives for .csv/.tsv/.xlsx source data, sequence/tree files (.fasta/.aln/.nwk), and filename typos
        typo_fn_pat = re.compile(r"(allign|suplement|figrue|tabel|experimnet|refferenc)", re.I)
        for zp in zip_targets:
            z_lbl = os.path.basename(zp)
            try:
                with zipfile.ZipFile(zp, "r") as zf:
                    names = [n for n in sorted(zf.namelist()) if not n.startswith("__MACOSX") and not os.path.basename(n).startswith(".")]
                    print(f"\nZIP Archive: {z_lbl} ({len(names)} entry/entries)")
                    for nm in names[:40]:
                        print(f"  - {nm}")
                        m_tfn = typo_fn_pat.search(nm)
                        if m_tfn:
                            all_findings.append(f"  !! [{z_lbl} :: {nm}] Suspected spelling typo '{m_tfn.group(0)}' in archived filename/path!")
                        low_nm = nm.lower()
                        if low_nm.endswith((".csv", ".tsv")) and not nm.endswith("/"):
                            try:
                                txt_z = zf.read(nm).decode("utf-8", errors="replace")
                                grid = _parse_delimited_text_to_grid(txt_z)
                                sh_lbl = os.path.basename(nm)
                                all_findings.extend(audit_sheet_placeholders_and_errors(z_lbl, sh_lbl, grid))
                                all_series.extend(compute_sheet_series_stats(z_lbl, sh_lbl, grid))
                            except Exception:
                                pass
                        elif low_nm.endswith((".fasta", ".fa", ".faa", ".fna", ".aln", ".clustal")) and not nm.endswith("/"):
                            try:
                                txt_seq = zf.read(nm).decode("utf-8", errors="replace")
                                headers = [ln[1:].strip()[:60] for ln in txt_seq.splitlines() if ln.startswith(">")]
                                if headers:
                                    print(f"      [Sequence Alignment '{os.path.basename(nm)}': {len(headers)} FASTA sequence(s); first 5: {headers[:5]}]")
                            except Exception:
                                pass
            except Exception as e:
                print(f"\nZIP Archive: {z_lbl} — ERROR reading zip: {e}")

        print("\n== 2. TEMPLATE PLACEHOLDERS, FORMULA ERRORS & CHECKLIST COMPLETENESS ==")
        if all_findings:
            for f_msg in all_findings:
                print(f_msg)
        else:
            print("  None detected.")

        print("\n== 3. SOURCE-DATA STATISTICAL SUMMARY (n, mean, SD, SEM, median) ==")
        if all_series:
            for sr in all_series:
                print(
                    f"  [{sr['file']} :: {sr['sheet']} :: {sr['axis']}] {sr['label'][:48]:<48s} | "
                    f"n={sr['n']:<4d} | mean={sr['mean']:<10.4g} | SD={sr['sd']:<10.4g} | "
                    f"SEM={sr['sem']:<10.4g} | median={sr['median']:<10.4g} | range=[{sr['min']:.4g}..{sr['max']:.4g}]"
                )
        else:
            print("  No numeric data series (>=2 values) detected.")

        if ms_txt:
            cross_check_manuscript_claims(ms_txt, all_series)

    for pdf_p in xfa_pdfs:
        fields = extract_xfa_pdf_fields(pdf_p)
        pdf_bn = os.path.basename(pdf_p)
        if fields:
            print(f"\n== 5. ADOBE XFA PDF FORM / REPORTING SUMMARY FIELDS ({pdf_bn}: {len(fields)} field(s)) ==")
            for tag, val in fields:
                print(f"  [{tag}]: {val}")
            if ms_txt:
                cross_check_xfa_with_manuscript(pdf_bn, fields, ms_txt)
        elif any(k in pdf_bn.lower() for k in ("reporting", "summary", "checklist", "form")):
            print(f"\n== 5. ADOBE XFA PDF FORM / REPORTING SUMMARY FIELDS ({pdf_bn}) ==")
            print("  (No XFA XML datasets stream detected — inspect standard PDF page text via render_figs.py info/page-text.)")


if __name__ == "__main__":
    main()
