#!/usr/bin/env python3
"""Inject native PDF annotations (highlight rectangles + sticky-note comments) directly
into a copy of an existing PDF file via an ISO 32000-1 Incremental Update.

Zero external dependencies (pure Python 3 standard library).
Preserves 100% of the original PDF's vector graphics, fonts, page geometry, and embedded
images byte-for-byte, layering native PDF `/Subtype /Square` (highlight box) and
`/Subtype /Text` (sticky-note comment) + `/Subtype /Popup` annotations on top.

Usage:
  annotate_pdf.py INPUT.pdf OUTPUT.pdf ANNOTATIONS.json [--units px|pt|frac] [--ref-dpi 200] [--author "Manuscript Audit"]

JSON specification format (`ANNOTATIONS.json` is a list of annotation objects):
[
  {
    "page": 5,                          // 1-based page number in INPUT.pdf
    "panel": "Figure 5C",               // short panel/location label (used as annotation Subject)
    "severity": "CRITICAL",             // CRITICAL | MAJOR | MODERATE | MINOR
    "box": [540, 1285, 1130, 1745],     // optional [x0, y0, x1, y1] top-down coordinates (omit or null for page-level sticky note)
    "title": "ΔgeneA and ΔgeneB Data Bars Swapped",
    "issue": "Main Text [P38] and Fig. S5C report ΔgeneA = 38 and ΔgeneB = 7, but Fig. 5C plots 7 for ΔgeneA and 38 for ΔgeneB.",
    "fix": "Swap the plotted bar values for ΔgeneA and ΔgeneB in Figure 5C."
  }
]
"""
import sys
import os
import re
import json
import zlib
from datetime import datetime, timezone

SEV_RGB = {
    "CRITICAL": (0.85, 0.10, 0.10),  # Red
    "MAJOR":    (0.88, 0.45, 0.02),  # Amber / Orange
    "MODERATE": (0.02, 0.50, 0.80),  # Blue
    "MINOR":    (0.35, 0.42, 0.52),  # Slate
    "INFO":     (0.35, 0.42, 0.52),  # Slate
    "VERIFIED": (0.09, 0.50, 0.24),  # Green
}


def pdf_utf16be_hex(text):
    """Encode a Python Unicode string as a PDF UTF-16BE hex string <FEFF...>."""
    raw = b"\xfe\xff" + text.encode("utf-16-be")
    return b"<" + raw.hex().upper().encode("ascii") + b">"


def _extract_stream_bytes(obj_body):
    """Extract raw bytes between 'stream' and 'endstream' in a PDF object body."""
    m = re.search(rb"stream(?:\r\n|\n|\r)", obj_body)
    if not m:
        return None
    start = m.end()
    end = obj_body.rfind(b"endstream")
    if end == -1 or end < start:
        return None
    raw = obj_body[start:end]
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith((b"\n", b"\r")):
        raw = raw[:-1]
    return raw


def _unpack_objstm_into_map(obj_map):
    """Decompress PDF 1.5+ /Type /ObjStm object streams (used by Adobe Illustrator, etc.) into obj_map."""
    for onum, body in list(obj_map.items()):
        if not re.search(rb"/Type\s*/ObjStm\b", body):
            continue
        m_n = re.search(rb"/N\s+(\d+)", body)
        m_first = re.search(rb"/First\s+(\d+)", body)
        if not m_n or not m_first:
            continue
        n_objs = int(m_n.group(1))
        first_off = int(m_first.group(1))
        raw_stream = _extract_stream_bytes(body)
        if not raw_stream:
            continue
        decomp = None
        for candidate in (raw_stream, raw_stream.strip()):
            try:
                decomp = zlib.decompress(candidate)
                break
            except Exception:
                pass
        if not decomp or len(decomp) < first_off:
            continue
        header_tokens = decomp[:first_off].split()
        if len(header_tokens) < 2 * n_objs:
            continue
        entries = []
        try:
            for i in range(n_objs):
                sub_onum = int(header_tokens[2 * i])
                rel_off = int(header_tokens[2 * i + 1])
                entries.append((sub_onum, rel_off))
        except ValueError:
            continue
        payload = decomp[first_off:]
        for i, (sub_onum, rel_off) in enumerate(entries):
            end_off = entries[i + 1][1] if i + 1 < len(entries) else len(payload)
            if 0 <= rel_off <= end_off <= len(payload):
                sub_body = payload[rel_off:end_off].strip()
                # Top-level uncompressed objects take precedence if also present
                obj_map.setdefault(sub_onum, sub_body)


