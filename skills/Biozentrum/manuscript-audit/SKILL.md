---
name: manuscript-audit
description: >-
  Perform a comprehensive pre-submission or post-review resubmission audit of a
  scientific manuscript package (main manuscript text in DOCX/LaTeX/PDF, main and
  supplementary figure PDFs/images, tables, video legends, and optional reviewer
  rebuttal letter). Checks reference/citation integrity and topical accuracy,
  figure/panel/table/video callout completeness and ordering, orphan Methods
  sections, panel-by-panel figure-vs-legend fidelity, quantitative/statistical
  data consistency across text and graphs, dilution/recipe arithmetic,
  nomenclature/spelling uniformity, and rebuttal-vs-manuscript alignment, then
  generates three synchronized deliverables: (1) a self-contained HTML audit
  report with cropped visual evidence, verbatim quotes, and drop-in corrections,
  (2) copies of the original Word (.docx) files with native Track Changes
  (<w:del>/<w:ins>) and anchored Margin Comments, and (3) copies of the original
  Figure PDFs with native PDF highlight rectangles (/Subtype /Square) and
  sticky-note popup comments (/Subtype /Text + /Popup) layered directly on top
  via PDF incremental update.
---

# Scientific Manuscript Pre-Submission & Resubmission Audit

Use this skill to perform a rigorous, end-to-end quality and internal-consistency audit of a scientific manuscript before **initial submission** or **post-review resubmission**, producing three synchronized deliverables:
1. **Self-Contained HTML Audit Report** (`manuscript_review_report.html`, via [scripts/build_html_report.py](./scripts/build_html_report.py))
2. **Annotated Word Documents with Native Track Changes & Margin Comments** (`*_audited.docx`, via [scripts/annotate_docx.py](./scripts/annotate_docx.py))
3. **Annotated Figure PDFs with Native PDF Highlight Boxes & Sticky-Note Comments** (`*_audited.pdf`, via [scripts/annotate_pdf.py](./scripts/annotate_pdf.py))

For the exhaustive catalog of 70+ domain-agnostic failure modes across all check categories, read [references/checklist.md](./references/checklist.md).

---

## Harness-Agnostic & Smaller-Model Guardrails (READ FIRST)

This skill is designed to work across **any agent harness** (OpenCode, Claude Code, Cursor, Aider, Cline, Windsurf, Goose, etc.) and with **both frontier and smaller/local models**. Follow these strict rules at all times:

1. **Locate `SKILL_DIR` First**: Set `SKILL_DIR` to the directory containing this `SKILL.md` file (e.g., `./manuscript-audit` or `~/.config/opencode/skills/manuscript-audit`). Run all helper scripts via `python3 "$SKILL_DIR/scripts/<script>.py"`.
2. **Never Read `.docx` or `.pdf` Files as Raw Text**: Always extract `.docx` files with `docx2txt.py` (Step 1) and render `.pdf` files to `.png` images with `render_figs.py` (Step 3) before inspecting them.
3. **Never Overwrite Original User Files & Never Use `/tmp/`**: Write all temporary/extracted files into a workspace-local `./scratch_audit/` directory (never `/tmp/`, which can contain stale files from earlier audits on the same host) and write final annotated outputs to `*_AUDITED.docx` and `*_AUDITED.pdf`.
4. **Never Skip Any Figure Page**: You must visually inspect **every single rendered page** (`main_1.png` .. `main_N.png`, `supp_1.png` .. `supp_N.png`) one by one. Do not stop after the first few figures.
5. **Never Guess Crop or PDF Box Coordinates (and Beware Chat Preview Downscaling/Caching)**: Always render with `--grid` (`main_1_grid.png`) and read the red pixel ruler numbers `(x0, y0, x1, y1)` printed directly on the grid image—or run `render_figs.py find-text Main-figure.pdf "Panel Label"`—before running `render_figs.py crop` or `annotate_pdf.py`. Never estimate coordinates from an ungridded image (chat viewers often auto-downscale tall images to `<=2000 px`), never count small replicate dots on a downscaled full-page preview (crop the graph at `400 dpi` first), and if you re-crop a region, write to a new filename (`fig5c_v2.jpg`) so the viewer does not display a cached image.
6. **Never Paraphrase Quotes or Emit Targets from Memory (Prevent Mid-Audit Note Drift)**: Never type a quote or `.docx` edit target from your own working notes or summary memory. Always record the `[P#]` paragraph ID from `manuscript_indexed.txt` at finding-discovery time, pass `"quote_para": "P38"` (or `"Rebuttal:P7"`) in `report_spec.json`, and run `preflight_edits.py` (Step 7) to verify every edit target and `quote_para` against ground-truth files before building deliverables.
7. **Windows PowerShell / `cmd.exe` -> WSL Quoting Rule**: If your host shell is Windows PowerShell or `cmd.exe` invoking `wsl` or `bash -c`, **never** pass inline Python one-liners, nested quotes, or `$VAR` expansions inside `bash -c "..."` (PowerShell strips/expands `$` tokens and mangles quotes before bash receives them). Instead, write a `.sh` or `.py` file into `scratch_audit/` using your file-write tool and run `wsl bash scratch_audit/run_step.sh`.
8. **Zero-Match Search Sanity Check (`[P107]`, not `P107:`)**: `docx2txt.py --indexed` formats paragraph markers as bracketed `[P1]`, `[P2]`, ..., `[P107]` and tables as `[TABLE 1]`, `[Row 1]`. Never search for `P107:` with a colon. Whenever a search/grep returns `0 matches`, inspect 5 lines of the target file first to confirm its formatting before concluding anything is missing.
9. **Never Claim Completion Without Running `verify_deliverables.py`**: In long sessions or after context compaction, never assume deliverables were already written to disk. You **must** run `verify_deliverables.py` in Step 8 and see `OVERALL STATUS: PASS` from the actual filesystem before finishing.

