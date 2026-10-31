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
10. **Zero-Hallucination & Grounding Rule (Never Copy Example IDs)**: Never copy placeholder accession numbers, sample codes, or citations from this prompt or `checklist.md` into your audit report or `.docx` edits. Every identifier, accession number, and quote in your deliverables must exist in the manuscript's actual extracted files.
11. **Dominant Document Nomenclature Rule**: When fixing a 1x typo in an acronym, identifier, or construct/sample code, always replace it with the **dominant spelling already used in the manuscript** (`>= 3` occurrences, reported in `consistency_audit_out.txt`), never a newly invented variant.
12. **Mandatory Declaration & Reviewer Reply Preservation**: Never delete the body paragraph of mandatory declarations (`Data availability`, `Code availability`, `Author contributions`, `Competing interests`, `Funding`) when removing a duplicate section heading—delete or merge only the redundant heading (word-level typo fixes inside declaration paragraphs, such as fixing a misspelled repository name, are allowed). In a Rebuttal Letter, never modify the Reviewer's prompt paragraph with Track Changes (`"replacement"`), never replace a substantive author reply with `"No change"`, and never create a numbered manuscript defect card for a host/audit-tool limitation (`"No action for authors"`).
13. **Batch Image Cropping & Explicit Output Paths**: Prefer `render_figs.py batch-crop` (which supports JSON specs, single-page `PAGE X0,Y0,X1,Y1:OUT.jpg`, and multi-page inline `PAGE1@X0,Y0,X1,Y1:OUT1.jpg PAGE2@X0,Y0,X1,Y1:OUT2.jpg`) or pass `--out` / a trailing output `.jpg` path to `render_figs.py crop`. Use the `padded_crop_box` coordinates printed by `render_figs.py find-text` so panel headers are never clipped, and check for `[BLANK / NEAR-BLANK CROP WARNING]` in the output.
14. **Anti-Complacency Rule — Phase 1 Scripts Are a Floor, Not a Ceiling**:
    - Never assume a reference, figure legend, or table is error-free just because an automated counter says `0 warnings` or `ScaleBar: YES`.
    - For **citations**, always read `scratch_audit/citation_verification_digest.txt` (generated by `deep_ref_audit.py`) from top to bottom. Even when a reference's abstract shares general domain words with the manuscript (`[ABSTRACT LOADED]`), you must manually verify: (a) whether the specific protein, gene, isoform, or species named in the citing clause matches the cited paper, (b) whether the cited abstract's polarity (`does not promote`, `independent of`, `inhibits`) contradicts the citing sentence, and (c) whether citations in a two-clause sentence (`Whereas Clause A, Clause B [1,2]`) are attached to the wrong clause.
    - For **scale bars**, inspect the per-panel ratio printed by `callout_audit.py` (`ScaleBar: 1x/5p (CHECK!)`). A single `"Scale bar, 5 µm"` in Panel A does **not** mean Panels B–E have scale bars or that different-magnification insets are covered; and geometric plot parameters like `"10 µm radius"` are not micrograph scale bars.
15. **Visual Chart-Reading & Microscopy Guardrails (Prevent Visual False Positives)**:
    - **Ridgeline / Stacked Histograms vs. Line Graphs**: Vertically stacked horizontal distributions (e.g., flow-cytometry or single-cell intensity histograms stacked across timepoints `0.5 h, 1 h, 2 h, 3 h`) use the vertical axis to separate conditions and the horizontal axis (`x`) for intensity. Never misread the vertical stacking order of ridgeline histograms as a quantitative decrease in `y`.
    - **Replicate Dots on Error-Bar Caps**: At 150–200 DPI, an individual replicate dot sitting directly on an SD/SEM error-bar cap or bar border can blend into the line. Before flagging `"only 2 dots plotted instead of n=3"`, crop the graph at `400 dpi` (`render_figs.py crop ... --dpi 400`) and verify whether the 3rd dot rests on the error-bar cap or overlaps an adjacent point.
    - **Control / Structural Channel Inspection (`PHASE`, `Brightfield`, `Membranes`, `DAPI`)**: In multi-channel micrographs across treatment doses or conditions, inspect the structural/control channel (`Membranes`, `Phase`, `DAPI`) alongside the target channel (`GFP`, `Peptidoglycan`, etc.)—watch for global cell loss, lysis, or blank/failed imaging at the highest dose.
    - **Axis Tick Sequence Verification**: Read every numeric tick along each x- and y-axis in sequence (watching for non-monotonic or 10x-typo ticks like `0, 2000, 100, 50, 25, 12.5` in a 2-fold dilution series, or negative ticks `-1, -2` on a linear Fold Change axis) and check `07_pdf_page_text_*.log` for `[AXIS TICK SEQUENCE ANOMALY]`.
