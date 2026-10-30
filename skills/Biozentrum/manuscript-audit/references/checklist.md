# Comprehensive Scientific Manuscript Audit Checklist

This reference provides a detailed, discipline-agnostic catalog of failure modes to check during **First Submission (Pre-Submission)** and **Post-Review Resubmission** manuscript audits.

---

## 1. Bibliography & In-Text Citations Integrity

### 1.1 Structural & Numbering Checks (Automated via `scripts/ref_audit.py` & `scripts/deep_ref_audit.py`)
- **Contiguous Numbering**: Reference list is numbered sequentially (`1..N`) with no skipped numbers or unnumbered entries.
- **Zero Uncited References**: Every reference `1..N` in the bibliography is cited at least once in the main text, tables, or figure legends.
- **Zero Dangling Callouts**: No in-text citation number exceeds `N` or points to a missing entry.
- **In-Text Citation Style Uniformity & Author–Year / Zotero Hygiene**:
  - Check whether in-text citations mix parenthetical `(n)` and bracketed `[n]` styles, or whether parenthetical `(n)` is used alongside a journal style that requires bracketed `[n]`, superscript, or author-year citations.
  - In Author–Year manuscripts, check for duplicate-year citation artifacts such as `"Author et al, 2019, 2019"` (caused by two unmerged citation-manager entries from the same year that should be disambiguated as `2019a, 2019b` or merged into a single entry).
  - Verify bidirectional Author–Year coverage: every `(Author, Year)` cited in the text exists in the alphabetical bibliography, and every bibliography entry is cited at least once.
- **Zero Duplicate References**: No two entries in the reference list refer to the same publication (including preprints vs. published versions of the same study, or identical entries pasted under two numbers). When duplicates are found, `ref_audit.py` reports the exact paragraphs `[P#]` where each duplicate number is cited so callouts to the higher number can be re-pointed before shifting downstream references (`Refs k+1..N -> k..N-1`).
- **Bibliographic Formatting, Live Crossref Resolution & Metadata Hygiene**:
  - No multi-space or tab-delimited corruption inside titles or author lists.
  - No broken/split words (e.g., single capital letter separated from its word stem: `"B acteria"`).
  - Every journal article entry includes authors, title, journal name, publication year, and volume/pages or article ID/DOI (watch for truncated page ranges like `"6-."` missing the ending page number).
  - Preprint DOIs (`10.1101/...` bioRxiv/medRxiv, `10.21203/...` Research Square) must be explicitly labeled as preprints (not disguised under a journal name or date) and checked via `deep_ref_audit.py` for whether a peer-reviewed published Version of Record (`is-preprint-of`) now exists.
  - Check for Retraction notices, Expressions of Concern, or Corrections via `deep_ref_audit.py`.
  - Check for leftover editorial notes or placeholders inside references (e.g., `"(if this paper was accepted..."`, `"TODO"`, `"in preparation"`).
  - Check that software/package version numbers cited in references match the version numbers stated in the main text/Methods (`ref_audit.py`).
  - Species/gene names in reference titles are spelled accurately (watch for OCR corruptions in older papers).

### 1.2 Topical & Placement Verification (Manual + Search Verification)
- **Claim-to-Reference Alignment**: Does the cited paper actually demonstrate the specific claim in the sentence? Common failure modes:
  - **Model / Organism Swap**: Text claims a phenomenon in organism/tissue $X$, but the cited reference studied organism/tissue $Y$.
  - **Pathway / Structure Swap**: Text cites a paper on structure/isoform $A$ (e.g., extracellular structure, acute phase) to support a claim about structure/isoform $B$ (e.g., intracellular isoform, chronic/latent phase), especially when the actual reference for $B$ is elsewhere in the bibliography.
  - **Mutant / Genotype Swap**: Text cites a range `(a, b–d)` for a specific mutant/knockout phenotype, where one of the middle papers (`c`) inside the range studies an unrelated gene or species.
  - **Category Overgeneralization**: Text groups two taxa/factors together under a shared qualifier (e.g., *"Intranuclear bacteria such as X and Y..."*), where the cited papers show that $Y$ does not belong to that category.
