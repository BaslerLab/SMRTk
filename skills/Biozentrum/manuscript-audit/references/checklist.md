# Comprehensive Scientific Manuscript Audit Checklist

This reference provides a detailed, discipline-agnostic catalog of failure modes to check during **First Submission (Pre-Submission)** and **Post-Review Resubmission** manuscript audits.

---

## 1. Bibliography & In-Text Citations Integrity

### 1.1 Structural & Numbering Checks (Automated via `scripts/ref_audit.py` & `scripts/deep_ref_audit.py`)
- **Contiguous Numbering**: Reference list is numbered sequentially (`1..N`) with no skipped numbers or unnumbered entries.
- **Zero Uncited References**: Every reference `1..N` in the bibliography is cited at least once in the main text, tables, or figure legends.
- **Zero Dangling Callouts**: No in-text citation number exceeds `N` or points to a missing entry.
- **In-Text Citation Style Uniformity, Placement & Author–Year / Zotero Hygiene**:
  - Check whether in-text citations mix parenthetical `(n)` and bracketed `[n]` styles, or whether parenthetical `(n)` is used alongside a journal style that requires bracketed `[n]`, superscript, or author-year citations.
  - Check for displaced or orphan citation brackets at the start of a sentence (`"[13]Previously, ..."` or `". [10,12] Next sentence"`) where a reference belongs at the end of the preceding sentence, as well as unspaced citation brackets attached directly to words (`"word[41]"`).
  - In Author–Year manuscripts, check for duplicate-year citation artifacts such as `"Author et al, 2019, 2019"` (caused by two unmerged citation-manager entries from the same year that should be disambiguated as `2019a, 2019b` or merged into a single entry).
  - Verify bidirectional Author–Year coverage: every `(Author, Year)` cited in the text exists in the alphabetical bibliography, and every bibliography entry is cited at least once.
- **Zero Duplicate References**: No two entries in the reference list refer to the same publication (including preprints vs. published versions of the same study, or identical entries pasted under two numbers). When duplicates are found, `ref_audit.py` reports the exact paragraphs `[P#]` where each duplicate number is cited so callouts to the higher number can be re-pointed before shifting downstream references (`Refs k+1..N -> k..N-1`).
- **Bibliographic Formatting, Live Crossref Resolution & Metadata Hygiene**:
  - No multi-space or tab-delimited corruption, raw HTML entities (`&amp;`, `&lt;`), or non-breaking spaces (`U+00A0`) inside titles or author lists.
  - No broken/split words (e.g., single capital letter separated from its word stem: `"B acteria"`).
  - Every journal article entry includes authors, title, journal name, publication year, and volume/pages or article ID/DOI (watch for truncated page ranges like `"6-."`, missing page numbers after volume `"1995;16."`, or ALL-CAPS titles imported from legacy databases).
  - Watch for citation-manager export corruptions: intra-entry duplicate DOIs, a bare DOI string (`10.1016/...`) placed in the journal-name slot instead of the journal title, web-scraper metadata duplication (`"2017 18:1 18"`), book-chapter `'in <Journal>'` prefixes on regular journal articles, or database disambiguation suffixes like `'Science (1979)'`.
  - Preprint DOIs (`10.1101/...` bioRxiv/medRxiv, `10.21203/...` Research Square) must be explicitly labeled as preprints (not disguised under a journal name or date) and checked via `deep_ref_audit.py` for whether a peer-reviewed published Version of Record (`is-preprint-of`) now exists.
  - Check for Retraction notices, Expressions of Concern, or Corrections via `deep_ref_audit.py`.
  - Check for leftover editorial notes or placeholders inside references (e.g., `"(if this paper was accepted..."`, `"TODO"`, `"in preparation"`).
  - Check that software/package version numbers cited in references match the version numbers stated in the main text/Methods (`ref_audit.py`).
  - Species/taxonomic binomial names in reference titles follow standard binomial case (`Genus species` with lowercase specific epithet, e.g., `Escherichia coli` rather than title-cased `Escherichia Coli`).

### 1.2 Topical, Semantic, Polarity & Placement Verification (`deep_ref_audit.py` + Model Review)
- **Embedded CSL-JSON Abstract & Title Extraction**:
  - When a `.docx` file contains Mendeley WebExtension (`word/webextensions/webextension*.xml`) or Zotero (`w:instrText`) CSL-JSON metadata, `docx2txt.py --extract-csl` and `deep_ref_audit.py` automatically extract the embedded paper **titles**, **DOIs**, and **abstracts** alongside each citing sentence so you can directly evaluate semantic support.