---

## Step 0 — Host Software, Dependencies & Offline Workarounds

### 0.1 What Runs with Zero External Dependencies (Pure Python 3 Standard Library)
Almost every script in this skill uses **only the Python 3 standard library** (`python3 >= 3.8`, no `pip` packages or system binaries required):
- `docx2txt.py` (Word `.docx` indexed text extraction with provenance SHA-256 header, automatic Zotero/Mendeley/EndNote `<w:sdt>` content-control unwrapping, Office MathML `<m:oMath>` equation linearization, `<w:br/>` soft-break sub-indexing `[P1.1]`, `[P1.2]`, `--extract-refs` bibliography extraction, media extraction, and XML formatting/line-drift/citation-field diagnostics)
- `ref_audit.py`, `deep_ref_audit.py`, `callout_audit.py`, `consistency_audit.py` (automated text, numbered + Author–Year/Zotero citation, cached Crossref + NCBI GEO verification, legend completeness matrix, `EV`/Expanded View + lowercase `(a)` panel callout, math, cover-letter, and rebuttal audits, with `--ignore` / `scratch_audit/audit_ignore.txt` support)
- `xlsx_audit.py` (pure-Python `.xlsx` workbook/sheet reader, template placeholder & formula-error scanner, column/row `n / mean / SD / SEM / median` statistical calculator, and automated manuscript `mean ± SD/SEM` cross-checker — works without `openpyxl` or `pandas`)
- `preflight_edits.py` (2-second preflight validator for `.docx` edit targets, `.pdf` annotation boxes, and `report_spec.json` quotes/images, with `--autofix-para` relocation)
- `annotate_docx.py` (injects native Word `<w:del>`/`<w:ins>` Track Changes and Margin Comments into `.docx` copies, supporting `<w:sdt>` controls, `<w:br/>` soft breaks, inline citation fields, curly/straight quote normalization, and fuzzy near-miss hints)
- `annotate_pdf.py` (injects native PDF `/Subtype /Square` highlight boxes and `/Subtype /Text` sticky-note comments onto `.pdf` copies via ISO 32000-1 incremental update, supporting both classic PDFs and Adobe Illustrator `/Type /ObjStm` + `/Type /XRef` compressed-object-stream PDFs)
- `build_html_report.py`, `validate_html.py`, `verify_quotes.py`, `verify_deliverables.py` (compiles and validates the self-contained HTML report with automatic section finding counts and verifies all final deliverables + extract provenance on disk)
- `render_figs.py size`, `render_figs.py page-text`, `render_figs.py extract-embedded`, and `render_figs.py video` (pure-Python PDF page size parser including `/ObjStm`, PDF-vs-DOCX page text comparator, embedded JPEG/PNG image extractor, and video/zip-archive metadata inspector)

### 0.2 What Needs a Host PDF Renderer (and How to Install or Work Around It)
Full-page PDF rasterization (`render_figs.py render` and `render_figs.py crop`) requires **Python `Pillow` (`PIL`)** plus **any one** of the following PDF backends (auto-detected in this order):
1. **PyMuPDF** (`pymupdf` / `fitz` Python package) — *fastest, self-contained, no system binaries needed*
2. **pypdfium2** (`pypdfium2` Python package)
3. **Poppler CLI** (`pdftoppm` or `pdftocairo`)
4. **Ghostscript CLI** (`gs`)
5. **ImageMagick CLI** (`magick` or `convert`)