16. **Context Compaction Recovery & Incremental Spec Building**:
    - Save findings incrementally to `scratch_audit/report_spec.json` as you complete each audit phase rather than holding all findings in conversation memory until the end.
    - If your agent harness compacts the conversation history mid-audit, immediately run `ls -la scratch_audit/ scratch_audit/crops/` and read `scratch_audit/phase1_triage_summary.txt` and `scratch_audit/report_spec.json` to recover your exact progress without re-running completed steps.

---

## Step 0 — Host Software, Dependencies & Offline Workarounds

### 0.1 What Runs with Zero External Dependencies (Pure Python 3 Standard Library)
Almost every script in this skill uses **only the Python 3 standard library** (`python3 >= 3.8`, no `pip` packages or system binaries required):
- `run_phase1.py` (single-command parallel Phase 1 pre-audit orchestrator: auto-discovers all `.docx`, `.xlsx`, `.pdf`, and video/archive files in the workspace and runs the extraction, citation, callout, consistency, spreadsheet, PDF text-map, and video storyboard scripts in parallel)
- `docx2txt.py` (Word `.docx` indexed text extraction with provenance SHA-256 header, automatic Zotero/Mendeley/EndNote `<w:sdt>` content-control unwrapping in both paragraphs and tables, skipped `<w:del>` tracked deletions, Office MathML `<m:oMath>` + `<m:d>` delimiter equation linearization, `<w:br/>` soft-break sub-indexing `[P1.1]`, `[P1.2]`, `--extract-refs` bibliography extraction including `Supplementary References` `S1..SN`, `--extract-csl` Mendeley WebExtension & Zotero embedded CSL-JSON/abstract extractor, media extraction, and XML formatting/line-drift/citation-field diagnostics)
- `ref_audit.py`, `deep_ref_audit.py`, `callout_audit.py`, `consistency_audit.py` (automated text, numbered + `S1..SN` + Author–Year/Zotero citation checks, embedded CSL-JSON abstract + cached Crossref/NCBI verification, legend completeness matrix, `EV`/Expanded View + concatenated panel callouts, within-document nomenclature/hyphenation consistency, unit/dilution checks, Author Contributions vs. byline coverage, cover-letter, and rebuttal audits, with `--ignore` / `scratch_audit/audit_ignore.txt` support)
- `xlsx_audit.py` (pure-Python `.xlsx` workbook/sheet reader and Adobe XFA PDF Reporting Summary field extractor: template placeholder & formula-error scanner, column/row `n / mean / SD / SEM / median` calculator, and manuscript `mean ± SD/SEM` cross-checker — works without `openpyxl` or `pandas`)
- `preflight_edits.py` (2-second preflight validator for `.docx` edit targets, `.pdf` annotation boxes, and `report_spec.json` quotes/images, with `--autofix-para` relocation and guardrails against mandatory declaration deletions and `[REVIEWER]` replacement tags)
- `annotate_docx.py` (injects native Word `<w:del>`/`<w:ins>` Track Changes and Margin Comments into `.docx` copies, supporting `<w:sdt>` controls, `<m:oMath>` equations, `<w:br/>` soft breaks, inline citation fields, curly/straight quote normalization, and fuzzy near-miss hints)
- `annotate_pdf.py` (injects native PDF `/Subtype /Square` highlight boxes and `/Subtype /Text` sticky-note comments onto `.pdf` copies via ISO 32000-1 incremental update, supporting both classic PDFs and Adobe Illustrator `/Type /ObjStm` + `/Type /XRef` compressed-object-stream PDFs)
- `build_html_report.py`, `validate_html.py`, `verify_quotes.py`, `verify_deliverables.py` (compiles and validates the self-contained HTML report with automatic section finding counts, multi-file `quote_para` resolution, inline quote verification, and on-disk counterpart checks)
- `render_figs.py size`, `render_figs.py page-text`, `render_figs.py extract-embedded`, `render_figs.py batch-crop`, and `render_figs.py video` (pure-Python PDF page size parser including `/ObjStm`, PDF-vs-DOCX page text comparator with `@200dpi_px` bounding boxes and Poppler `pdftotext -bbox` fallback, embedded JPEG/PNG image extractor, batch panel cropper, and pure-Python MP4/MOV/AVI video metadata & storyboard generator)

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

### Fast-Path Single-Command Phase 1 Orchestrator (`run_phase1.py`) — RECOMMENDED START

To save turns and guarantee that no companion file (`.docx`, `.xlsx`, Reporting Summary `.pdf`, or zipped movie archive) is overlooked, run [scripts/run_phase1.py](./scripts/run_phase1.py) first:

```bash
python3 "$SKILL_DIR/scripts/run_phase1.py" . --out-dir scratch_audit
```