- **Claim-to-Reference Alignment**: Read the citing sentence alongside the cited paper's title and abstract. Common failure modes to look for:
  - **Chronological Anachronism**: Citing a paper published before a named technology, method, or discovery existed.
  - **Abstract Polarity Reversal**: Text claims factor $X$ activates/promotes process $Y$, whereas the cited title or abstract explicitly states that $X$ does **not** promote $Y$ or acts in the opposite direction.
  - **System, Compartment, or Context Mismatch**: Text claims a phenomenon in system/compartment/condition $A$, whereas the cited paper studied system/compartment/condition $B$.
  - **Reagent / Compound Class Mismatch**: Text cites a paper on class $A$ to support a claim about a distinct class $B$.
  - **Identifier / Isoform / Variant Swap**: Text cites a paper on variant/component $A$ to support a claim about variant/component $B$, especially when the actual reference for $B$ is elsewhere in the bibliography.
  - **Citation Range Over-Inclusion**: Text cites a range `(a, b–d)` for a specific claim, where one of the middle papers (`c`) inside the range studies an unrelated topic.
  - **Category Overgeneralization**: Text groups multiple items together under a shared qualifier where the cited papers show that one of the items does not belong to that category.
- **Clause-Level Callout Placement**:
  - When a sentence has two distinct clauses ("Whereas $A$ occurs in condition $X$, $B$ remains uncharacterized in condition $Y$ (1,2)"), verify whether `(1,2)` support clause 1 ($A$ in $X$) or clause 2 ($B$ in $Y$). Place citations immediately after the specific clause they support.
  - When a two-sentence passage states an empirical finding in sentence 1 and a speculation in sentence 2, ensure the citation is attached to sentence 1 (the empirical report), not solely to the speculation in sentence 2.
- **Table & Reagent Source Attribution**:
  - In materials, sample, or reagent tables (`1..N` and `Supplementary References` `S1..SN`), verify that the cited "Source / Reference" actually describes the specific item or construct listed.

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

### 3.4 In-Image Text, Axes, Condition Labels, Schematics & Image Integrity (400 DPI Check)
- **Spelling & Grammar of Figure Text (`render_figs.py page-text --compare-text` + Visual Inspection)**: Read every header, axis label, and legend key inside the figure artwork at high DPI:
  - Catch spelling or grammatical errors in workflow headers, legends, and axis titles.
  - Check consistency of column headers and embedded PDF identifiers against the `.docx` text.
- **Axis Breaks & Colorbar Orientation**:
  - Check for undisclosed y-axis breaks/truncations and inverted or out-of-order heatmap/colorbar tick labels.
- **Missing Condition Titles & Prose-vs-Legend Panel Swaps**:
  - If one panel has a condition title above it, its paired control panel should have a corresponding title.
  - Check whether the Results prose swaps adjacent panel letters relative to the Figure Legend and artwork.
- **Condition / Group Disclosure on Axes & Legends**:
  - If a plot compares groups that differ in condition, sample type, or background, ensure both the legend and/or axis clearly disclose that difference.
- **Schematic Logic, Stoichiometry & Boundary Consistency**:
  - In schematic or model diagrams, trace directional arrows (activation vs. inhibition, input vs. output) against the experimental findings reported in the Results.
  - Verify that multi-subunit stoichiometries and structural/domain boundary coordinates in diagrams are arithmetically consistent and non-overlapping.
- **Image, Gel & Blot Crop Integrity**:
  - Inspect all gels, blots, plates, and image crops across Main and Supplementary figures for accidental duplication/reuse, horizontal mirroring, uncropped borders, unlabeled size/weight markers, or undisclosed lane splicing.

### 3.5 Supplementary Video & Movie Verification (`render_figs.py video`)
- **Container, Stream & Playback Metadata Hygiene**:
  - Verify that every cited video/movie file (including videos inside `.zip` archives) opens cleanly, has non-zero duration and standard codec/resolution, and does **not** contain an unintentional microphone/room audio track.
  - Compare the video container's actual playback frame rate (`fps`), total frame count, and duration against the descriptions stated in the Video Legend.
- **Storyboard vs. Video Legend Parity**: Inspect the timestamped storyboard contact sheet (`*_storyboard.jpg`) generated by `render_figs.py video` against the corresponding Video Legend to verify channel colors, scale bars, timestamp overlays, and that each video file matches its numbered legend (no swapped video files).

---

## 4. Quantitative, Statistical & Arithmetic Triangulation