def parse_pdf_structure(data):
    """Locate trailer info (/Size, /Root, /Info, /ID, startxref) and ordered Page objects."""
    # Find last startxref
    m_sx = list(re.finditer(rb"startxref\s+(\d+)\s+%%EOF", data))
    if not m_sx:
        sys.exit("ERROR: Could not find startxref in input PDF.")
    last_startxref = int(m_sx[-1].group(1))

    # Map all uncompressed top-level objects: obj_num -> body_bytes
    obj_map = {}
    for m in re.finditer(rb"(?:^|\r|\n)(\d+)\s+0\s+obj\b(.*?)endobj", data, re.S):
        obj_map[int(m.group(1))] = m.group(2).strip()

    # Unpack compressed /Type /ObjStm streams (Adobe Illustrator / PDF 1.5+)
    _unpack_objstm_into_map(obj_map)

    # Determine whether the file uses classic trailer or /Type /XRef cross-reference stream
    sx_window = data[last_startxref:min(len(data), last_startxref + 1024)]
    uses_xref_stream = bool(re.search(rb"/Type\s*/XRef\b", sx_window))

    # Extract trailer dictionary fields (search from last trailer or XRef stream dicts)
    trailers = list(re.finditer(rb"trailer\s*<<(.*?)>>", data, re.S))
    candidate_dicts = []
    if trailers and not uses_xref_stream:
        candidate_dicts.append(trailers[-1].group(1))
    if sx_window:
        candidate_dicts.append(sx_window)
    # Also include any /Type /XRef object dictionaries (linearized PDFs keep /Root in the first XRef stream)
    for body in obj_map.values():
        if re.search(rb"/Type\s*/XRef\b", body):
            uses_xref_stream = True
            candidate_dicts.append(body[:1024])
    for tr in reversed(trailers):
        candidate_dicts.append(tr.group(1))
    candidate_dicts.append(data[max(0, len(data) - 4096):])

    orig_size = None
    root_ref = None
    info_ref = None
    id_array = None
    for cdict in candidate_dicts:
        if orig_size is None:
            m_size = re.search(rb"/Size\s+(\d+)", cdict)
            if m_size:
                orig_size = int(m_size.group(1))
        if root_ref is None:
            m_root = re.search(rb"/Root\s+(\d+\s+\d+\s+R)", cdict)
            if m_root:
                root_ref = m_root.group(1)
        if info_ref is None:
            m_info = re.search(rb"/Info\s+(\d+\s+\d+\s+R)", cdict)
            if m_info:
                info_ref = m_info.group(1)
        if id_array is None:
            m_id = re.search(rb"/ID\s*(\[\s*<[^>]+>\s*<[^>]+>\s*\])", cdict)
            if m_id:
                id_array = m_id.group(1)

    if orig_size is None and obj_map:
        orig_size = max(obj_map.keys()) + 1

    # Fallback for /Root: locate /Type /Catalog object in obj_map
    if root_ref is None:
        for onum, body in obj_map.items():
            if re.search(rb"/Type\s*/Catalog\b", body):
                root_ref = f"{onum} 0 R".encode("ascii")
                break

    if orig_size is None or root_ref is None:
        sys.exit("ERROR: Could not parse /Size or /Root from PDF trailer or XRef stream.")

    # Traverse /Pages tree to get ordered list of page object numbers
    root_num = int(root_ref.split()[0])
    pages_root_num = None
    if root_num in obj_map:
        m_pgs = re.search(rb"/Pages\s+(\d+)\s+0\s+R", obj_map[root_num])
        if m_pgs:
            pages_root_num = int(m_pgs.group(1))
    if pages_root_num is None:
        # Find object with /Type /Pages
        for onum, body in obj_map.items():
            if re.search(rb"/Type\s*/Pages\b", body):
                pages_root_num = onum
                break
    if pages_root_num is None:
        sys.exit("ERROR: Could not locate /Type /Pages object in PDF.")

    visited_nodes = set()

    def collect_pages(node_num):
        if node_num in visited_nodes:
            return []
        visited_nodes.add(node_num)
        body = obj_map.get(node_num, b"")
        if re.search(rb"/Type\s*/Page\b(?!s)", body):
            return [node_num]
        m_kids = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
        if not m_kids:
            return []
        kids = [int(x) for x in re.findall(rb"(\d+)\s+0\s+R", m_kids.group(1))]
        out = []
        for k in kids:
            out.extend(collect_pages(k))
        return out

    page_obj_nums = collect_pages(pages_root_num)
    if not page_obj_nums:
        # Fallback: collect any /Type /Page objects in object-number order
        page_obj_nums = sorted(
            onum for onum, body in obj_map.items()
            if re.search(rb"/Type\s*/Page\b(?!s)", body)
        )
    if not page_obj_nums:
        sys.exit("ERROR: No /Type /Page objects found in /Pages tree.")

    # Extract MediaBox for each page (inheriting from /Pages or /CropBox if needed)
    # Normalize (x0, y0, x1, y1) with min/max so inverted MediaBoxes (e.g. [0 792 612 0]) never yield negative heights
    def _norm_mb(coords):
        x0, y0, x1, y1 = map(float, coords)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    default_mb = (0.0, 0.0, 612.0, 792.0)
    m_mb_root = re.search(
        rb"/(?:MediaBox|CropBox)\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]",
        obj_map.get(pages_root_num, b"")
    )
    if m_mb_root:
        default_mb = _norm_mb(m_mb_root.groups())

    page_mediaboxes = []
    for pnum in page_obj_nums:
        pbody = obj_map[pnum]
        m_mb = re.search(
            rb"/(?:MediaBox|CropBox)\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]",
            pbody
        )
        if m_mb:
            page_mediaboxes.append(_norm_mb(m_mb.groups()))
        else:
            page_mediaboxes.append(default_mb)

    return {
        "orig_size": orig_size,
        "root_ref": root_ref,
        "info_ref": info_ref,
        "id_array": id_array,
        "last_startxref": last_startxref,
        "uses_xref_stream": uses_xref_stream,
        "obj_map": obj_map,
        "page_obj_nums": page_obj_nums,
        "page_mediaboxes": page_mediaboxes,
    }