- **Clause-Level Callout Placement**:
  - When a sentence has two distinct clauses ("Whereas $A$ occurs in condition $X$, $B$ remains uncharacterized in condition $Y$ (1,2)"), verify whether `(1,2)` support clause 1 ($A$ in $X$) or clause 2 ($B$ in $Y$). Place citations immediately after the specific clause they support.
  - When a two-sentence passage states an empirical finding in sentence 1 and a speculation in sentence 2, ensure the citation is attached to sentence 1 (the empirical report), not solely to the speculation in sentence 2.
- **Table & Reagent Source Attribution**:
  - In strain/plasmid/cell-line/antibody tables, verify that the cited "Source / Reference" actually created the exact construct listed (e.g., watch for a 1980s/1990s mutant paper cited as the source of a strain carrying a modern fluorescent reporter plasmid from a different study).

---

## 2. In-Text Callouts to Figures, Panels, Tables, Videos & Methods

### 2.1 Completeness & Bidirectional Parity (Automated via `scripts/callout_audit.py`)
- **Every Figure Cited**: All main figures (`Figure 1..N`) and supplementary/extended-data figures (`Figure S1..SN`) are cited in the main text.
- **Every Panel Cited**: Every sub-panel `(A), (B), (C)...` defined in a figure or supplementary figure is specifically cited in the text (or intentionally covered by a range `Figure 1A–C`).
- **Every Sub-Graph within a Multi-Graph Panel Cited**: When a single panel letter (e.g., `Figure S4B` or `Figure S5C`) contains **two side-by-side graphs** (`left` and `right`), verify that **both** the left and right graphs are cited and explained in the text.
- **Every Table & Supplementary Table Cited**: All tables (`Table 1..N`, `Table S1..SN`) are cited in the text.
- **Every Video / Movie / Supplementary Data File Cited**: Every supplementary video/movie (`Video S1..SN`) or dataset (`Data S1..SN`) listed in the legends is cited at the appropriate point in the Results/Discussion.
- **Zero Phantom Callouts**: The text never cites a figure, panel letter, table, or video that does not exist in the figure PDFs or legends.

### 2.2 Callout Ordering & Precision
- **Sequential First-Callout Order**:
  - Main figures (`Figure 1, 2, 3...`) and supplementary figures (`Figure S1, S2, S3...`) should be introduced in numerical order.
  - Within each figure, panels (`A, B, C, D, E...`) should be cited in alphabetical order on first appearance (flag cases like `Figure 4D` cited before `Figure 4C`, or `Figure S5B,C` cited before `Figure S5A`).
- **Specific Panel vs. Bare Figure Callouts**:
  - When a figure has multiple disparate panels (`A–E`), citing `(Figure 3)` for a specific observation shown only in `Figure 3B` is imprecise; specify `(Figure 3B)`.
- **Symmetric Parallel Callouts**:
  - When a sentence compares two parallel experiments shown across two figures (e.g., *"No activity was observed in mutant X and mutant Y (Figure 5A and 6C)"*), check whether `5A` is an image (with `5B` being the quantification) while `6C` is the quantification (with `6B` being the image). Recommend symmetric callouts `(Figure 5A,B and 6B,C)`.

### 2.3 Materials & Methods ↔ Results/Figures Parity
- **Orphan Methods Subsections**: Every protocol/subsection in Materials & Methods (e.g., Immunofluorescence, Western blotting, qPCR, Flow cytometry, specific drug treatments, or staining reagents like Phalloidin) must correspond to data actually presented or mentioned in the Results, Figures, Supplementary Figures, or Videos. Flag any leftover Methods section whose assay/reagent never appears in the rest of the paper.
- **Missing Methods Coverage**: Conversely, every experimental technique, inhibitor, cell line, or image-analysis pipeline shown in the Figures/Results must have a corresponding description in Materials & Methods.