In a single parallel invocation (`~2–10 seconds`), `run_phase1.py` automatically discovers every file in the manuscript folder and executes Steps 1, 2, 3A (`page-text --compare-text`), and 3C (`video`), writing:
- `scratch_audit/manuscript_indexed.txt`, `rebuttal_indexed.txt`, `cover_indexed.txt`, and `supp_*_indexed.txt` (for any companion `.docx` files)
- `scratch_audit/extracted_refs.csl.json` (embedded Mendeley/Zotero CSL-JSON records including paper abstracts when present) and `scratch_audit/01_docx_formatting.log`
- `scratch_audit/02_ref_audit.log`, `03_deep_ref_audit.log`, `04_callout_audit.log`, `05_consistency_audit.log`, and `06_xlsx_and_xfa_audit.log`
- `scratch_audit/07_pdf_page_text_*.log` (per-page figure text maps + PDF-vs-DOCX identifier/spelling comparison with `@200dpi_px=[x0, y0, x1, y1]` bounding boxes)
- `scratch_audit/videos/*_storyboard.jpg` and `08_video_audit.log` (video container metadata, companion legend text, and storyboard contact sheets)
- `scratch_audit/phase1_triage_summary.txt` (consolidated summary of all Phase 1 outputs)

After `run_phase1.py` finishes, read `scratch_audit/phase1_triage_summary.txt` and the indexed text files, and proceed directly to rendering full-page figure grids in Step 3 (`render_figs.py render ... --grid`). The individual commands for Steps 1–3 are documented below for reference or granular re-runs.

---

### Step 1 — Extract Paragraph-Indexed Text, References, Embedded CSL-JSON, Media & Formatting Diagnostics

Run [scripts/docx2txt.py](./scripts/docx2txt.py) on every `.docx` file in the submission package (manuscript, rebuttal letter, cover letter, and any companion supplementary `.docx` files):

```bash
# 1. Extract paragraph-indexed text ([P1], [P2], ..., [TABLE 1] for tables), standalone reference list, and embedded Mendeley/Zotero CSL-JSON:
python3 "$SKILL_DIR/scripts/docx2txt.py" Text.docx --indexed --extract-csl scratch_audit/extracted_refs.csl.json > scratch_audit/manuscript_indexed.txt
python3 "$SKILL_DIR/scripts/docx2txt.py" Text.docx --extract-refs scratch_audit/refs.txt

# 2. Check Word continuous line-numbering, unsuppressed empty-paragraph line drift, font-size overrides, citation-manager fields, and embedded media:
python3 "$SKILL_DIR/scripts/docx2txt.py" Text.docx --audit-format > scratch_audit/format_audit.txt
python3 "$SKILL_DIR/scripts/docx2txt.py" Text.docx --media scratch_audit/ms_media

# 3. (If present) Extract Rebuttal Letter, Cover Letter, and any companion Supplementary .docx files:
python3 "$SKILL_DIR/scripts/docx2txt.py" Rebuttal_comments.docx --indexed > scratch_audit/rebuttal_indexed.txt
python3 "$SKILL_DIR/scripts/docx2txt.py" Rebuttal_comments.docx --media scratch_audit/rebuttal_media
python3 "$SKILL_DIR/scripts/docx2txt.py" Cover_letter.docx --indexed > scratch_audit/cover_indexed.txt
```

Read `scratch_audit/manuscript_indexed.txt`, `scratch_audit/format_audit.txt`, and (if present) `scratch_audit/rebuttal_indexed.txt`, `scratch_audit/cover_indexed.txt`, and `scratch_audit/supp_*_indexed.txt` in full.

---

### Step 2 — Run Automated Reference, Deep Bibliography, Callout, Consistency & Spreadsheet Pre-Checks

Run all static/bibliographic/spreadsheet analyzers (or review their outputs if already generated by `run_phase1.py`):