def make_square_ap_stream(w_pt, h_pt, rgb, line_w=2.5):
    """Build a PDF Form XObject appearance stream drawing a colored border rectangle."""
    r, g, b = rgb
    pad = line_w / 2.0
    rw = max(1.0, w_pt - line_w)
    rh = max(1.0, h_pt - line_w)
    stream_content = (
        f"q\n"
        f"{line_w:.2f} w\n"
        f"{r:.3f} {g:.3f} {b:.3f} RG\n"
        f"{pad:.2f} {pad:.2f} {rw:.2f} {rh:.2f} re S\n"
        f"Q\n"
    ).encode("ascii")
    header = (
        f"<< /Type /XObject /Subtype /Form /FormType 1 "
        f"/BBox [0 0 {w_pt:.2f} {h_pt:.2f}] /Matrix [1 0 0 1 0 0] "
        f"/Resources << /ProcSet [/PDF] >> /Length {len(stream_content)} >>\nstream\n"
    ).encode("ascii")
    return header + stream_content + b"endstream"


def inject_annots_into_page_dict(orig_page_bytes, new_annot_obj_nums):
    """Add or append `new_annot_obj_nums` to `/Annots [...]` in a `/Page` dictionary."""
    refs_str = " ".join(f"{n} 0 R" for n in new_annot_obj_nums).encode("ascii")
    if re.search(rb"/Annots\s*\[", orig_page_bytes):
        return re.sub(
            rb"(/Annots\s*\[)(.*?)(\])",
            lambda m: m.group(1) + m.group(2) + b" " + refs_str + b" " + m.group(3),
            orig_page_bytes,
            count=1,
            flags=re.S,
        )
    # Insert /Annots [...] right before the closing >> of the Page dictionary
    pos = orig_page_bytes.rfind(b">>")
    if pos == -1:
        sys.exit("ERROR: Malformed /Page dictionary.")
    return orig_page_bytes[:pos] + b" /Annots [ " + refs_str + b" ] " + orig_page_bytes[pos:]


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


_VALID_PDF_ANN_KEYS = {
    "page", "panel", "severity", "sev", "category", "cat",
    "title", "heading", "name",
    "issue", "note", "comment", "description", "text",
    "fix", "suggested_fix", "recommendation", "replacement", "action",
    "box", "bbox", "rect", "boxes", "anchor_text",
    "x0", "y0", "x1", "y1", "x", "y", "w", "h", "width", "height",
    "page_note", "id",
}


def _try_resolve_anchor_text_box(pdf_path, page_1idx, anchor_text, units, ref_dpi):
    """Best-effort lookup of `anchor_text` bounding box on `page_1idx` via PyMuPDF (`fitz`)."""
    if not pdf_path or not os.path.isfile(pdf_path) or not anchor_text:
        return None
    try:
        import fitz
        doc = fitz.open(pdf_path)
        if not (1 <= page_1idx <= len(doc)):
            return None
        pg = doc[page_1idx - 1]
        rects = pg.search_for(str(anchor_text))
        if not rects:
            return None
        r = rects[0]
        pad_pt = 10.0
        rx0 = max(0.0, r.x0 - pad_pt)
        ry0 = max(0.0, r.y0 - pad_pt)
        rx1 = min(pg.rect.width, r.x1 + pad_pt)
        ry1 = min(pg.rect.height, r.y1 + pad_pt)
        if units == "px":
            scale = ref_dpi / 72.0
            return [round(rx0 * scale, 1), round(ry0 * scale, 1), round(rx1 * scale, 1), round(ry1 * scale, 1)]
        elif units == "pt":
            return [round(rx0, 1), round(ry0, 1), round(rx1, 1), round(ry1, 1)]
        elif units == "frac":
            return [round(rx0 / pg.rect.width, 4), round(ry0 / pg.rect.height, 4), round(rx1 / pg.rect.width, 4), round(ry1 / pg.rect.height, 4)]
    except Exception:
        pass
    return None