### 4.1 Text Numbers ↔ Main Graphs ↔ Supplementary Graphs ↔ Source Data
- **Bar Height & Stacked-Bar Sum Verification**:
  - For every number stated in the Results text, read the corresponding bar/point value in the main figure AND in any companion supplementary figure.
  - **Adjacent Bar Swap Detection**: When the text states Category $A = 38$ and Category $B = 7$ (confirmed by the supplementary plot or source data), but the main figure chart plots Category $A = 7$ and Category $B = 38$, the bars or x-axis labels in the main figure are **swapped**!
- **Scatter / Dot Count vs. Stated `n`**:
  - Count the individual plotted points (dots, triangles, squares) on every bar/scatter plot and compare against the `n = X` stated in the figure legend and Results/Methods text.
  - Ensure the legend defines what each point represents (independent biological replicate vs. technical replicate / field of view / single observation) and what the error bars represent (`mean ± SD` vs. `mean ± SEM` vs. `median ± IQR`).
- **Statistical Test & Symbol Hygiene**:
  - Every p-value threshold symbol (`ns`, `*`, `**`, `***`, `****`) defined in a figure legend should correspond to symbols actually used in that panel (remove unused symbols copied from boilerplate legends).
  - Ensure the statistical test stated in the legend matches the experimental design and the Statistical Analysis section in Materials & Methods (flag hybrid parametric/non-parametric descriptions like `"Mann-Whitney or Student's t-test"` on `median ± IQR` data, `"75% IQR"` instead of `25th–75th percentile (50% IQR)` on boxplots, or parametric tests applied to `n = 2` replicates).
- **Mathematical / Algorithmic & Equation Parity**:
  - Verify that the prose description of quantitative plots, indices, or normalization formulas in the Results and Figure Legends is mathematically valid and consistent with Materials & Methods.
  - Verify that Office MathML (`<m:oMath>`) equations extracted by `docx2txt.py` use the exact same variable symbols and subscripts/superscripts as the surrounding prose definitions, worked numerical examples, and figure axis labels.
- **Excel Source Data, `.csv`/`.tsv`/`.zip` Archives, Supplementary Spreadsheets & XFA Reporting Checklists (`scripts/xlsx_audit.py`)**:
  - Compare the `n`, `mean`, `SD`, `SEM`, and `median` computed by `xlsx_audit.py`—as well as any paired adjacent-column differences (`Paired(B-A)`) or replicate time series—against the `mean ± SD/SEM` and sample-size `n` claims in the manuscript text and figure legends.
  - Check whether a `mean ± SD` statement in the prose accidentally reports a second group's mean in place of the first group's SD (`Mean-as-SD conflation`), or confuses a window average with a cumulative sum.
  - Check every `.xlsx`/`.csv` supplementary table, `.zip` archive (sequence counts in `.fasta`/`.aln` files and archive filename typos), and reporting checklist for unfilled template strings (`<insert here>`, `TODO`), non-numeric censored tokens embedded inside numeric series, Excel formula errors (`#REF!`, `#DIV/0!`, `#VALUE!`), and incomplete figure/panel rows.
  - When an Adobe XFA PDF Reporting Summary is included (extracted by `xlsx_audit.py`, including `/FlateDecode` compressed object streams), compare its filled fields (such as data exclusion statements, data availability statements, descriptive statistics definitions, and reagent validation details) against the manuscript text, Methods, and source data.

### 4.2 Experimental Recipes, Dilutions & Cross-Section Parameters
- **Dilution & Concentration Arithmetic** (`scripts/consistency_audit.py`):
  - Recompute stock-to-working dilutions (`V_stock / V_total = C_final / C_stock`) and check for 10-fold calculation errors or discrepancies between concentrations stated in Results vs. Methods/Legends.
- **Cross-Section Parameter Consistency**:
  - Compare experimental parameters—such as time windows, temperatures, concentrations, doses, wash counts/durations, and analysis thresholds—across Results, Materials & Methods, Figure Legends, Tables, and Video Legends.

### 4.3 Physical Plausibility, Headline Claims & Source-Protocol Cross-Check
- **Physical & Methodological Plausibility**:
  - Check that stated physical measurements, resolution claims, scaling factors, and unit conversions (e.g., 1D linear vs. 2D area vs. 3D volume scaling) are physically and mathematically sound.
- **Headline Quantitative Claim Completeness**:
  - Central quantitative claims in the Abstract/Results should have a measurement procedure in Materials & Methods, an explicit sample size (`n`), and a corresponding figure panel, table, or `mean ± SD/SEM`.