```bash
# 2A. Reference numbering (1..N and S1..SN) or Author-Year/Zotero inventory, citation style consistency,
#     duplicate-year citation artifacts ("Author et al, 2019, 2019"), duplicate bibliography detection, & sentence-to-title map:
python3 "$SKILL_DIR/scripts/ref_audit.py" scratch_audit/manuscript_indexed.txt --docx Text.docx > scratch_audit/ref_audit_out.txt

# 2B. Deep bibliography & citation audit: auto-loads embedded Mendeley/Zotero CSL-JSON (including paper abstracts!) +
#     cached Crossref & PubMed/EuropePMC abstract retrieval + Corpus-IDF topical/entity/polarity/review-type verification:
python3 "$SKILL_DIR/scripts/deep_ref_audit.py" scratch_audit/manuscript_indexed.txt --docx Text.docx > scratch_audit/deep_ref_audit_out.txt

# 2C. Figure, Table, Video, Panel callout order (supports S#, EV#, ED#, concatenated panels, and both (A) and (a) panel styles),
#     missing/phantom callouts, Legend Completeness Matrix, & Inter-Legend Copy-Paste / Table-Pairing Audit:
python3 "$SKILL_DIR/scripts/callout_audit.py" scratch_audit/manuscript_indexed.txt --supp-files . > scratch_audit/callout_audit_out.txt

# 2D. Consecutive duplicate words, editing-collision grammar scanner ("has have", a/an, double comparatives),
#     abbreviation lifecycle (used before definition, re-defined, single-use), SI/unit formatting hygiene,
#     within-document hyphenation/transposition/singleton typo candidates, prose-vs-table strain inventory,
#     Word table internal k (P%) arithmetic, Cross-Section (Abstract <-> Results <-> Discussion) claim/number/polarity comparator,
#     Author Contributions vs. byline coverage, Conditional Submission-Readiness triggers (animal IACUC, human IRB,
#     cell-line mycoplasma, omics/structure accession codes, software versions), plus Rebuttal & Cover Letter checks:
python3 "$SKILL_DIR/scripts/consistency_audit.py" scratch_audit/manuscript_indexed.txt \
  --rebuttal scratch_audit/rebuttal_indexed.txt \
  --cover-letter scratch_audit/cover_indexed.txt \
  --format-audit scratch_audit/format_audit.txt > scratch_audit/consistency_audit_out.txt

# 2E. (If .xlsx Source Data, Supplementary Tables, or XFA PDF Reporting Summaries are present) Pure-Python .xlsx & XFA field extractor:
#     dumps sheet dimensions/headers, flags unfilled template placeholders (<insert here>) & formula errors,
#     computes column/row n / mean / SD / SEM / median + paired differences, cross-checks manuscript mean ± SD/SEM claims
#     (including Mean-as-SD conflation), inspects .csv/.zip archives, and dumps filled (including /FlateDecode compressed) XFA PDF fields:
python3 "$SKILL_DIR/scripts/xlsx_audit.py" . --manuscript-txt scratch_audit/manuscript_indexed.txt --out-tsv scratch_audit/xlsx_tsv > scratch_audit/xlsx_audit_out.txt
```

**Model-Driven Analytical Review in Step 2**:
- **Evaluate Citation-to-Claim Alignment**: In `scratch_audit/citation_verification_digest.txt` and `scratch_audit/deep_ref_audit_out.txt` (Section 3 `REVIEW QUEUE`, which includes embedded CSL-JSON and PubMed/EuropePMC abstracts when available), read the citing sentence alongside the reference title and abstract. Evaluate whether the cited work genuinely supports the claim:
  - *Chronological plausibility & primary vs. review attribution*: Could a paper from that publication year establish the named method, tool, or discovery attributed to it, or is a secondary Review article cited for a primary experimental discovery/construct?
  - *Direction of effect / polarity*: Does the cited title or abstract support the claimed direction of effect, or does it report the opposite (`not`, `independent of`, `inhibits` vs. `activates`)?
  - *System & context match*: Does the cited study investigate the same experimental system, compartment, class of reagent/compound, or specific isoform/identifier claimed in the prose, or a different one?
  - *Clause-level placement*: In multi-clause sentences, is each citation placed after the specific clause it supports?
  - *Avoid false positives*: Methods, statistical software, and general background citations naturally share fewer surface keywords with domain prose—only flag a citation mismatch when the reference title/abstract genuinely contradicts or fails to support the specific claim attributed to it.
- **Evaluate Within-Document Nomenclature, Abbreviation Lifecycle, Grammar, Submission Readiness & Cross-Section Claims**: Review Sections 1–11 of `scratch_audit/consistency_audit_out.txt` (including Abbreviation Lifecycle anomalies, Track-Change merge collisions, SI unit hygiene, Word table `k (P%)` arithmetic, Conditional Submission-Readiness triggers, and Cross-Section `Abstract ↔ Results ↔ Discussion` quantitative/polarity contrasts) and verify cross-table consistency across all supplementary tables (whether `.docx`, `.xlsx`, or PDF-formatted `Supplementary_Information.pdf` extracted via `render_figs.py pdf-to-indexed-txt`).

---

### Step 3 — Map PDF Pages (with DOCX Cross-Check & Pixel Bounding Boxes), Render Figure PDFs with Pixel Grids & Storyboard Videos

- **Case 3A — Figures are provided as standalone PDF files (`Main-figure.pdf`, `Supplementary-figure.pdf`)**:
  First print the per-page figure label and text map with `page-text --compare-text scratch_audit/manuscript_indexed.txt` (this maps every PDF page to its `Figure X` label, prints exact `@200dpi` pixel dimensions per page, flags any `[RASTER-HEAVY PAGE WARNING]` where figure labels are rasterized/outlined, and prints `@200dpi_px=[x0, y0, x1, y1]` pixel bounding boxes for embedded PDF identifiers or words that differ from the `.docx` text), then render every page at `200 dpi` with `--grid` enabled:
  ```bash
  python3 "$SKILL_DIR/scripts/render_figs.py" page-text Main-figure.pdf --compare-text scratch_audit/manuscript_indexed.txt > scratch_audit/main_pdf_map.txt
  python3 "$SKILL_DIR/scripts/render_figs.py" page-text Supplementary-figure.pdf --compare-text scratch_audit/manuscript_indexed.txt > scratch_audit/supp_pdf_map.txt
  python3 "$SKILL_DIR/scripts/render_figs.py" render Main-figure.pdf scratch_audit/pages --dpi 200 --prefix main --grid
  python3 "$SKILL_DIR/scripts/render_figs.py" render Supplementary-figure.pdf scratch_audit/pages --dpi 200 --prefix supp --grid
  ```