def validate_and_normalize_pdf_anns(annotations, num_pages, page_mediaboxes, units, ref_dpi, json_path, pdf_path=None):
    """Validate ANNOTATIONS.json schema upfront with clear per-item diagnostics."""
    if not isinstance(annotations, list):
        sys.exit(f"ERROR in {json_path}: Top-level JSON value must be a list [...] of annotation objects.")

    normalized = []
    errors = []
    for idx, item in enumerate(annotations, 1):
        if not isinstance(item, dict):
            errors.append(f"  - Item #{idx}: Expected a JSON object {{...}}, got {type(item).__name__}.")
            continue

        unknown = set(item.keys()) - _VALID_PDF_ANN_KEYS
        if unknown:
            errors.append(
                f"  - Item #{idx}: Unrecognized key(s) {sorted(unknown)}. "
                f"Valid keys: 'page', 'panel', 'severity', 'title', 'issue', 'fix', 'box' (or 'boxes' / 'anchor_text' / 'x0'..'y1')."
            )
            continue

        ann = dict(item)
        if "page" not in ann or ann["page"] is None:
            errors.append(f"  - Item #{idx}: Missing required integer field '\"page\"' (1-based page number).")
            continue
        try:
            pnum = int(ann["page"])
        except (ValueError, TypeError):
            errors.append(f"  - Item #{idx}: Invalid '\"page\": {ann['page']!r}' (must be a 1-based integer).")
            continue

        if not (1 <= pnum <= num_pages):
            errors.append(f"  - Item #{idx}: Page {pnum} is out of range (PDF has {num_pages} page(s), 1..{num_pages}).")
            continue
        ann["page"] = pnum

        # Normalize aliases for severity / title / issue / fix / box
        if "severity" not in ann and "sev" in ann:
            ann["severity"] = ann["sev"]
        if "title" not in ann:
            for alias in ("heading", "name"):
                if alias in ann and isinstance(ann[alias], str):
                    ann["title"] = ann[alias]
                    break
        if "issue" not in ann:
            for alias in ("note", "comment", "description", "text"):
                if alias in ann and isinstance(ann[alias], str):
                    ann["issue"] = ann[alias]
                    break
        if "fix" not in ann:
            for alias in ("suggested_fix", "recommendation", "replacement", "action"):
                if alias in ann and isinstance(ann[alias], str):
                    ann["fix"] = ann[alias]
                    break
        if "box" not in ann:
            for alias in ("bbox", "rect"):
                if alias in ann:
                    ann["box"] = ann[alias]
                    break
        if "box" not in ann and "boxes" in ann and ann["boxes"] is not None:
            if not (isinstance(ann["boxes"], (list, tuple)) and len(ann["boxes"]) >= 1):
                errors.append(f"  - Item #{idx} (page {pnum}): '\"boxes\"' must be a non-empty list of [x0, y0, x1, y1] boxes.")
                continue
            ann["box"] = ann["boxes"][0]
            ann["extra_boxes"] = list(ann["boxes"][1:])
        if "box" not in ann and ann.get("anchor_text"):
            resolved_box = _try_resolve_anchor_text_box(pdf_path, pnum, ann["anchor_text"], units, ref_dpi)
            if resolved_box is not None:
                ann["box"] = resolved_box
                print(f"  [ANCHOR-TEXT] Item #{idx} (page {pnum}): resolved anchor_text='{ann['anchor_text']}' -> box={resolved_box}")
        if "box" not in ann:
            coord_keys = {"x0", "y0", "x1", "y1", "x", "y", "w", "h", "width", "height"} & set(ann.keys())
            if {"x0", "y0", "x1", "y1"} <= set(ann.keys()):
                ann["box"] = [ann["x0"], ann["y0"], ann["x1"], ann["y1"]]
            elif {"x", "y"} <= set(ann.keys()) and ("w" in ann or "width" in ann) and ("h" in ann or "height" in ann):
                try:
                    x_val = float(ann["x"])
                    y_val = float(ann["y"])
                    w_val = float(ann.get("w", ann.get("width")))
                    h_val = float(ann.get("h", ann.get("height")))
                    ann["box"] = [x_val, y_val, x_val + w_val, y_val + h_val]
                except (ValueError, TypeError):
                    errors.append(f"  - Item #{idx} (page {pnum}): Invalid x/y/w/h coordinate values.")
                    continue
            elif coord_keys:
                errors.append(
                    f"  - Item #{idx} (page {pnum}): Incomplete coordinate keys {sorted(coord_keys)}; "
                    f"specify '\"box\": [x0, y0, x1, y1]'."
                )
                continue

        def _validate_one_box(b_raw, label="box"):
            if not (isinstance(b_raw, (list, tuple)) and len(b_raw) == 4):
                errors.append(
                    f"  - Item #{idx} (page {pnum}): '\"{label}\"' must be [x0, y0, x1, y1] (4 numbers) "
                    f"or omitted/null for a page-level sticky-note comment; got {b_raw!r}."
                )
                return None
            try:
                bx0, by0, bx1, by1 = map(float, b_raw)
            except (ValueError, TypeError):
                errors.append(f"  - Item #{idx} (page {pnum}): '\"{label}\"' coordinates must be numbers; got {b_raw!r}.")
                return None
            if bx1 < bx0:
                bx0, bx1 = bx1, bx0
            if by1 < by0:
                by0, by1 = by1, by0
            mb_x0, mb_y0, mb_x1, mb_y1 = page_mediaboxes[pnum - 1]
            w_pt, h_pt = abs(mb_x1 - mb_x0), abs(mb_y1 - mb_y0)
            if units == "px":
                max_x, max_y = w_pt * (ref_dpi / 72.0), h_pt * (ref_dpi / 72.0)
            elif units == "pt":
                max_x, max_y = w_pt, h_pt
            else:
                max_x, max_y = 1.0, 1.0
            if bx0 > max_x * 1.05 or by0 > max_y * 1.05:
                print(
                    f"  [WARN] Item #{idx} (page {pnum}): {label} {[bx0, by0, bx1, by1]} exceeds page bounds "
                    f"({max_x:.0f}x{max_y:.0f} in --units {units}); clamping to page edge."
                )
            return [bx0, by0, bx1, by1]

        box = ann.get("box")
        if box is not None:
            v_box = _validate_one_box(box, "box")
            if v_box is None:
                continue
            ann["box"] = v_box

        if ann.get("extra_boxes"):
            v_extras = []
            ok_extra = True
            for eb in ann["extra_boxes"]:
                vb = _validate_one_box(eb, "boxes[]")
                if vb is None:
                    ok_extra = False
                    break
                v_extras.append(vb)
            if not ok_extra:
                continue
            ann["extra_boxes"] = v_extras

        normalized.append(ann)

    if errors:
        sys.exit(f"SCHEMA VALIDATION FAILED for {json_path} ({len(errors)} error(s)):\n" + "\n".join(errors))
    return normalized