---

## 3. Visual Figure & Panel-by-Panel Legend Audit

Always inspect the full figure page at 150–200 DPI for layout, then crop and inspect individual panels/labels at **300–400 DPI** (`scripts/render_figs.py`).

### 3.1 Figure Title & Scope Accuracy
- **Title vs. Content Match**: Does the bold title of the figure legend accurately summarize what is shown in that figure? (Watch for figure titles that mention two compartments/conditions—e.g., *"in the nucleus or cytoplasm"*—after one of the conditions was moved to a Supplementary Figure).

### 3.2 Panel Letter & Sub-Element Completeness
- **1-to-1 Panel Letter Match**: Every `(A), (B), (C)...` in the figure image exists in the legend, and vice versa. Single-panel figures without `(A)` in the artwork must not have `(A)` in the legend.
- **Unexplained Sub-Elements**: Check every panel for:
  - Top/side **cartoon schematics** (`Early -> Mid -> Late`) that are drawn inside a panel but omitted from the legend.
  - **Multi-row or multi-column layouts** (e.g., top row = low-magnification overview, bottom row = magnified inset; or top row = merged channels, bottom row = single grayscale channel; or a 2×2 grid of sub-panels) where the legend fails to explain what each row/column represents.
  - **Side-by-side replicate images** under a single panel letter (e.g., two separate Z-stacks or `Example 1` and `Example 2`) not explained in the legend.
  - **Phantom Sub-Panels in Combined Legends**: When a legend combines two panels `(B,C)` and states *"Scale bars represent 50 µm in the overview image and 10 µm in the magnified image"*, check whether **both** `B` and `C` actually have the overview column (`C` may only have the magnified columns!).
  - **Ordered Descriptions of Multi-Graph Panels**: If a panel contains two graphs (`left` and `right`), the legend should describe them in left-to-right order (not right-to-left).

### 3.3 Channels, Labels, Annotations & Scale Bars
- **Fluorescence / Color Channel Completeness**: Every color channel visible in a merged image or shown as a separate grayscale column (e.g., cyan, magenta, red, green, grey/white) must be identified with its marker/fluorophore and color in the legend.
- **Annotation Symbol & Color-Key Fidelity**:
  - Check every **arrow**, **arrowhead** (triangle), **asterisk (`*`)**, **dashed/dotted outline**, **solid/dashed box**, **crosshair**, and **axis arrow (`X, Y, Z`)** drawn on the image.
  - Verify that the symbol is mentioned in the legend with the **exact terminology** (`arrowhead` vs. `arrow`) and that it points to what the legend claims (e.g., watch for arrowheads pointing to cytosolic bacterial clusters while the legend says *"arrows highlight host nuclei"*!).
  - Verify color-key mappings (`yellow, black, and white boxes represent X, Y, and Z`) pixel-by-pixel against the column headers and image.
  - Flag low-contrast annotations (e.g., a black dashed box drawn on a dark fluorescence background).
- **Scale Bar Presence & Proportionality**:
  - Every micrograph (and every independent example/inset, such as `Example 2` in a two-example figure or the right image in a two-image panel) should have a visible scale bar or an explicit shared-scale statement.
  - **Reverse Error**: Every scale bar drawn on an image (e.g., in `Figure S3 RAW image`) must have its physical length (`µm`, `nm`, `mm`) stated in the legend!
  - **Multiple Scale Bars in One Panel**: If different rows/insets of the same panel have different magnifications and different scale bar lengths (e.g., `WT` row vs. `ΔmutA` row), the legend must state both scale bar values rather than a single value.
  - **Geometric Sanity Check**: If an overview image has a `100 µm` scale bar and a dashed box that is ~`80 µm` wide, the scale bar inside the magnified view of that box (~25% of the box width) cannot be `50 µm`—it is `20 µm`! Always check that overview vs. inset scale bar numbers are geometrically consistent.