- **Case 3B — Figures are embedded inside the `.docx` (extracted into `scratch_audit/ms_media/` in Step 1) or provided as `.png`/`.jpg`/`.tif` files**:
  First wrap the image(s) into a standard uncompressed `/Page` PDF at `200 dpi` (transparent RGBA/LA/palette backgrounds are automatically composited onto pure white so dark text/axes on transparent PNGs remain legible, and `1 image pixel == 1 px @ 200 dpi` in both `render_figs.py` and `annotate_pdf.py`), then render with `--grid`:
  ```bash
  python3 "$SKILL_DIR/scripts/render_figs.py" wrap-images scratch_audit/ms_media Figures.pdf --dpi 200
  python3 "$SKILL_DIR/scripts/render_figs.py" render Figures.pdf scratch_audit/pages --dpi 200 --prefix fig --grid
  ```
- **Case 3C — Supplementary Videos (`.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`, or `.zip` archives containing videos) are present**:
  Run `render_figs.py video --manuscript-txt scratch_audit/manuscript_indexed.txt` to auto-extract any zipped video archives and companion legend files, inspect container metadata (resolution, duration, frame count, FPS, codec, and audio tracks), and build a 6-frame timestamped storyboard contact sheet (`*_storyboard.jpg`):
  ```bash
  python3 "$SKILL_DIR/scripts/render_figs.py" video . scratch_audit/videos --frames 6 --manuscript-txt scratch_audit/manuscript_indexed.txt
  ```
  Compare the extracted container metadata (frame rate, total frame count, duration, resolution) and every `scratch_audit/videos/*_storyboard.jpg` image against the manuscript's video legends (checking whether the stated playback rate, frame counts, time stamps, scale bars, and visual events match the actual video files, and checking any `[ABRUPT VIDEO FRAME TRANSITION / SCENE CUT]` flags). If `[VIDEO AUDIT GAP — NO FFMPEG]` is printed for an H.264/HEVC video, note that host limitation in the Section intro or `verified_items` (never as a `"No action for authors"` defect card).
- **Case 3D — No Figure Artwork Files Are Provided in the Package**:
  If `run_phase1.py` emits `[PACKAGE COMPLETENESS GAP — CASE 3D: NO FIGURE ARTWORK FILES FOUND]` (i.e., the manuscript cites `Figure 1..N` in the text/legends, but no `.pdf`/`.png`/`.jpg` figure files or embedded `.docx` images were included in the folder), report the missing figure/table files as a `MAJOR` package-completeness finding in `MANUSCRIPT_AUDIT_REPORT.html`, audit the figure legends and callouts in the `.docx`, and pass `--no-figures-in-package` to `verify_deliverables.py` in Step 8.

This creates:
- Clean full-page images (`main_1.png`, `main_2.png`, ...) for visual inspection.
- Grid-overlay images (`main_1_grid.png`, `main_2_grid.png`, ...) with pixel ruler lines every `100 px` and red coordinate labels every `200 px`.

---

### Step 4 — Deep 12-Point Panel-by-Panel Visual, Schematic & Legend Audit

View **every single rendered figure page** (`main_1.png` .. `main_N.png`, `supp_1.png` .. `supp_N.png`) alongside its verbatim Figure Legend in `scratch_audit/manuscript_indexed.txt`. Check every panel `(A, B, C, ...)` against these **12 general points**:

1. **Title vs. Content & Prose-vs-Legend Panel Alignment**: Does the figure title accurately reflect the figure's content? Does the Results prose assign the same panel letters `(A, B, C, ...)` to each condition or sub-panel as the Figure Legend and the artwork (watching for swapped adjacent panel letters between body prose and legend)?
2. **All Visual Elements Explained**: Are all schematics/diagrams, dashed zoom boxes, magnified insets, outlines, arrows/arrowheads, and symbols on the figure explained in the legend? Conversely, if a multi-panel figure has sub-panels on the page with **no panel letters (`A, B, C`) drawn**, flag the missing panel letters.
3. **Arrows/Markers Point to What the Legend Claims**: Verify visually that arrows, arrowheads, and ROI boxes point to the exact features described in the legend.
4. **Scale Bars**:
   - Is a scale bar present in every image/panel where required, and stated in the legend with its physical value and unit?
   - If a panel has both an overview and a magnified inset, are **both** scale bar lengths stated, and is the inset scale bar geometrically consistent with the zoom ratio of the overview ROI box?