**Run the environment check first:**
```bash
SKILL_DIR="./manuscript-audit"   # adjust to path of this skill folder
mkdir -p scratch_audit
python3 "$SKILL_DIR/scripts/render_figs.py" check-env
```

- **If `STATUS: READY` is printed**: Proceed directly to Step 1 (`render_figs.py` also auto-discovers `PIL`/`pymupdf` inside local `.venv`, `conda`, `miniforge`, `pipx`, `rattler`, and `homebrew` environments even if the default system `python3` lacks them).
- **If `STATUS: PARTIAL` is printed and you have package-install access**, run **one** of:
  ```bash
  # Option A (Cross-platform Python — works on Linux, macOS, Windows without root):
  pip install pymupdf pillow          # or: pip3 install --user pymupdf pillow

  # Option B (Ubuntu / Debian Linux):
  sudo apt-get update && sudo apt-get install -y poppler-utils python3-pil

  # Option C (macOS with Homebrew):
  brew install poppler && pip3 install pillow

  # Option D (Conda / Mamba):
  conda install -c conda-forge pymupdf pillow
  ```
- **If you have NO internet / NO install permissions (Air-Gapped / Restricted Sandbox Workaround)**:
  Use the built-in **100% pure-Python standard-library image extractor**:
  ```bash
  python3 "$SKILL_DIR/scripts/render_figs.py" size Main-figure.pdf
  python3 "$SKILL_DIR/scripts/render_figs.py" extract-embedded Main-figure.pdf scratch_audit/main_embedded
  python3 "$SKILL_DIR/scripts/render_figs.py" extract-embedded Supplementary-figure.pdf scratch_audit/supp_embedded
  ```
  This decompresses and reconstructs every embedded JPEG (`/DCTDecode`) and PNG (`/FlateDecode`) panel/graph image directly from the PDF binary stream using only Python `zlib` and `struct`, allowing you to inspect and embed the figure panels in the HTML report with zero external packages.

---

## Supported Audit Modes

- **Mode A — First Submission (Pre-Submission) Mode**:
  - **Inputs**: Manuscript text (`.docx`, `.tex`, `.pdf`, or `.md`), Main Figures (`.pdf`/images), Supplementary Figures/Tables/Legends, and optional Video legends.
  - **Run**: Steps 0, 1, 2, 3, 4, 5, 7, 8 (skip Step 6 Rebuttal cross-audit).
- **Mode B — Post-Review Resubmission Mode**:
  - **Inputs**: All First Submission inputs **plus** the Reviewer Rebuttal / Response Letter (`.docx`, `.pdf`, or `.md`).
  - **Run**: All Steps 0 through 8.

---

## Step-by-Step Execution Recipe

### Step 1 — Extract Paragraph-Indexed Text, References, Embedded Media & Formatting Diagnostics

Run [scripts/docx2txt.py](./scripts/docx2txt.py) on every `.docx` file in the submission package (manuscript, rebuttal letter, and cover letter if present):

```bash
# 1. Extract paragraph-indexed text ([P1], [P2], ..., [TABLE 1] for tables) and standalone reference list
python3 "$SKILL_DIR/scripts/docx2txt.py" Text.docx --indexed > scratch_audit/manuscript_indexed.txt
python3 "$SKILL_DIR/scripts/docx2txt.py" Text.docx --extract-refs scratch_audit/refs.txt

# 2. Check Word continuous line-numbering, unsuppressed empty-paragraph line drift, font-size overrides, and embedded media
python3 "$SKILL_DIR/scripts/docx2txt.py" Text.docx --audit-format > scratch_audit/format_audit.txt
python3 "$SKILL_DIR/scripts/docx2txt.py" Text.docx --media scratch_audit/ms_media

# 3. (If present) Extract Rebuttal Letter and Cover Letter text + embedded media
python3 "$SKILL_DIR/scripts/docx2txt.py" Rebuttal_comments.docx --indexed > scratch_audit/rebuttal_indexed.txt
python3 "$SKILL_DIR/scripts/docx2txt.py" Rebuttal_comments.docx --media scratch_audit/rebuttal_media
python3 "$SKILL_DIR/scripts/docx2txt.py" Cover_letter.docx --indexed > scratch_audit/cover_indexed.txt
```