### 3.4 In-Image Text, Axes & Condition Labels (400 DPI Check)
- **Spelling & Grammar of Figure Text**: Read every header, axis label, and legend key inside the figure artwork at high DPI:
  - Catch spelling errors in workflow headers (e.g., `"Pre-proccessed"`).
  - Catch singular/plural grammatical errors on axes (e.g., `"per 1000 cell"` instead of `"cells"`).
  - Check consistency of column headers across panels (`"Merge"` vs. `"Merged"`).
- **Missing Condition Titles**: If one panel has a condition title `"Inhibitor X"` above it, its paired control panel should have `"Vehicle / DMSO"` above it.
- **Strain / Genotype Disclosure on Axes & Legends**: If a plot compares two compartments or conditions where one group uses `WT` and the other uses a mutant (`ΔmutA`), both the legend and/or axis must disclose the difference in genotype.

### 3.5 Supplementary Video & Movie Verification (Automated via `render_figs.py video`)
- **Container & Stream Hygiene**: Verify that every cited `Video S1..SN` / `Movie S1..SN` file opens cleanly, has non-zero duration and standard codec/resolution, and does **not** contain an unintentional microphone/room audio track.
- **Storyboard vs. Video Legend Parity**: Inspect the timestamped storyboard contact sheet (`*_storyboard.jpg`) generated by `render_figs.py video` against the corresponding Video Legend to verify channel colors, scale bars, timestamp overlays (`hpi` / `min`), and that each video file matches its numbered legend (no swapped video files).

---

## 4. Quantitative, Statistical & Arithmetic Triangulation

### 4.1 Text Numbers ↔ Main Graphs ↔ Supplementary Graphs
- **Bar Height & Stacked-Bar Sum Verification**:
  - For every number stated in the Results text, read the corresponding bar/point value in the main figure AND in any companion supplementary figure.
  - **Adjacent Bar Swap Detection**: When the text states Category $A = 38$ and Category $B = 7$ (confirmed by the supplementary replicate plot), but the main figure stacked bar chart plots Category $A = 7$ and Category $B = 38$, the bars or x-axis labels in the main figure are **swapped**!
- **Scatter / Dot Count vs. Stated `n`**:
  - Count the individual plotted points (dots, triangles, squares) on every bar/scatter plot and compare against the `n = X` stated in the figure legend and Results/Methods text.
  - Flag discrepancies (e.g., legend states `n = 3`, but the control bar plots `4` dots; or text states `3 biological replicates`, while `WT` plots `6` dots and mutants plot `3–4` dots).
  - Ensure the legend defines what each dot represents (biological replicate vs. technical replicate / field of view / single cell / single pixel) and what the error bars represent (`mean ± SD` vs. `mean ± SEM` vs. `median ± IQR`).
- **Statistical Test & Symbol Hygiene**:
  - Every p-value threshold symbol (`ns`, `*`, `**`, `***`, `****`) defined in a figure legend should correspond to symbols actually used in that panel (remove unused symbols copied from boilerplate legends).
  - Ensure the statistical test stated in the legend (`two-tailed Student's t-test`, `one-way ANOVA`) matches the experimental design and the Statistical Analysis section in Materials & Methods.
- **Mathematical / Algorithmic & OMML Equation Parity**:
  - Verify that the prose description of quantitative plots (such as 2D pixel intensity scatter plots, colocalization indices, or normalization formulas) in the Results and Figure Legends is mathematically valid and consistent with the Materials & Methods section.
  - Verify that Office MathML (`<m:oMath>`) equations extracted by `docx2txt.py` use the exact same symbol definitions (`R(t)`, `A_0`, subscripts/superscripts) as the surrounding prose and figure axis labels.