5. **Multi-Row / Multi-Column Layout Parity**: If a combined legend `(B,C)` describes a shared multi-row or multi-column structure (such as overview vs. zoom columns or condition headers), verify that both panels actually share that layout.
6. **Sample Size (`n`) & Plotted Data Points**: Count the individual replicate points plotted on bar/scatter graphs (at `400 dpi` when needed) and compare against `n = ...` in the legend, Results text, and `.xlsx` source data.
7. **Error Bars & Central Tendency**: Does every graph with error bars state whether they represent `SD`, `SEM`, `CI`, or `IQR`?
8. **Statistical Tests & Unused Symbols**: Are the statistical test name, sidedness, and multiple-comparison correction stated? Flag significance threshold symbols (`*`, `**`, `***`, `****`) defined in the legend if they do not appear on the graph (or vice versa).
9. **Axis Labels, Y-Axis Breaks, Colorbar Orientation & Panel Order**:
   - Read all text inside the figure (axes, titles, workflow headers, keys) for spelling/grammar errors, unit formatting issues, undisclosed axis breaks/truncations, and inverted or out-of-order colorbar/scale tick labels.
   - Check whether a legend describes left/right or top/bottom sub-panels in the same order they appear on the page.
10. **Cross-Legend Parameter Consistency**: Compare experimental parameters (time windows, concentrations, temperatures, thresholds, instrument settings) across main legends, supplementary legends, and Methods for the same experiment.
11. **Schematic / Model Logic & Quantitative Visual Consistency**:
   - In summary schematics, pathway diagrams, or architecture illustrations, verify that directional arrows (e.g., activation vs. inhibition, input vs. output) and structural/domain boundaries are logically consistent with the experimental findings and numerical coordinates reported in the text.
12. **Image, Gel & Blot Integrity**:
   - Inspect all gels, blots, plates, and image crops across Main and Supplementary figures for accidental panel/band duplication, mirroring, unlabeled molecular-weight/size markers, or undisclosed lane splicing.

Whenever you find a visual issue on page `P`, use the `@200dpi_px=[x0, y0, x1, y1]` coordinates printed in `scratch_audit/*_pdf_map.txt` or read the pixel coordinates from `main_P_grid.png` (`supp_P_grid.png`), and crop high-resolution (`400 dpi`) evidence images using `crop` or `batch-crop`:

```bash
# Option A: Crop a single region at 400 dpi using @200dpi pixel coordinates:
python3 "$SKILL_DIR/scripts/render_figs.py" crop Main-figure.pdf 5 540 1285 1130 1745 --dpi 400 --units px --ref-dpi 200 --out scratch_audit/crops/fig5c.jpg

# Option B: Batch-crop multiple regions in a single command (JSON list of {"pdf", "page", "box": [x0,y0,x1,y1], "out"}):
python3 "$SKILL_DIR/scripts/render_figs.py" batch-crop scratch_audit/crops_spec.json --dpi 400 --units px --ref-dpi 200
```

After creating all crops, build and view a contact sheet to verify no crop is clipped or mis-aimed:
```bash
python3 "$SKILL_DIR/scripts/render_figs.py" contact-sheet scratch_audit/crops --out scratch_audit/crops/contact_sheet.png
```

---

### Step 5 — Quantitative, Statistical, Physical & Source-Protocol Triangulation

1. **Text Numbers ↔ Main Figure Graph ↔ Supplementary Graph ↔ `.xlsx` Source Data**:
   - Whenever the Results text states numerical counts, percentages, fold changes, or `mean ± SD/SEM` for conditions or groups, cross-check those values against the plotted graph bars/points in **both** the Main Figure and Supplementary Figure (catching swapped bars/labels or mismatched values) and against the raw data tables in `scratch_audit/xlsx_audit_out.txt` / `scratch_audit/xlsx_tsv/`.
   - When inspecting `.xlsx` source data, pay attention to the table layout (e.g., whether columns represent independent groups, paired before/after columns, or replicate time series across rows/columns) and check whether the prose accidentally reports a second group's mean in place of a standard deviation or confuses a subset average with a cumulative total.
2. **Mathematical, Algorithmic & Equation Consistency**:
   - Check whether quantitative definitions, normalization procedures, and plot descriptions in the Results and Figure Legends are mathematically consistent with Materials & Methods.
   - Check every `<m:oMath>` equation in `manuscript_indexed.txt` to ensure all variable symbols and subscripts/superscripts match their prose definitions immediately below the equation and any worked numerical examples.
3. **Dilution, Molarity, Unit & Recipe Arithmetic**:
   - Review the dilution checks in `scratch_audit/consistency_audit_out.txt` (`V_added / V_total` vs. stated `%`) and verify stock-to-working concentrations, missing units on numerical quantities, and geometric area/volume unit conversions.