- **Published Source-Protocol Verification**:
  - When a manuscript adapts a published method and cites a paper for specific reagent concentrations, equations, or benchmarks, verify that the manuscript's numbers and terminology match the cited protocol.

---

## 5. Rebuttal Letter & Cover Letter ↔ Manuscript Cross-Audit

When auditing a submission or post-review resubmission that includes a point-by-point response letter (`Rebuttal_comments.docx`) and/or a cover letter (`Cover_letter.docx`):

1. **Location & Scope Claims**:
   - If the rebuttal claims *"We have moved/limited X to the Supplementary Information"* or *"We have removed X"*, verify whether $X$ is actually in the Supplementary Information or if it is still in the Main Text and Main Figures!
2. **Promised Edits & Future-Tense Leftovers**:
   - Scan the rebuttal for future-tense or conditional draft language (`"We will change the title..."`, `"We have this data and can re-analyze..."`, `"If the reviewers consider these informative, we would be happy to include them..."`).
   - Verify whether each promised change (e.g., title change, rewritten Introduction/Discussion, added control experiment) was actually executed in the manuscript file.
3. **Line-Number & Quote Verification (`consistency_audit.py --format-audit`)**:
   - Check every line-number citation (`lines L1–L2`) in the rebuttal against the manuscript.
   - Check if inserting new paragraphs during revision shifted downstream line numbers.
   - Check if Word continuous line numbering (`<w:lnNumType>`) counts unsuppressed blank spacer paragraphs or mixed font sizes, and report both non-empty text line numbers and Word line numbers with the exact `+N` empty-paragraph drift offset.
4. **Figure & Video Callout Accuracy in the Rebuttal**:
   - Verify that every `Figure X` or `Video SY` cited in the rebuttal actually shows what the rebuttal claims (watching for old figure/video numbers from the initial submission or shifted supplementary figure numbers).
5. **Definitions & Explanations Given Only in the Rebuttal**:
   - If the rebuttal defines a key quantitative threshold or experimental rationale in response to a reviewer, verify that this definition/rationale was also added to the manuscript text/Methods.
6. **Embedded Rebuttal Figures**:
   - Extract all images embedded in the rebuttal letter (`docx2txt.py --media`) and check that each has a caption/explanation, condition/channel labels, and scale bar dimensions.
7. **Addressee & Cross-Response Hygiene**:
   - Check for copy-paste errors such as writing *"We agree with the editor..."* inside a response to Reviewer #1, or referencing *"as shown in our response to Reviewer 2"* when the data is in Reviewer 1's response.
8. **Cover Letter ↔ Manuscript Consistency (`consistency_audit.py --cover-letter`)**:
   - Verify that the manuscript title, author list, and headline claims stated in the Cover Letter match the manuscript.
   - Verify that every `Figure X`, `Fig. SX`, `Table SX`, or `Video SX` cited in the Cover Letter exists in the manuscript and matches its legend, and check that `Acknowledgments` and the `Cover Letter` do not reference experiments or sections that were removed from the manuscript.

---

## 6. Prose, Nomenclature, Scientific Homophones, Unit Hygiene & Journal Declarations (`scripts/consistency_audit.py`)

1. **Consecutive Duplicate Words, Garbled Clauses & Copy-Paste Legend Duplication**:
   - Check for immediate word repetitions (`"the the"`), leftover draft clause duplications within the same sentence, and duplicated paragraph blocks inside figure legends.
2. **Nomenclature, Acronym & Cross-Table Consistency**:
   - Check for within-document nomenclature variations (e.g., prefix/suffix drift, hyphenated-vs-unhyphenated variants, and character transpositions in recurring identifiers or acronyms).
   - Verify that every strain, construct, or reagent code mentioned in the Main Text/Legends is present in the corresponding Strain/Reagent Table (and vice versa), that table-vs-prose decimal precision matches (`2.9` in prose vs. `2.85` in Table 1), and flag any caption-only tables whose body is missing from both the `.docx` and `.xlsx` files.
   - Check software/package version consistency across Main Text, Materials & Methods, and Figure Legends.
3. **Context-Sensitive Homophones & Malapropisms**:
   - Watch for valid-English words that pass standard spellcheckers but are wrong in technical context (e.g., `dies` vs. `dyes`, `silicon oil` vs. `silicone oil`, `unclear` vs. `nuclear`, `stain` vs. `strain`, `track` vs. `tract`, `compliment` vs. `complement`, `principle` vs. `principal`, `discreet` vs. `discrete`, `statically` vs. `statistically`, `rational` vs. `rationale`).