- **Excel Source Data, Supplementary Spreadsheets & Reporting Checklists (Automated via `scripts/xlsx_audit.py`)**:
  - Recompute `n`, `mean`, `SD`, `SEM`, and `median` directly from `.xlsx` source-data sheets (`xlsx_audit.py --manuscript-txt`) and verify that every `mean ± SD/SEM` and sample-size `n` claim in the manuscript text and figure legends matches the underlying spreadsheet values (catching stale rounding or subset/row-count discrepancies).
  - Check every `.xlsx` supplementary table and reporting checklist for unfilled template strings (`<insert here>`, `Example for demonstration`, `TODO`), Excel formula errors (`#REF!`, `#DIV/0!`, `#VALUE!`), and incomplete figure/panel rows.

### 4.2 Experimental Recipes, Dilutions & Time Windows
- **Dilution & Concentration Arithmetic** (Automated via `scripts/consistency_audit.py`):
  - Recompute every stock-to-working dilution (`V_stock / V_total = C_final / C_stock`).
  - Flag 10-fold calculation errors (e.g., `1.5 µL` of DMSO in `300 µL` medium is `0.5% v/v`, not `0.05% v/v`!) and discrepancies between the concentration stated in Results (`0.5% DMSO`) vs. Methods/Legends (`0.05% DMSO`).
- **Cross-Section Parameter Consistency**:
  - Compare **timepoints / imaging windows** (`14–17 hpi` vs. `15–17 hpi` vs. `14–16 hpi`), **MOIs** (`MOI of 10` vs. `MOI of 500`), **temperatures**, **bath/wash counts and durations**, and **antibiotic concentrations** across Results, Materials & Methods, Figure Legends, and Video Legends.

### 4.3 Physical Plausibility, Headline Claims & Source-Protocol Cross-Check
- **Physical & Optical Limits**:
  - Check optical resolution claims against the diffraction limit (`~200–250 nm` laterally for standard confocal microscopy) and verify whether a stated dimension refers to **pre-expansion (effective biological) scale** vs. **post-expansion (physical hydrogel) scale**.
  - Check geometric area/volume conversions (e.g., `3 × 3 mm = 9 mm² = 0.09 cm²`) and chemical solubility/molarity limits.
- **Headline Quantitative Claim Completeness**:
  - Central quantitative claims in the Abstract/Results (e.g., expansion factors, fold-changes, inhibition percentages) must not appear only as bare numbers—verify that they have a measurement procedure in Materials & Methods, sample size (`n`), and a quantification plot or `mean ± SD/SEM`.
- **Published Source-Protocol Verification**:
  - When a manuscript adapts a published method and cites a paper for specific reagent concentrations, chemical anchoring/labeling steps, or resolution benchmarks, verify that the manuscript's numbers and terminology match the cited protocol (and any published errata/corrections).

---

## 5. Rebuttal Letter & Cover Letter ↔ Manuscript Cross-Audit

When auditing a submission or post-review resubmission that includes a point-by-point response letter (`Rebuttal_comments.docx`) and/or a cover letter (`Cover_letter.docx`):

1. **Location & Scope Claims**:
   - If the rebuttal claims *"We have moved/limited X to the Supplementary Information"* or *"We have removed X"*, verify whether $X$ is actually in the Supplementary Information or if it is still in the Main Text and Main Figures!
2. **Promised Edits & Future-Tense Leftovers**:
   - Scan the rebuttal for future-tense or conditional draft language (`"We will change the title..."`, `"We have this data and can re-analyze..."`, `"If the reviewers consider these informative, we would be happy to include them..."`).
   - Verify whether each promised change (e.g., title change, rewritten Introduction/Discussion, added control experiment) was actually executed in the manuscript file.
3. **Line-Number & Quote Verification (Automated via `consistency_audit.py --format-audit`)**:
   - Check every line-number citation (`lines L1–L2`) in the rebuttal against the manuscript.
   - Check if inserting new paragraphs during revision shifted downstream line numbers (so that `lines 277–281` in the rebuttal now point to the wrong paragraph!).
   - Check if Word continuous line numbering (`<w:lnNumType>`) counts unsuppressed blank spacer paragraphs or mixed font sizes (`12 pt` vs. `10/11 pt`), and report both non-empty text line numbers and Word line numbers with the exact `+N` empty-paragraph drift offset.