4. **Physical & Methodological Plausibility**:
   - Evaluate whether stated physical measurements, resolution claims, scales, and instrument parameters are physically plausible for the described apparatus and consistent across the manuscript (e.g., ensuring scaling factors or dimensional conversions are applied in the right direction and with the right dimensionality).
5. **Central Quantitative Claims, Bidirectional Methods Coverage & Reporting Summaries**:
   - Verify that headline quantitative claims in the Abstract/Results have (a) a measurement procedure in Materials & Methods, (b) explicit sample sizes (`n`), and (c) supporting data in a figure or table.
   - Review Section 6 of `scratch_audit/callout_audit_out.txt` for **both** Orphan Methods subsections (methods described for experiments not in the paper) and Figure/Results assays missing from Materials & Methods.
   - When an Adobe XFA Reporting Summary PDF is present (extracted at the bottom of `scratch_audit/xlsx_audit_out.txt`), compare its filled fields (such as data exclusion statements, statistical definitions, and reagent/antibody validation details) against the manuscript text, Methods, and source data.
6. **Published Source-Protocol Cross-Check**:
   - When the manuscript adapts a named published protocol or cites a reference for specific reagent ratios, equations, or benchmarks, cross-check the manuscript's stated values and terminology against the cited protocol (using web search if available in your harness).

---

### Step 6 — Rebuttal Letter & Cover Letter ↔ Manuscript Cross-Audit *(When Present)*

Compare every paragraph of `scratch_audit/rebuttal_indexed.txt` and `scratch_audit/cover_indexed.txt` against `scratch_audit/manuscript_indexed.txt` and the figures (using Sections 6 and 7 of `scratch_audit/consistency_audit_out.txt`):
1. **Unfulfilled Promises & Draft Leftovers**: Check every `"will change"`, `"can re-analyze"`, or `"would be happy to include"` flagged in `consistency_audit_out.txt`. Verify whether promised edits (e.g., title changes, added controls, text revisions) were actually applied to `Text.docx`.
2. **Main Text vs. Supplementary Location Contradictions**: If the rebuttal claims material was moved to or limited to the Supplementary Information, verify whether it is still in the Main Text or Main Figures.
3. **Wrong Figure / Video / Line-Number Citations & Leftover Acknowledgments**:
   - Review Section `6a` of `consistency_audit_out.txt`, which pairs every `lines X–Y` citation in the rebuttal with both the non-empty line number and the Word-with-unsuppressed-empty-spacers line number (`+N` drift offset from `format_audit.txt`).
   - Review Sections `6d` and `7a` of `consistency_audit_out.txt`, which list every `Figure X`, `Fig. SX`, and `Video SX` cited in the Rebuttal and Cover Letter alongside the manuscript's actual legend titles—verify that no cited figure/table/video number is out of date or points to the wrong panel, and check that `Acknowledgments` and the `Cover Letter` do not reference experiments or sections that were removed from the manuscript.
4. **Embedded Rebuttal Figures**: Inspect all images in `scratch_audit/rebuttal_media/` and check if they lack figure legends, condition/channel labels, or scale bar values.

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

- **Tip for Smaller Models**: Set `"quote_para": "P38"` (for `Text.docx` `[P38]`), `"quote_para": "Rebuttal:P7"` (for `Rebuttal_comments.docx` `[P7]`), `"quote_para": "Cover:P3"` (when `--cover-txt` is passed), or `"quote_para": "Supp:P12"` (when `--extra-txt Supp=scratch_audit/supp_indexed.txt` is passed) and leave `"verbatim_quote": ""` empty—`build_html_report.py` automatically fetches the exact verbatim paragraph from the indexed text file so `verify_quotes.py` is guaranteed to pass!

#### 7B. Prepare Word Edit Specs (`text_edits.json`) and PDF Annotation Specs (`*_fig_anns.json`)

1. **Word Edits JSON (`scratch_audit/text_edits.json`)**:
   Always include `"replacement"` whenever a concrete wording/typo/citation fix exists so Word `<w:del>`/`<w:ins>` Track Changes are generated alongside the margin comment (omit `"replacement"` only for open-ended structural questions). `"para"` accepts an integer (`34`), `"P34"`, a soft-break sub-line (`"P4.13"`), or a table (`"T1"`). When a target string appears multiple times in the same paragraph, specify `"occurrence": 2` (1-indexed) or `"all": true`:
   ```json
   [
     {
       "para": 34,
       "target": "between CMP-1006-treated and vehicle-treated samples",
       "replacement": "between CMP-1060-treated and vehicle-treated samples",
       "severity": "MINOR",
       "category": "Typo",
       "comment": "Identifier typo: change 'CMP-1006' to 'CMP-1060'."
     }
   ]
   ```