4. **Unit, Time, Symbol-Font, Greek Look-Alike & Spacing Uniformity**:
   - Harmonize unit capitalization and spacing (`mL` vs. `ml`, `µL` vs. `µl`/`uL`, `µm` vs. `um`, `s` vs. `sec`, `h` vs. `hr`/`hrs`, `17 h` vs. `17h`, comma decimal separators `4,3` vs. `4.3` in prose and tables).
   - Flag non-SI symbol corruptions (such as letter `'o'` or `'0'` for degree `°C`, non-standard Greek/math look-alike characters like curled beta `ϐ` `U+03D0` or hooked upsilon `ϒ` `U+03D2` misused for `β` and `γ`, or legacy Symbol-font ASCII substitutions where Greek letters or mathematical symbols were flattened into plain Latin characters or digits), missing units on numerical measurements, and unbalanced parentheses.
5. **Mandatory Journal Declaration Sections, Author Contributions & Repository Accessions**:
   - Verify presence of `Acknowledgments` / `Funding`, `Author Contributions`, `Competing Interests` / `Conflict of Interest`, and `Data / Code Availability`.
   - Verify that every author listed on the title page is included in `Author Contributions` (by full name or initials) and vice versa.
   - Verify that all public repository accession numbers include their official database prefix and resolve to the correct study via `deep_ref_audit.py`.
6. **Word Production & Citation-Manager Hygiene (`docx2txt.py --audit-format`)**:
   - Flag active citation-manager fields (`ADDIN ZOTERO`, `ADDIN EN.CITE`, `ADDIN MENDELEY`) or bibliographies wrapped inside `<w:sdt>` Structured Document Tags that should be unformatted to plain text before final submission if required by the journal's production system.
7. **Abbreviation Lifecycle Hygiene (`consistency_audit.py` Section 8)**:
   - Flag acronyms used in the text before their first parenthetical expansion (`used before definition`), acronyms expanded multiple times across different paragraphs (`redundant re-definition`), and acronyms defined once but never used again.
8. **Editing-Collision, Grammar & SI Notation Hygiene (`consistency_audit.py` Section 9)**:
   - Flag Track-Change merge collisions of adjacent auxiliary/reporting verbs (`has have`, `was were`, `is are`, `showed demonstrated`), indefinite article phonetic mismatches (`a osmotic`, `an uniform`), double comparatives (`more higher`), stripped power-of-10 superscripts (`106 CFU/mL` instead of `10^6 CFU/mL`), and within-document mixing of `uM`/`µM`, `mL`/`ml`, `h`/`hr`/`hrs`, or `37°C`/`37 °C`.
9. **Conditional Submission-Readiness & Minimal Reporting Triggers (`consistency_audit.py` Section 5b)**:
   - When vertebrate animals are used, verify an IACUC / institutional animal ethics approval statement is present; when human subjects or clinical samples are used, verify IRB / Informed Consent is stated; when mammalian cell lines are used, verify mycoplasma testing is stated; when high-throughput sequencing, proteomics, or structural determination is performed, verify public repository accession numbers (`GEO`, `SRA`, `PRIDE`, `PDB`, `EMDB`, `Zenodo`) are provided rather than `"upon request"`; and verify version numbers for named analysis software.
10. **Word Table (`[TABLE N]`) Internal Arithmetic & PDF Supplement Auditing (`consistency_audit.py` Section 10 & `render_figs.py pdf-to-indexed-txt`)**:
    - Verify that `k (P%)` subgroup cells inside Word tables match `k / N * 100` for the stated column/row total `N`, flag unfilled table placeholders (`TODO`, `TBD`), and extract/audit prose, legends, tables, and references from PDF-formatted Supplementary Information files (`Supplementary_Information.pdf`) alongside `.docx` files.
11. **Cross-Section Claim Alignment (`Abstract ↔ Results ↔ Discussion`) & PubMed/CSL Abstract Verification (`consistency_audit.py` Section 11 & `deep_ref_audit.py`)**:
    - Compare sentences in the Abstract and Discussion against matching sentences in Results to catch cross-section quantitative drift (different fold-changes, percentages, or `n`) or polarity/negation reversals; verify cited references against both embedded Mendeley/Zotero CSL-JSON abstracts and live PubMed/EuropePMC abstracts, including flagging primary/historical discoveries attributed solely to secondary Review articles and preprints that now have a published Version of Record.