Read `scratch_audit/manuscript_indexed.txt`, `scratch_audit/format_audit.txt`, and (if present) `scratch_audit/rebuttal_indexed.txt` and `scratch_audit/cover_indexed.txt` in full.

---

### Step 2 — Run Automated Reference, Deep Bibliography, Callout, Consistency & Excel Source-Data Pre-Checks

Run all five zero-dependency static/bibliographic/spreadsheet analyzers and read their outputs:

```bash
# 2A. Reference numbering or Author-Year/Zotero inventory, citation style consistency ((n) vs [n] vs superscript vs Author-Year),
#     duplicate-year citation artifacts ("Author et al, 2019, 2019"), duplicate bibliography detection, & sentence-to-title map:
python3 "$SKILL_DIR/scripts/ref_audit.py" scratch_audit/manuscript_indexed.txt --docx Text.docx > scratch_audit/ref_audit_out.txt

# 2B. Deep bibliography audit: local metadata completeness + cached live Crossref & NCBI GEO verification (title, first author, year,
#     ISO-4 normalized journal name, retractions, preprint->published checks; auto-falls back to offline mode) + manual REVIEW QUEUE:
python3 "$SKILL_DIR/scripts/deep_ref_audit.py" scratch_audit/manuscript_indexed.txt > scratch_audit/deep_ref_audit_out.txt

# 2C. Figure, Table, Video, Panel callout order (supports S#, EV#, ED#, and both (A) and (a) panel styles), missing/phantom callouts,
#     overlapping adjacent callouts, & bidirectional Methods <-> Figure reagent/assay coverage + Legend Completeness Matrix:
python3 "$SKILL_DIR/scripts/callout_audit.py" scratch_audit/manuscript_indexed.txt > scratch_audit/callout_audit_out.txt

# 2D. Consecutive duplicate words, garbled duplicated clauses, Δ/∆ symbol consistency, scientific homophones, singleton/bigram typos,
#     unit/spacing hygiene, dilution math verification, missing journal declaration sections, plus Rebuttal and Cover Letter cross-checks:
python3 "$SKILL_DIR/scripts/consistency_audit.py" scratch_audit/manuscript_indexed.txt \
  --rebuttal scratch_audit/rebuttal_indexed.txt \
  --cover-letter scratch_audit/cover_indexed.txt \
  --format-audit scratch_audit/format_audit.txt > scratch_audit/consistency_audit_out.txt

# 2E. (If .xlsx Source Data, Supplementary Tables, or Checklists are present) Pure-Python .xlsx audit:
#     dumps sheet dimensions/headers, flags unfilled template placeholders (<insert here>) & formula errors, computes column/row
#     n / mean / SD / SEM / median, and cross-checks 'mean ± SD/SEM' claims in manuscript_indexed.txt against the Excel sheets:
python3 "$SKILL_DIR/scripts/xlsx_audit.py" . --manuscript-txt scratch_audit/manuscript_indexed.txt --out-tsv scratch_audit/xlsx_tsv > scratch_audit/xlsx_audit_out.txt
```

**Manual Follow-Up Required in Step 2**:
- In `scratch_audit/ref_audit_out.txt` (Section 5) and `scratch_audit/deep_ref_audit_out.txt` (Section 3 `REVIEW QUEUE`), verify that each cited reference actually supports the factual claim in the citing sentence (correct organism/species, correct cellular compartment, correct direction of effect). Never auto-report an item from `REVIEW QUEUE` without reading the sentence and reference title first—methods/software citations naturally share fewer surface keywords with domain prose.
- Verify citations inside Tables (`[TABLE 1]`, `[Row 1]`, ...) against the numbered bibliography.

---

### Step 3 — Map PDF Pages (with DOCX Cross-Check), Render Figure PDFs with Pixel Grids & Storyboard Videos

- **Case 3A — Figures are provided as standalone PDF files (`Main-figure.pdf`, `Supplementary-figure.pdf`)**:
  First print the per-page figure label and text map with `page-text --compare-text scratch_audit/manuscript_indexed.txt` (this maps every PDF page to its `Figure X` label, prints exact `@200dpi` pixel dimensions per page, AND automatically flags any embedded PDF strain/gene/construct ID or spelling typo that disagrees with the `.docx` manuscript text!), then render every page at `200 dpi` with `--grid` enabled:
  ```bash
  python3 "$SKILL_DIR/scripts/render_figs.py" page-text Main-figure.pdf --compare-text scratch_audit/manuscript_indexed.txt > scratch_audit/main_pdf_map.txt
  python3 "$SKILL_DIR/scripts/render_figs.py" page-text Supplementary-figure.pdf --compare-text scratch_audit/manuscript_indexed.txt > scratch_audit/supp_pdf_map.txt
  python3 "$SKILL_DIR/scripts/render_figs.py" render Main-figure.pdf scratch_audit/pages --dpi 200 --prefix main --grid
  python3 "$SKILL_DIR/scripts/render_figs.py" render Supplementary-figure.pdf scratch_audit/pages --dpi 200 --prefix supp --grid
  ```