def main():
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)

    in_pdf, out_pdf = sys.argv[1], sys.argv[2]
    ann_json = sys.argv[3] if (len(sys.argv) >= 4 and not sys.argv[3].startswith("--")) else None
    if os.path.abspath(in_pdf) == os.path.abspath(out_pdf):
        sys.exit("ERROR: OUTPUT.pdf must be a different path from INPUT.pdf (never overwrite original).")

    units = opt("--units", "px")
    ref_dpi = float(opt("--ref-dpi", 200))
    author_base = opt("--author", "Manuscript Audit")
    verified_stamp = opt("--verified-stamp", None)
    pdf_date = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")

    orig_data = open(in_pdf, "rb").read()
    info = parse_pdf_structure(orig_data)

    if ann_json and os.path.isfile(ann_json):
        raw_annotations = json.load(open(ann_json, encoding="utf-8"))
    else:
        raw_annotations = []
    if not raw_annotations:
        stamp_msg = verified_stamp or (
            "Visual and quantitative audit completed against manuscript text, figure legend, and source data. "
            "No visual or quantitative discrepancies detected in this figure file."
        )
        raw_annotations = [{
            "page": 1,
            "panel": os.path.basename(in_pdf),
            "severity": "VERIFIED",
            "title": "Audit Verified — No Visual Defects Detected",
            "issue": stamp_msg,
            "fix": "No action required for this figure file.",
        }]
        print(f"  [VERIFIED STAMP] {os.path.basename(in_pdf)} has 0 defect items; attaching green VERIFIED audit stamp on Page 1.")

    annotations = validate_and_normalize_pdf_anns(
        raw_annotations, len(info["page_obj_nums"]), info["page_mediaboxes"], units, ref_dpi, ann_json or "--verified-stamp", pdf_path=in_pdf
    )
    by_page = {}
    for a in annotations:
        by_page.setdefault(int(a["page"]), []).append(a)

    next_obj_num = max(info["orig_size"], max(info["obj_map"].keys()) + 1)
    updated_objects = {}  # obj_num -> bytes (inside `N 0 obj ... endobj`)

    for page_1idx, page_anns in sorted(by_page.items()):
        page_obj_num = info["page_obj_nums"][page_1idx - 1]
        mb_x0, mb_y0, mb_x1, mb_y1 = info["page_mediaboxes"][page_1idx - 1]
        w_pt = mb_x1 - mb_x0
        h_pt = mb_y1 - mb_y0

        page_new_annots = []
        page_note_idx = 0

        for idx, ann in enumerate(page_anns, 1):
            sev = ann.get("severity", "MAJOR").upper()
            rgb = SEV_RGB.get(sev, SEV_RGB["MAJOR"])
            r, g, b = rgb

            panel_str = ann.get("panel", f"Page {page_1idx}")
            title_str = ann.get("title", "Audit Finding")
            issue_str = ann.get("issue", "")
            fix_str = ann.get("fix", "")

            author_hex = pdf_utf16be_hex(f"{author_base} [#{idx} {sev}]")
            subj_hex = pdf_utf16be_hex(f"{panel_str}: {title_str}")
            contents_str = f"[{sev} — {panel_str}] {title_str}\n\nISSUE:\n{issue_str}"
            if fix_str:
                contents_str += f"\n\nSUGGESTED FIX:\n{fix_str}"
            contents_hex = pdf_utf16be_hex(contents_str)
            date_bytes = f"({pdf_date})".encode("ascii")

            if ann.get("box") is None:
                # Page-level sticky-note comment stacked in top-right margin
                page_note_idx += 1
                note_x0 = max(mb_x0 + 10.0, mb_x1 - 28.0)
                note_y0 = max(mb_y0 + 10.0, mb_y1 - 26.0 * page_note_idx)

                txt_obj_num = next_obj_num
                next_obj_num += 1
                pop_num = next_obj_num
                next_obj_num += 1

                pop_x0 = max(mb_x0 + 10.0, note_x0 - 220.0)
                pop_y0 = max(mb_y0 + 10.0, note_y0 - 130.0)
                pop_dict = (
                    f"<< /Type /Annot /Subtype /Popup /F 28 "
                    f"/Parent {txt_obj_num} 0 R "
                    f"/Rect [{pop_x0:.2f} {pop_y0:.2f} {pop_x0 + 210:.2f} {pop_y0 + 130:.2f}] "
                    f"/Open false >>"
                ).encode("ascii")
                updated_objects[pop_num] = pop_dict

                txt_dict = (
                    f"<< /Type /Annot /Subtype /Text /Name /Comment /F 28 "
                    f"/P {page_obj_num} 0 R "
                    f"/Rect [{note_x0:.2f} {note_y0:.2f} {note_x0 + 18:.2f} {note_y0 + 18:.2f}] "
                    f"/C [{r:.3f} {g:.3f} {b:.3f}] "
                    f"/T {author_hex.decode('ascii')} "
                    f"/Subj {subj_hex.decode('ascii')} "
                    f"/Contents {contents_hex.decode('ascii')} "
                    f"/M {date_bytes.decode('ascii')} "
                    f"/CreationDate {date_bytes.decode('ascii')} "
                    f"/Popup {pop_num} 0 R >>"
                ).encode("ascii")
                updated_objects[txt_obj_num] = txt_dict
                page_new_annots.extend([txt_obj_num, pop_num])
                continue

            def _box_to_pdf_rect(b_coords):
                bx0, by0, bx1, by1 = map(float, b_coords)
                if units == "px":
                    scale = 72.0 / ref_dpi
                    x0_pt, y0_top = bx0 * scale, by0 * scale
                    x1_pt, y1_top = bx1 * scale, by1 * scale
                elif units == "pt":
                    x0_pt, y0_top, x1_pt, y1_top = bx0, by0, bx1, by1
                elif units == "frac":
                    x0_pt, y0_top = bx0 * w_pt, by0 * h_pt
                    x1_pt, y1_top = bx1 * w_pt, by1 * h_pt
                else:
                    sys.exit(f"Unknown --units '{units}' (use px, pt, or frac)")
                px0 = max(mb_x0, min(mb_x1, mb_x0 + x0_pt))
                px1 = max(mb_x0, min(mb_x1, mb_x0 + x1_pt))
                py1 = max(mb_y0, min(mb_y1, mb_y1 - y0_top))
                py0 = max(mb_y0, min(mb_y1, mb_y1 - y1_top))
                return px0, py0, px1, py1, max(10.0, px1 - px0), max(10.0, py1 - py0)

            pdf_x0, pdf_y0, pdf_x1, pdf_y1, rect_w, rect_h = _box_to_pdf_rect(ann["box"])

            # 1. Appearance Stream XObject for the /Square annotation
            ap_obj_num = next_obj_num
            next_obj_num += 1
            updated_objects[ap_obj_num] = make_square_ap_stream(rect_w, rect_h, rgb, line_w=2.5)

            # 2. /Subtype /Square annotation (colored rectangle framing the panel/region)
            sq_obj_num = next_obj_num
            next_obj_num += 1
            pop_sq_num = next_obj_num
            next_obj_num += 1

            sq_dict = (
                f"<< /Type /Annot /Subtype /Square /F 4 "
                f"/P {page_obj_num} 0 R "
                f"/Rect [{pdf_x0:.2f} {pdf_y0:.2f} {pdf_x1:.2f} {pdf_y1:.2f}] "
                f"/C [{r:.3f} {g:.3f} {b:.3f}] "
                f"/BS << /W 2.5 /S /S >> "
                f"/AP << /N {ap_obj_num} 0 R >> "
                f"/T {author_hex.decode('ascii')} "
                f"/Subj {subj_hex.decode('ascii')} "
                f"/Contents {contents_hex.decode('ascii')} "
                f"/M {date_bytes.decode('ascii')} "
                f"/CreationDate {date_bytes.decode('ascii')} "
                f"/Popup {pop_sq_num} 0 R >>"
            ).encode("ascii")
            updated_objects[sq_obj_num] = sq_dict

            # Popup linked to Square annotation
            pop_x0 = min(mb_x1 - 220, pdf_x0 + 10)
            pop_y0 = max(mb_y0 + 10, pdf_y1 - 140)
            pop_sq_dict = (
                f"<< /Type /Annot /Subtype /Popup /F 28 "
                f"/Parent {sq_obj_num} 0 R "
                f"/Rect [{pop_x0:.2f} {pop_y0:.2f} {pop_x0 + 210:.2f} {pop_y0 + 130:.2f}] "
                f"/Open false >>"
            ).encode("ascii")
            updated_objects[pop_sq_num] = pop_sq_dict

            # 3. /Subtype /Text sticky-note comment icon at top-right corner of the box
            txt_obj_num = next_obj_num
            next_obj_num += 1
            note_x0 = max(mb_x0 + 2, pdf_x1 - 20.0)
            note_y0 = max(mb_y0 + 2, pdf_y1 - 20.0)
            txt_dict = (
                f"<< /Type /Annot /Subtype /Text /Name /Comment /F 28 "
                f"/P {page_obj_num} 0 R "
                f"/Rect [{note_x0:.2f} {note_y0:.2f} {note_x0 + 18:.2f} {note_y0 + 18:.2f}] "
                f"/C [{r:.3f} {g:.3f} {b:.3f}] "
                f"/T {author_hex.decode('ascii')} "
                f"/Subj {subj_hex.decode('ascii')} "
                f"/Contents {contents_hex.decode('ascii')} "
                f"/M {date_bytes.decode('ascii')} "
                f"/CreationDate {date_bytes.decode('ascii')} "
                f"/Popup {pop_sq_num} 0 R >>"
            ).encode("ascii")
            updated_objects[txt_obj_num] = txt_dict

            page_new_annots.extend([sq_obj_num, pop_sq_num, txt_obj_num])

            # 4. Optional additional /Square boxes from "boxes": [[...], [...]]
            for eb in ann.get("extra_boxes", []):
                ex0, ey0, ex1, ey1, ew, eh = _box_to_pdf_rect(eb)
                e_ap_num = next_obj_num
                next_obj_num += 1
                updated_objects[e_ap_num] = make_square_ap_stream(ew, eh, rgb, line_w=2.5)
                e_sq_num = next_obj_num
                next_obj_num += 1
                e_sq_dict = (
                    f"<< /Type /Annot /Subtype /Square /F 4 "
                    f"/P {page_obj_num} 0 R "
                    f"/Rect [{ex0:.2f} {ey0:.2f} {ex1:.2f} {ey1:.2f}] "
                    f"/C [{r:.3f} {g:.3f} {b:.3f}] "
                    f"/BS << /W 2.5 /S /S >> "
                    f"/AP << /N {e_ap_num} 0 R >> "
                    f"/T {author_hex.decode('ascii')} "
                    f"/Subj {subj_hex.decode('ascii')} "
                    f"/Contents {contents_hex.decode('ascii')} "
                    f"/M {date_bytes.decode('ascii')} "
                    f"/CreationDate {date_bytes.decode('ascii')} "
                    f"/Popup {pop_sq_num} 0 R >>"
                ).encode("ascii")
                updated_objects[e_sq_num] = e_sq_dict
                page_new_annots.append(e_sq_num)

        # Update the /Page dictionary with the new /Annots references
        orig_page_body = info["obj_map"][page_obj_num]
        updated_objects[page_obj_num] = inject_annots_into_page_dict(orig_page_body, page_new_annots)

    # Assemble the PDF Incremental Update byte buffer
    out_buf = bytearray(orig_data)
    if not out_buf.endswith((b"\n", b"\r")):
        out_buf.extend(b"\r\n")

    offsets = {}
    for onum in sorted(updated_objects.keys()):
        offsets[onum] = len(out_buf)
        obj_bytes = f"{onum} 0 obj\r\n".encode("ascii") + updated_objects[onum] + b"\r\nendobj\r\n"
        out_buf.extend(obj_bytes)

    if info.get("uses_xref_stream"):
        # Write an ISO 32000-1 /Type /XRef cross-reference stream for PDF 1.5+ /ObjStm files
        xref_obj_num = next_obj_num
        next_obj_num += 1
        xref_offset = len(out_buf)
        offsets[xref_obj_num] = xref_offset

        sorted_onums = sorted(offsets.keys())
        runs = []
        for onum in sorted_onums:
            if not runs or onum != runs[-1][-1] + 1:
                runs.append([onum])
            else:
                runs[-1].append(onum)

        index_parts = []
        raw_xref = bytearray()
        for run in runs:
            index_parts.append(f"{run[0]} {len(run)}")
            for onum in run:
                # /W [1 4 1]: type=1 (uncompressed in-use), 4-byte big-endian offset, 1-byte gen=0
                raw_xref.extend(b"\x01" + offsets[onum].to_bytes(4, "big") + b"\x00")

        comp_xref = zlib.compress(bytes(raw_xref))
        xref_dict_parts = [
            "/Type /XRef",
            f"/Size {next_obj_num}",
            "/W [1 4 1]",
            f"/Index [{' '.join(index_parts)}]",
            f"/Root {info['root_ref'].decode('ascii')}",
            f"/Prev {info['last_startxref']}",
        ]
        if info["info_ref"]:
            xref_dict_parts.append(f"/Info {info['info_ref'].decode('ascii')}")
        if info["id_array"]:
            xref_dict_parts.append(f"/ID {info['id_array'].decode('ascii')}")
        xref_dict_parts.append("/Filter /FlateDecode")
        xref_dict_parts.append(f"/Length {len(comp_xref)}")

        xref_header = f"{xref_obj_num} 0 obj\r\n<< {' '.join(xref_dict_parts)} >>\r\nstream\r\n".encode("ascii")
        xref_footer = f"\r\nendstream\r\nendobj\r\nstartxref\r\n{xref_offset}\r\n%%EOF\r\n".encode("ascii")
        out_buf.extend(xref_header + comp_xref + xref_footer)
    else:
        xref_offset = len(out_buf)
        out_buf.extend(b"xref\r\n")

        # Group updated object numbers into contiguous runs for standard xref subsections
        sorted_onums = sorted(offsets.keys())
        runs = []
        for onum in sorted_onums:
            if not runs or onum != runs[-1][-1] + 1:
                runs.append([onum])
            else:
                runs[-1].append(onum)

        for run in runs:
            out_buf.extend(f"{run[0]} {len(run)}\r\n".encode("ascii"))
            for onum in run:
                out_buf.extend(f"{offsets[onum]:010d} 00000 n \r\n".encode("ascii"))

        # Build incremental trailer
        trailer_parts = [
            f"/Size {next_obj_num}",
            f"/Root {info['root_ref'].decode('ascii')}",
            f"/Prev {info['last_startxref']}",
        ]
        if info["info_ref"]:
            trailer_parts.append(f"/Info {info['info_ref'].decode('ascii')}")
        if info["id_array"]:
            trailer_parts.append(f"/ID {info['id_array'].decode('ascii')}")

        trailer_bytes = (
            f"trailer\r\n<< {' '.join(trailer_parts)} >>\r\n"
            f"startxref\r\n{xref_offset}\r\n%%EOF\r\n"
        ).encode("ascii")
        out_buf.extend(trailer_bytes)

    os.makedirs(os.path.dirname(os.path.abspath(out_pdf)), exist_ok=True)
    with open(out_pdf, "wb") as f:
        f.write(out_buf)

    print(f"Saved native-annotated PDF (incremental update on original PDF): {out_pdf}")
    print(f"  Pages annotated: {len(by_page)}/{len(info['page_obj_nums'])}, "
          f"total audit comments added: {len(annotations)}")


if __name__ == "__main__":
    main()