4. **Figure & Video Callout Accuracy in the Rebuttal**:
   - Verify that every `Figure X` or `Video SY` cited in the rebuttal actually shows what the rebuttal claims (watch for old figure/video numbers from the initial submission or shifted supplementary figure numbers like citing `Fig. S5` for an experiment that is now `Fig. S6`).
5. **Definitions & Explanations Given Only in the Rebuttal**:
   - If the rebuttal defines a key quantitative threshold or experimental rationale in response to a reviewer (e.g., *"Here we define multinucleated cells as cells having >3 nuclei"*, or why a higher `MOI` or dose was used for a mutant), verify that this definition/rationale was also added to the manuscript text/Methods!
6. **Embedded Rebuttal Figures**:
   - Extract all images embedded in the rebuttal letter (`docx2txt.py --media`) and check that each has a caption/explanation, channel labels, and scale bar dimensions.
7. **Addressee & Cross-Response Hygiene**:
   - Check for copy-paste errors such as writing *"We agree with the editor..."* inside a response to Reviewer #1, or referencing *"as shown in our response to Reviewer 2"* when the data is in Reviewer 1's response.
8. **Cover Letter ↔ Manuscript Consistency (Automated via `consistency_audit.py --cover-letter`)**:
   - Verify that the manuscript title, author list, and headline quantitative/mechanistic claims stated in the Cover Letter match the manuscript.
   - Verify that every `Figure X`, `Fig. SX`, `Table SX`, or `Video SX` cited in the Cover Letter actually exists in the manuscript and matches its legend (e.g., catch cases where the Cover Letter cites `"Figure 7"` for a model schematic after the schematic was moved to `"Figure S9"` and the main text only has `Figures 1–6`).

---

## 6. Prose, Scientific Homophones, Unit Hygiene & Journal Declarations (Automated via `scripts/consistency_audit.py`)

1. **Consecutive Duplicate Words & Garbled Duplicated Clauses**:
   - Check for immediate word repetitions (`"images images"`, `"the the"`) and leftover draft clause duplications within the same sentence (e.g., *"the inner layer is surrounded by a thin layer surrounded by an outer membrane"*).
2. **Context-Sensitive Scientific Homophones**:
   - Watch for valid-English words that pass standard spellcheckers but are wrong in scientific context:
     - `commercialized / fluorescent / membrane dies` -> `dyes`
     - `unclear invasion / envelope / pore / localization` -> `nuclear ...`
     - `mutant / bacterial / wild-type stain(s)` -> `strain(s)`
     - `respiratory / gastrointestinal / urinary track` -> `tract`
     - `compliment fixation / cascade / complemented` -> `complement` / `complemented`
     - `principle component / investigator` -> `principal`
     - `discreet bands / puncta / foci` -> `discrete`
3. **Unit, Time & Spacing Uniformity**:
   - Harmonize `mL` vs. `ml`, `µL` vs. `µl`/`uL`, `µm` vs. `um`, `s` vs. `sec`, `h` vs. `hr`/`hrs`, and number-unit spacing (`17 h` vs. `17h`, `1.5 min` vs. `1min30sec`).
4. **Mandatory Journal Declaration Sections & Repository Accessions**:
   - Verify presence of `Acknowledgments` / `Funding`, `Author Contributions`, `Competing Interests` / `Conflict of Interest`, and `Data / Code Availability`.
   - Verify that all public repository accession numbers include their official database prefix (e.g., `GSE100001` rather than bare `"GEO accession 100001"`, `PRJNA...`, `PXD...`, `EMD-...`) and resolve to the correct study via `deep_ref_audit.py`.
5. **Word Production & Citation-Manager Hygiene (`docx2txt.py --audit-format`)**:
   - Flag active citation-manager fields (`ADDIN ZOTERO`, `ADDIN EN.CITE`, `ADDIN MENDELEY`) or bibliographies wrapped inside `<w:sdt>` Structured Document Tags that should be unformatted to plain text before final submission if required by the journal's production system.