2. **PDF Annotations JSON (`scratch_audit/main_fig_anns.json`, `scratch_audit/supp_fig_anns.json`)**:
   Always include `"box": [x0, y0, x1, y1]` (or `"boxes": [[x0, y0, x1, y1], ...]` for multi-region findings on the same page, or `"anchor_text": "Label"` to auto-anchor on vector PDF text) using pixel coordinates read from the `200 dpi` `--grid` images, `scratch_audit/*_pdf_map.txt`, or `render_figs.py find-text` so red highlight rectangles are drawn directly around the affected panel(s). If a standalone figure PDF in a multi-file package has 0 visual defects, run `annotate_pdf.py Figure_EV3.pdf Figure_EV3_AUDITED.pdf --verified-stamp "Audited: no visual defects detected"` so every source figure PDF has a verified `*_AUDITED.pdf` counterpart:
   ```json
   [
     {
       "page": 5,
       "panel": "Figure 5C",
       "severity": "CRITICAL",
       "box": [540, 1285, 1130, 1745],
       "title": "Group A and Group B Data Bars Swapped",
       "issue": "Main Text [P38] and Fig. S5C report Group A = 38 and Group B = 7, but Fig. 5C plots 7 for Group A and 38 for Group B.",
       "fix": "Swap the plotted bar values for Group A and Group B in Figure 5C."
     }
   ]
   ```

#### 7C. Run the 2-Second Preflight Validator (`preflight_edits.py`) BEFORE Building Deliverables

Run [scripts/preflight_edits.py](./scripts/preflight_edits.py) with `--autofix-para` to verify every `.docx` target (and automatically relocate any off-by-one `para` index), verify every `.pdf` annotation page/box, check guardrails against mandatory declaration deletions and `[REVIEWER]` replacement tags, and verify every `quote_para` and image path in `report_spec.json`:

```bash
python3 "$SKILL_DIR/scripts/preflight_edits.py" \
  --docx Text.docx:scratch_audit/text_edits.json \
  --pdf Main-figure.pdf:scratch_audit/main_fig_anns.json \
  --pdf Supplementary-figure.pdf:scratch_audit/supp_fig_anns.json \
  --report scratch_audit/report_spec.json \
  --manuscript-txt scratch_audit/manuscript_indexed.txt \
  --rebuttal-txt scratch_audit/rebuttal_indexed.txt \
  --cover-txt scratch_audit/cover_indexed.txt \
  --autofix-para
```

Fix any `[MISS]` or `[FAIL]` reported by `preflight_edits.py`, then compile all three deliverables:

```bash
# 1. Compile self-contained HTML report (add --cover-txt or --extra-txt Prefix=path.txt if used):
python3 "$SKILL_DIR/scripts/build_html_report.py" scratch_audit/report_spec.json MANUSCRIPT_AUDIT_REPORT.html \
  --manuscript-txt scratch_audit/manuscript_indexed.txt \
  --rebuttal-txt scratch_audit/rebuttal_indexed.txt \
  --cover-txt scratch_audit/cover_indexed.txt

# 2. Generate annotated Word document(s) with native Track Changes & Margin Comments:
python3 "$SKILL_DIR/scripts/annotate_docx.py" Text.docx Text_AUDITED.docx scratch_audit/text_edits.json

# 3. Generate annotated Figure PDF(s) via byte-preserving ISO 32000-1 incremental update:
python3 "$SKILL_DIR/scripts/annotate_pdf.py" Main-figure.pdf Main-figure_AUDITED.pdf scratch_audit/main_fig_anns.json
python3 "$SKILL_DIR/scripts/annotate_pdf.py" Supplementary-figure.pdf Supplementary-figure_AUDITED.pdf scratch_audit/supp_fig_anns.json
```

---

### Step 8 — Mandatory Post-Generation On-Disk Verification (`verify_deliverables.py`)

Before reporting completion to the user, run [scripts/verify_deliverables.py](./scripts/verify_deliverables.py) to verify on disk that every deliverable exists, is non-empty, has valid internal XML/PDF/HTML structure, has a `*_AUDITED` counterpart for every original `.docx`/`.pdf` file, and passes both `validate_html.py` and `verify_quotes.py`:

```bash
python3 "$SKILL_DIR/scripts/verify_deliverables.py" \
  --html MANUSCRIPT_AUDIT_REPORT.html \
  --docx Text_AUDITED.docx \
  --pdf Main-figure_AUDITED.pdf Supplementary-figure_AUDITED.pdf \
  --source-text scratch_audit/manuscript_indexed.txt scratch_audit/rebuttal_indexed.txt
```

(Or pass `--dir .` to auto-discover the `.html` report, all `*_AUDITED.docx` files, all `*_AUDITED.pdf` files, `../Original` counterparts, and `scratch_audit/*_indexed.txt` in the workspace directory; pass `--no-counterpart Cover_letter.docx` if a clean secondary `.docx` file required no edits.)

If `verify_deliverables.py` reports any `FAIL`, fix the underlying file and re-run until `OVERALL STATUS: PASS` is printed. (Tip: prefix any calculated/derived check numbers in your prose explanations with `~` or `≈` so `verify_quotes.py` automatically recognizes them as derived values.)