- **Case 3B — Figures are embedded inside the `.docx` (extracted into `scratch_audit/ms_media/` in Step 1) or provided as `.png`/`.jpg`/`.tif` files**:
  First wrap the image(s) into a standard uncompressed `/Page` PDF at `200 dpi` (so `1 image pixel == 1 px @ 200 dpi` in both `render_figs.py` and `annotate_pdf.py`), then render with `--grid`:
  ```bash
  # Wrap all extracted manuscript images into a single multi-page PDF (or wrap individual figures into Figure1.pdf, etc.):
  python3 "$SKILL_DIR/scripts/render_figs.py" wrap-images scratch_audit/ms_media Figures.pdf --dpi 200
  python3 "$SKILL_DIR/scripts/render_figs.py" render Figures.pdf scratch_audit/pages --dpi 200 --prefix fig --grid
  ```
- **Case 3C — Supplementary Videos (`.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`, or `.zip` archives containing videos) are present**:
  Run `render_figs.py video` to auto-extract any zipped movie archives, inspect container metadata (resolution, duration, fps, codec, and whether an unintentional audio stream is present—even when `ffmpeg` is absent via pure-Python MP4/MOV/AVI header parsing), and build a 6-frame timestamped storyboard contact sheet (`*_storyboard.jpg`) when `ffmpeg` is installed:
  ```bash
  python3 "$SKILL_DIR/scripts/render_figs.py" video . scratch_audit/videos --frames 6
  ```
  View every `scratch_audit/videos/*_storyboard.jpg` image and compare against the manuscript's `Video S1..Sn` / `Movie EV1..EVn` legends (checking time stamps, scale bars, channel colors, and whether the biological event described in the legend is visible). If `[VIDEO AUDIT GAP — NO FFMPEG]` is printed, include that note in the HTML report.

This creates:
- Clean full-page images (`main_1.png`, `main_2.png`, ...) for visual inspection.
- Grid-overlay images (`main_1_grid.png`, `main_2_grid.png`, ...) with pixel ruler lines every `100 px` and red coordinate labels every `200 px`.

---

### Step 4 — Deep 10-Point Panel-by-Panel Visual & Legend Audit

View **every single rendered figure page** (`main_1.png` .. `main_N.png`, `supp_1.png` .. `supp_N.png`) alongside its verbatim Figure Legend in `scratch_audit/manuscript_indexed.txt`. Check every panel `(A, B, C, ...)` against these **10 mandatory points**:

1. **Title vs. Content**: Does the figure title claim something not shown in that figure (e.g., *"in the nucleus or cytoplasm"* when cytoplasmic data are only in a Supplementary Figure)? Does any image label have a spelling typo (e.g., `"Pre-proccessed"`)?
2. **All Visual Elements Explained**: Are all schematics/cartoons, dashed zoom boxes, magnified insets, dotted outlines, arrows/arrowheads, and asterisks (`*`) on micrographs explained in the legend?
3. **Arrows/Markers Point to What the Legend Claims**: Verify that arrows described as pointing to *"nuclei"* do not actually point to *cytosolic bacterial clusters* or vice versa.
4. **Scale Bars**:
   - Is a scale bar present in **every** image/row/example, and stated in the legend with its `µm`/`nm` value?
   - If a panel has both an overview and a magnified inset, are **both** scale bar lengths stated, and does the inset scale bar length match the physical zoom ratio of the dashed ROI box?
5. **Multi-Row / Multi-Column Layout Parity**: If a combined legend `(B,C)` states *"Scale bars represent 50 µm in the overview image and 10 µm in the magnified image"*, verify that **both** B and C actually have the overview column and condition title header.
6. **Sample Size (`n`) & Plotted Dots**: Count the individual replicate dots plotted on every bar/scatter graph and compare against `n = ...` in the legend and Results text (e.g., 4 dots plotted on the DMSO bar vs. `"n = 3"` in the legend).
7. **Error Bars & Central Tendency**: Does every graph with error bars state `mean ± SD` or `mean ± SEM`?
8. **Statistical Tests & Unused Symbols**: Are the test name, sidedness (`two-tailed`), and multiple-comparison correction stated? Remove unused significance symbols (`**`, `***`, `****`) copied from boilerplate legends if those symbols do not appear on the graph.
9. **Axis Labels & Panel Order**: Check for axis typos (e.g., `"per 1000 infected cell"` singular) and check whether a legend describes left/right graphs in reverse order.
10. **Cross-Legend Parameter Consistency**: Compare `MOI`, imaging time windows (`14 to 17 hpi` vs. `15 to 17 hpi`), and vehicle concentrations across main and supplementary legends for the same experiment.

Whenever you find a visual issue on page `P`, look at `main_P_grid.png` (or `supp_P_grid.png`) to read the `[x0, y0, x1, y1]` pixel coordinates around the panel—or query vector PDF text coordinates directly with `find-text`—and crop a high-resolution (`400 dpi`) evidence image for the HTML report:

```bash
# Optional: locate exact @200dpi pixel coordinates [x0, y0, x1, y1] of any text/panel label on the PDF page:
python3 "$SKILL_DIR/scripts/render_figs.py" find-text Main-figure.pdf "Figure 5"

# Crop the region at 400 dpi using @200dpi pixel coordinates:
python3 "$SKILL_DIR/scripts/render_figs.py" crop Main-figure.pdf 5 540 1285 1130 1745 --dpi 400 --units px --ref-dpi 200 --out scratch_audit/crops/fig5c.jpg
```

After creating all crops, build and view a contact sheet to verify no crop is clipped or mis-aimed:
```bash
python3 "$SKILL_DIR/scripts/render_figs.py" contact-sheet scratch_audit/crops --out scratch_audit/crops/contact_sheet.png
```

---

### Step 5 — Quantitative, Statistical, Physical & Source-Protocol Triangulation

1. **Text Numbers ↔ Main Figure Graph ↔ Supplementary Graph**:
   - Whenever the Results text states numerical counts or percentages for conditions/strains (e.g., `WT = 86 (67 + 19)`, `ΔgeneA = 38 (33 + 5)`, `ΔgeneB = 7 (6 + 1)`), check the plotted bar heights above each x-axis label in **both** the Main Figure and Supplementary Figure to catch **swapped bars/labels**.
2. **Mathematical & Algorithmic Feasibility**:
   - Check whether image-analysis or scatter-plot descriptions are mathematically possible (e.g., a scatter plot where each `(x, y)` dot is a pixel cannot plot `x` across *all image pixels* and `y` only within *nuclear ROI pixels*—check the Methods section to see which is correct).
3. **Dilution, Molarity & Recipe Arithmetic**:
   - Review the dilution checks in `scratch_audit/consistency_audit_out.txt` (`V_added / V_total` vs. stated `%`) and verify buffer/gel/stock concentrations, neutralization stoichiometry, and area/volume conversions (e.g., `3 × 3 mm = 9 mm² = 0.09 cm²`).
4. **Physical & Optical Plausibility**:
   - Verify that physical and resolution claims obey fundamental limits (e.g., optical diffraction limit `~200–250 nm` laterally on a confocal microscope; distinguishing **pre-expansion effective scale** vs. **post-expansion physical gel scale** in expansion microscopy).
5. **Central Quantitative Claims & Bidirectional Methods Coverage**:
   - Verify that headline quantitative claims in the Abstract/Results (e.g., expansion factors, fold-changes, inhibition percentages) have (a) a measurement procedure in Materials & Methods, (b) explicit sample sizes (`n`), and (c) a quantification panel or `mean ± SD/SEM`.
   - Review Section 6 of `scratch_audit/callout_audit_out.txt` for **both** Orphan Methods subsections (methods with no Results/Figures) and Figure assays/reagents missing from Materials & Methods.
6. **Published Source-Protocol Cross-Check**:
   - When the manuscript adapts a named published protocol or cites a reference for specific reagent ratios, chemistry, or physical benchmarks, cross-check the manuscript's stated values and terminology against the cited protocol (using web search if available in your harness).

---

### Step 6 — Rebuttal Letter & Cover Letter ↔ Manuscript Cross-Audit *(When Present)*

Compare every paragraph of `scratch_audit/rebuttal_indexed.txt` and `scratch_audit/cover_indexed.txt` against `scratch_audit/manuscript_indexed.txt` and the figures (using Sections 6 and 7 of `scratch_audit/consistency_audit_out.txt`):
1. **Unfulfilled Promises & Draft Leftovers**: Check every `"will change"`, `"can re-analyze"`, or `"would be happy to include"` flagged in `consistency_audit_out.txt`. Verify whether promised changes (e.g., changing the manuscript title) were actually applied to `Text.docx`.
2. **Main Text vs. Supplementary Location Contradictions**: If the rebuttal claims experiments were *"limited to the Supplementary Information"*, check whether they are still in the Main Text and Main Figures.
3. **Wrong Figure / Video / Line-Number Citations**:
   - Review Section `6a` of `consistency_audit_out.txt`, which pairs every `lines X–Y` citation in the rebuttal with both the non-empty line number and the Word-with-unsuppressed-empty-spacers line number (`+N` drift offset from `format_audit.txt`).
   - Review Sections `6d` and `7a` of `consistency_audit_out.txt`, which print every `Figure X`, `Fig. SX`, and `Video SX` cited in the Rebuttal and Cover Letter side-by-side against the manuscript's actual legend titles (catching misnumbered figure citations such as citing `Fig. S5` for an experiment that is actually `Fig. S6`, or citing `Figure 7` in the cover letter when the paper only has `Figures 1–6`).
4. **Embedded Rebuttal Figures**: Inspect all images in `scratch_audit/rebuttal_media/` and check if they lack figure legends, channel color labels, or scale bar values.

---

### Step 7 — Preflight-Validate All Specs, Then Generate the Three Synchronized Deliverables

#### 7A. Initialize & Populate `report_spec.json` (`build_html_report.py --init-template` FIRST)

**Always** initialize `scratch_audit/report_spec.json` with `--init-template` first and edit that template so field names never drift:

```bash
python3 "$SKILL_DIR/scripts/build_html_report.py" --init-template scratch_audit/report_spec.json
```

The template structure used by [scripts/build_html_report.py](./scripts/build_html_report.py) is:

```json
{
  "title": "Pre-Submission / Resubmission Manuscript Audit Report",
  "subtitle": "Manuscript Title Here",
  "mode": "First Submission Mode (or Resubmission Mode)",
  "files_audited": ["Text.docx", "Main-figure.pdf", "Supplementary-figure.pdf"],
  "summary_table": [
    {"area": "1. Bibliography & Citations", "status": "ISSUES FOUND", "summary": "Key takeaways..."}
  ],
  "sections": [
    {
      "id": "sec-1",
      "title": "1. Section Title",
      "intro": "Optional section intro.",
      "findings": [
        {
          "id": "F1.1",
          "severity": "CRITICAL",
          "category": "Category Label",
          "title": "Short finding title",
          "location": "Main Text [P38] & Figure 5C",
          "quote_para": "P38",
          "verbatim_quote": "",
          "issue": "Detailed explanation (supports **bold**, `code`, <em>, <sub>, <sup>).",
          "fix": "Exact drop-in replacement text or concrete action required.",
          "images": [
            {"path": "scratch_audit/crops/fig5c.jpg", "caption": "Figure 5C crop showing the issue."}
          ]
        }
      ]
    }
  ],
  "verified_items": [
    "All panel letters (A–E) match between figure PDFs and legends."
  ]
}
```

- **Tip for Smaller Models**: Set `"quote_para": "P38"` (for `Text.docx` `[P38]`) or `"quote_para": "Rebuttal:P7"` (for `Rebuttal_comments.docx` `[P7]`) and leave `"verbatim_quote": ""` empty—`build_html_report.py` automatically fetches the exact verbatim paragraph from the indexed text file so `verify_quotes.py` is guaranteed to pass!

#### 7B. Prepare Word Edit Specs (`text_edits.json`) and PDF Annotation Specs (`*_fig_anns.json`)

1. **Word Edits JSON (`scratch_audit/text_edits.json`)**:
   Always include `"replacement"` whenever a concrete wording/typo/citation fix exists so Word `<w:del>`/`<w:ins>` Track Changes are generated alongside the margin comment (omit `"replacement"` only for open-ended structural questions). `"para"` accepts an integer (`34`), `"P34"`, a soft-break sub-line (`"P4.13"`), or a table (`"T1"`):
   ```json
   [
     {
       "para": 34,
       "target": "between CMP-1006-treated and vehicle-treated cells",
       "replacement": "between CMP-1060-treated and vehicle-treated cells",
       "severity": "MINOR",
       "category": "Typo",
       "comment": "Compound code typo: change 'CMP-1006' to 'CMP-1060'."
     }
   ]
   ```
2. **PDF Annotations JSON (`scratch_audit/main_fig_anns.json`, `scratch_audit/supp_fig_anns.json`)**:
   Always include `"box": [x0, y0, x1, y1]` (or `"boxes": [[x0, y0, x1, y1], ...]` for multi-region findings on the same page) using pixel coordinates read from the `200 dpi` `--grid` images or `render_figs.py find-text` so red highlight rectangles are drawn directly around the affected panel(s):
   ```json
   [
     {
       "page": 5,
       "panel": "Figure 5C",
       "severity": "CRITICAL",
       "box": [540, 1285, 1130, 1745],
       "title": "ΔgeneA and ΔgeneB Data Bars Swapped",
       "issue": "Main Text [P38] and Fig. S5C report ΔgeneA = 38 and ΔgeneB = 7, but Fig. 5C plots 7 for ΔgeneA and 38 for ΔgeneB.",
       "fix": "Swap the plotted bar values for ΔgeneA and ΔgeneB in Figure 5C."
     }
   ]
   ```

#### 7C. Run the 2-Second Preflight Validator (`preflight_edits.py`) BEFORE Building Deliverables

Run [scripts/preflight_edits.py](./scripts/preflight_edits.py) with `--autofix-para` to verify every `.docx` target (and automatically relocate any off-by-one `para` index), verify every `.pdf` annotation page/box, and verify every `quote_para` and image path in `report_spec.json`:

```bash
python3 "$SKILL_DIR/scripts/preflight_edits.py" \
  --docx Text.docx:scratch_audit/text_edits.json \
  --pdf Main-figure.pdf:scratch_audit/main_fig_anns.json \
  --pdf Supplementary-figure.pdf:scratch_audit/supp_fig_anns.json \
  --report scratch_audit/report_spec.json \
  --manuscript-txt scratch_audit/manuscript_indexed.txt \
  --autofix-para
```

Fix any `[MISS]` or `[FAIL]` reported by `preflight_edits.py`, then compile all three deliverables:

```bash
# 1. Compile self-contained HTML report:
python3 "$SKILL_DIR/scripts/build_html_report.py" scratch_audit/report_spec.json MANUSCRIPT_AUDIT_REPORT.html \
  --manuscript-txt scratch_audit/manuscript_indexed.txt \
  --rebuttal-txt scratch_audit/rebuttal_indexed.txt

# 2. Generate annotated Word document(s) with native Track Changes & Margin Comments:
python3 "$SKILL_DIR/scripts/annotate_docx.py" Text.docx Text_AUDITED.docx scratch_audit/text_edits.json

# 3. Generate annotated Figure PDF(s) via byte-preserving ISO 32000-1 incremental update:
python3 "$SKILL_DIR/scripts/annotate_pdf.py" Main-figure.pdf Main-figure_AUDITED.pdf scratch_audit/main_fig_anns.json
python3 "$SKILL_DIR/scripts/annotate_pdf.py" Supplementary-figure.pdf Supplementary-figure_AUDITED.pdf scratch_audit/supp_fig_anns.json
```

---

### Step 8 — Mandatory Post-Generation On-Disk Verification (`verify_deliverables.py`)

Before reporting completion to the user, run [scripts/verify_deliverables.py](./scripts/verify_deliverables.py) to verify on disk that every deliverable exists, is non-empty, has valid internal XML/PDF/HTML structure, and passes both `validate_html.py` and `verify_quotes.py`:

```bash
python3 "$SKILL_DIR/scripts/verify_deliverables.py" \
  --html MANUSCRIPT_AUDIT_REPORT.html \
  --docx Text_AUDITED.docx \
  --pdf Main-figure_AUDITED.pdf Supplementary-figure_AUDITED.pdf \
  --source-text scratch_audit/manuscript_indexed.txt scratch_audit/rebuttal_indexed.txt
```

(Or pass `--dir .` to auto-discover `MANUSCRIPT_AUDIT_REPORT.html`, all `*_AUDITED.docx` files, all `*_AUDITED.pdf` files, and `scratch_audit/*_indexed.txt` in the workspace directory; pass `--no-counterpart Cover_letter.docx` if a clean secondary `.docx` file required no edits.)

If `verify_deliverables.py` reports any `FAIL`, fix the underlying file and re-run until `OVERALL STATUS: PASS` is printed. (Tip: prefix any calculated/derived check numbers in your prose explanations with `~` or `≈`, e.g. `~250 nm` or `≈600 nm`, so `verify_quotes.py` automatically recognizes them as derived values.)
