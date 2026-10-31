#!/usr/bin/env python3
"""Render, crop, grid-overlay, extract embedded images, and assemble contact sheets from PDF figures.

Designed to work across any agent harness (OpenCode, Claude Code, Cursor, Aider, etc.)
and operating system (Linux, macOS, Windows/WSL) with automatic backend fallback:
  1. PyMuPDF (`pymupdf` / `fitz` Python package)
  2. pypdfium2 (`pypdfium2` Python package)
  3. Poppler (`pdftoppm` or `pdftocairo` CLI) + Pillow (`PIL`)
  4. Ghostscript (`gs` CLI) + Pillow (`PIL`)
  5. ImageMagick (`magick` or `convert` CLI) + Pillow (`PIL`)
  6. Pure-Python Standard-Library Fallback (`extract-embedded` subcommand — extracts
     raw JPEG and PNG `/Subtype /Image` streams directly from the PDF with zero
     external packages or binaries!).

Usage:
  render_figs.py check-env
  render_figs.py size             FILE.pdf|IMG [PAGE]
  render_figs.py page-text        FILE.pdf [--compare-text manuscript_indexed.txt] [--extra-txt supp.txt]
  render_figs.py find-text        FILE.pdf QUERY [--units px|pt|frac] [--ref-dpi 200]
  render_figs.py render           FILE.pdf|IMG OUTDIR [--dpi 200] [--pages 1-3,5] [--prefix page] [--grid]
  render_figs.py crop             FILE.pdf|IMG PAGE X0 Y0 X1 Y1 [OUT.jpg] [--dpi 400] [--units pt|px|frac] [--ref-dpi 200] [--out crop.jpg]
  render_figs.py batch-crop       SPEC.json | FILE.pdf|IMG PAGE X0,Y0,X1,Y1:OUT1.jpg [X0,Y0,X1,Y1:OUT2.jpg ...] [--dpi 400] [--units px|pt|frac]
  render_figs.py video            VIDEO_OR_DIR OUTDIR [--frames 6] [--compare-text manuscript_indexed.txt]
  render_figs.py wrap-images      IMG_OR_DIR [IMG2 ...] OUT.pdf [--dpi 200]
  render_figs.py extract-embedded FILE.pdf OUTDIR
  render_figs.py contact-sheet    CROPS_DIR [--out contact_sheet.png] [--cols 3]
  render_figs.py compare          IMG1 IMG2 OUT.jpg [--label1 "Left"] [--label2 "Right"] [--box1 x0,y0,x1,y1] [--box2 x0,y0,x1,y1]
"""
import sys
import os
import re
import json
import glob
import zlib
import struct
import shutil
import tempfile
import subprocess


# ---------------------------------------------------------------------------
# Environment & Cross-Platform Tool Discovery
# ---------------------------------------------------------------------------

def _candidate_python_interpreters():
    """Search common virtualenv, Conda, Homebrew, pipx, and cache paths for Python 3."""
    patterns = [
        os.path.abspath(".venv/bin/python3"),
        os.path.abspath("venv/bin/python3"),
        os.path.expanduser("~/.venv/bin/python3"),
        os.path.expanduser("~/venv/bin/python3"),
        os.path.expanduser("~/miniconda3/bin/python3"),
        os.path.expanduser("~/anaconda3/bin/python3"),
        os.path.expanduser("~/miniforge3/bin/python3"),
        os.path.expanduser("~/mambaforge/bin/python3"),
        os.path.expanduser("~/.conda/envs/*/bin/python3"),
        os.path.expanduser("~/.local/pipx/venvs/*/bin/python3"),
        os.path.expanduser("~/.cache/rattler/cache/cached-envs-v0/*/bin/python3"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
    ]
    out = []
    for pat in patterns:
        for p in sorted(glob.glob(pat)):
            if os.path.isfile(p) and os.access(p, os.X_OK):
                out.append(p)
    return out


def _find_binary(name):
    """Locate a CLI binary on PATH or in common package-manager locations."""
    p = shutil.which(name)
    if p:
        return p
    extra_Patterns = [
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        os.path.expanduser(f"~/miniconda3/bin/{name}"),
        os.path.expanduser(f"~/miniforge3/bin/{name}"),
        os.path.expanduser(f"~/.conda/envs/*/bin/{name}"),
        os.path.expanduser(f"~/.cache/rattler/cache/cached-envs-v0/*/bin/{name}"),
    ]
    for pat in extra_Patterns:
        matches = sorted(glob.glob(pat))
        if matches:
            return matches[0]
    return None


def _get_fitz():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        try:
            import fitz
            return fitz
        except ImportError:
            return None


def _get_pdfium():
    try:
        import pypdfium2
        return pypdfium2
    except ImportError:
        return None


def _ensure_pil():
    """Import Pillow (PIL), automatically re-execing into another local Python env if needed."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageChops
        return Image, ImageDraw, ImageFont, ImageChops
    except ImportError:
        if not os.environ.get("_RENDER_FIGS_REEXEC"):
            for py in _candidate_python_interpreters():
                env_root = os.path.dirname(os.path.dirname(py))
                if (glob.glob(os.path.join(env_root, "lib/python*/site-packages/PIL"))
                        or glob.glob(os.path.join(env_root, "lib/python*/site-packages/fitz"))):
                    os.environ["_RENDER_FIGS_REEXEC"] = "1"
                    os.execv(py, [py] + sys.argv)
        sys.exit(
            "ERROR: Python 'Pillow' (PIL) library is not installed in the active environment.\n"
            "  Fix Option 1 (pip):    pip install pymupdf pillow\n"
            "  Fix Option 2 (conda):  conda install -c conda-forge pymupdf pillow\n"
            "  Fix Option 3 (system): sudo apt-get install python3-pil poppler-utils  (or: brew install poppler)\n"
            "  Offline Workaround:    python3 scripts/render_figs.py extract-embedded FILE.pdf OUTDIR\n"
            "                         (uses 100% pure Python stdlib to extract embedded images without PIL)"
        )


def check_environment():
    """Print a diagnostic summary of available PDF/image backends and installation instructions."""
    fitz = _get_fitz()
    pdfium = _get_pdfium()
    has_pil = False
    try:
        import PIL
        has_pil = True
    except ImportError:
        for py in _candidate_python_interpreters():
            env_root = os.path.dirname(os.path.dirname(py))
            if glob.glob(os.path.join(env_root, "lib/python*/site-packages/PIL")):
                has_pil = f"via {py}"
                break

    pdftoppm = _find_binary("pdftoppm")
    pdftocairo = _find_binary("pdftocairo")
    pdfinfo = _find_binary("pdfinfo")
    pdftotext = _find_binary("pdftotext")
    gs = _find_binary("gs")
    magick = _find_binary("magick") or _find_binary("convert")
    ffmpeg = _find_binary("ffmpeg")
    ffprobe = _find_binary("ffprobe")

    print("=== MANUSCRIPT-AUDIT HOST ENVIRONMENT CHECK ===")
    print(f"Python version          : {sys.version.split()[0]} ({sys.executable})")
    print(f"Core text/DOCX/PDF tools: READY (100% Python stdlib — zero external dependencies)")
    print(f"Pillow (PIL)            : {has_pil if has_pil else 'NOT FOUND'}")
    print(f"PyMuPDF (fitz)          : {'FOUND' if fitz else 'not installed'}")
    print(f"pypdfium2               : {'FOUND' if pdfium else 'not installed'}")
    print(f"Poppler pdftoppm        : {pdftoppm or 'not installed'}")
    print(f"Poppler pdftocairo      : {pdftocairo or 'not installed'}")
    print(f"Poppler pdfinfo         : {pdfinfo or 'not installed'}")
    print(f"Poppler pdftotext       : {pdftotext or 'not installed'}")
    print(f"Ghostscript (gs)        : {gs or 'not installed'}")
    print(f"ImageMagick             : {magick or 'not installed'}")
    print(f"FFmpeg / FFprobe (video): {(ffmpeg + ' / ' + (ffprobe or 'no ffprobe')) if ffmpeg else 'not installed'}")

    can_render = bool(fitz or pdfium or (has_pil and (pdftoppm or pdftocairo or gs or magick)))
    if can_render:
        print("\nSTATUS: READY — Full PDF page rendering, grid overlay, and cropping are available.")
    else:
        print("\nSTATUS: PARTIAL — Full-page PDF rasterization requires one PDF renderer + Pillow.")
        print("  Recommended install (choose one):")
        print("    pip install pymupdf pillow")
        print("    sudo apt-get update && sudo apt-get install -y poppler-utils python3-pil")
        print("    brew install poppler && pip3 install pillow")
        print("  Zero-install fallback available right now:")
        print("    python3 scripts/render_figs.py extract-embedded FILE.pdf OUTDIR")


# ---------------------------------------------------------------------------
# Pure-Python PDF Page Size Parser & Embedded Image Extractor (Zero Dependencies)
# ---------------------------------------------------------------------------

def _unpack_objstm_into_map(obj_map):
    """Decompress PDF 1.5+ /Type /ObjStm object streams (Adobe Illustrator, etc.) into obj_map."""
    for onum, body in list(obj_map.items()):
        if not re.search(rb"/Type\s*/ObjStm\b", body):
            continue
        m_n = re.search(rb"/N\s+(\d+)", body)
        m_first = re.search(rb"/First\s+(\d+)", body)
        if not m_n or not m_first:
            continue
        n_objs, first_off = int(m_n.group(1)), int(m_first.group(1))
        m_st = re.search(rb"stream(?:\r\n|\n|\r)", body)
        end_st = body.rfind(b"endstream")
        if not m_st or end_st == -1 or end_st <= m_st.end():
            continue
        raw = body[m_st.end():end_st].strip()
        try:
            decomp = zlib.decompress(raw)
        except Exception:
            continue
        if len(decomp) < first_off:
            continue
        toks = decomp[:first_off].split()
        if len(toks) < 2 * n_objs:
            continue
        try:
            entries = [(int(toks[2 * i]), int(toks[2 * i + 1])) for i in range(n_objs)]
        except ValueError:
            continue
        payload = decomp[first_off:]
        for i, (sub_onum, rel_off) in enumerate(entries):
            end_off = entries[i + 1][1] if i + 1 < len(entries) else len(payload)
            if 0 <= rel_off <= end_off <= len(payload):
                obj_map.setdefault(sub_onum, payload[rel_off:end_off].strip())


def _parse_pdf_page_sizes_pure_python(pdf_path):
    """Parse (width_pt, height_pt) for each page directly from raw PDF bytes (no dependencies)."""
    data = open(pdf_path, "rb").read()
    obj_map = {}
    for m in re.finditer(rb"(?:^|\r|\n)(\d+)\s+0\s+obj\b(.*?)endobj", data, re.S):
        obj_map[int(m.group(1))] = m.group(2).strip()
    _unpack_objstm_into_map(obj_map)

    pages_root_num = None
    for onum, body in obj_map.items():
        if re.search(rb"/Type\s*/Pages\b", body):
            if not re.search(rb"/Parent\s+\d+\s+0\s+R", body):
                pages_root_num = onum
                break
            if pages_root_num is None:
                pages_root_num = onum

    if pages_root_num is None:
        return []

    def collect_pages(node_num, inherited_mb):
        body = obj_map.get(node_num, b"")
        m_mb = re.search(rb"/(?:MediaBox|CropBox)\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", body)
        mb = tuple(map(float, m_mb.groups())) if m_mb else inherited_mb
        if re.search(rb"/Type\s*/Page\b(?!s)", body):
            return [(mb[2] - mb[0], mb[3] - mb[1])]
        m_kids = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
        if not m_kids:
            return []
        kids = [int(x) for x in re.findall(rb"(\d+)\s+0\s+R", m_kids.group(1))]
        out = []
        for k in kids:
            out.extend(collect_pages(k, mb))
        return out

    return collect_pages(pages_root_num, (0.0, 0.0, 612.0, 792.0))


def _write_pure_python_png(out_path, width, height, channels, raw_pixels):
    """Write an uncompressed/zlib-compressed PNG file using only Python stdlib (`zlib`, `struct`)."""
    # Color type: 0 = Grayscale (1 ch), 2 = RGB (3 ch), 6 = RGBA (4 ch)
    color_type = {1: 0, 3: 2, 4: 6}.get(channels)
    if color_type is None:
        return False
    stride = width * channels
    if len(raw_pixels) < stride * height:
        return False

    # Prepend filter byte 0 (None) to each scanline
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        row_start = y * stride
        scanlines.extend(raw_pixels[row_start:row_start + stride])

    def png_chunk(chunk_type, data_bytes):
        crc = zlib.crc32(chunk_type + data_bytes) & 0xFFFFFFFF
        return struct.pack(">I", len(data_bytes)) + chunk_type + data_bytes + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    compressed = zlib.compress(bytes(scanlines), 6)
    with open(out_path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(png_chunk(b"IHDR", ihdr))
        f.write(png_chunk(b"IDAT", compressed))
        f.write(png_chunk(b"IEND", b""))
    return True


def extract_embedded_images_pure_python(pdf_path, outdir):
    """Extract embedded JPEG and FlateDecode PNG images from a PDF using 100% Python stdlib."""
    data = open(pdf_path, "rb").read()
    os.makedirs(outdir, exist_ok=True)
    count = 0
    for m in re.finditer(rb"(?:^|\r|\n)(\d+)\s+0\s+obj\b\s*<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream", data, re.S):
        obj_num = int(m.group(1))
        header = m.group(2)
        stream_bytes = m.group(3)
        if not re.search(rb"/Subtype\s*/Image\b", header):
            continue
        m_w = re.search(rb"/Width\s+(\d+)", header)
        m_h = re.search(rb"/Height\s+(\d+)", header)
        if not m_w or not m_h:
            continue
        w, h = int(m_w.group(1)), int(m_h.group(1))
        if w < 16 or h < 16:
            continue

        if b"/DCTDecode" in header:
            count += 1
            out_file = os.path.join(outdir, f"obj_{obj_num:04d}_{w}x{h}.jpg")
            with open(out_file, "wb") as f:
                f.write(stream_bytes)
            print(f"Extracted JPEG: {out_file} ({w}x{h} px)")
        elif b"/FlateDecode" in header:
            try:
                raw = zlib.decompress(stream_bytes)
            except Exception:
                continue
            channels = 3 if b"/DeviceRGB" in header else (1 if b"/DeviceGray" in header else 4)
            # Check if PNG Up/Paeth predictor was used (stride + 1 per row)
            if len(raw) == (w * channels + 1) * h:
                # Reconstruct PNG-predicted scanlines
                stride = w * channels
                recon = bytearray(stride * h)
                for y in range(h):
                    f_type = raw[y * (stride + 1)]
                    row = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
                    out_row_off = y * stride
                    prev_row_off = (y - 1) * stride
                    for i in range(stride):
                        val = row[i]
                        a = recon[out_row_off + i - channels] if i >= channels else 0
                        b = recon[prev_row_off + i] if y > 0 else 0
                        c = recon[prev_row_off + i - channels] if (y > 0 and i >= channels) else 0
                        if f_type == 1:
                            val = (val + a) & 0xFF
                        elif f_type == 2:
                            val = (val + b) & 0xFF
                        elif f_type == 3:
                            val = (val + ((a + b) >> 1)) & 0xFF
                        elif f_type == 4:
                            p = a + b - c
                            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                            val = (val + pr) & 0xFF
                        recon[out_row_off + i] = val
                raw = bytes(recon)

            out_file = os.path.join(outdir, f"obj_{obj_num:04d}_{w}x{h}.png")
            if _write_pure_python_png(out_file, w, h, channels, raw):
                count += 1
                print(f"Extracted PNG : {out_file} ({w}x{h} px)")
    print(f"Total embedded images extracted to {outdir}: {count}")


# ---------------------------------------------------------------------------
# Multi-Backend PDF / Image Page Size, Rendering & Image-to-PDF Wrapping
# ---------------------------------------------------------------------------

def _is_image_file(path):
    return os.path.splitext(path)[1].lower() in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp')


def _open_image_white_bg(img_path):
    """Open an image file and composite any alpha/transparency channel onto pure white (255, 255, 255) before converting to RGB."""
    Image, _, _, _ = _ensure_pil()
    im = Image.open(img_path)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, rgba).convert("RGB")
    else:
        im = im.convert("RGB")
    im.load()
    return im


def _natural_sort_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def wrap_images_to_pdf(inputs, out_pdf, ref_dpi=200.0):
    """Wrap one or more image files (or directories of images) into an uncompressed /Page PDF
    compatible with `annotate_pdf.py` (where 1 image pixel == 1 px at `--ref-dpi`)."""
    _ensure_pil()
    img_paths = []
    for inp in inputs:
        if os.path.isdir(inp):
            for fn in sorted(os.listdir(inp), key=_natural_sort_key):
                full = os.path.join(inp, fn)
                if os.path.isfile(full) and _is_image_file(full):
                    img_paths.append(full)
        elif os.path.isfile(inp) and _is_image_file(inp):
            img_paths.append(inp)
    if not img_paths:
        sys.exit(f"ERROR: No image files found in {inputs}")

    # Build a standard ISO 32000-1 PDF with uncompressed /Type /Page objects
    objects = {}  # obj_num -> bytes
    page_obj_nums = []
    next_num = 3  # 1 = Catalog, 2 = Pages

    for p_idx, img_p in enumerate(img_paths, 1):
        im = _open_image_white_bg(img_p)
        w_px, h_px = im.size
        w_pt = w_px * (72.0 / ref_dpi)
        h_pt = h_px * (72.0 / ref_dpi)

        raw_rgb = im.tobytes()
        comp_rgb = zlib.compress(raw_rgb, 6)

        img_obj_num = next_num
        content_obj_num = next_num + 1
        page_obj_num = next_num + 2
        next_num += 3
        page_obj_nums.append(page_obj_num)

        img_hdr = (
            f"<< /Type /XObject /Subtype /Image /Width {w_px} /Height {h_px} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(comp_rgb)} >>\r\nstream\r\n"
        ).encode("ascii")
        objects[img_obj_num] = img_hdr + comp_rgb + b"\r\nendstream"

        c_stream = f"q\n{w_pt:.2f} 0 0 {h_pt:.2f} 0 0 cm\n/Im1 Do\nQ\n".encode("ascii")
        objects[content_obj_num] = (
            f"<< /Length {len(c_stream)} >>\r\nstream\r\n".encode("ascii") + c_stream + b"endstream"
        )

        objects[page_obj_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w_pt:.2f} {h_pt:.2f}] "
            f"/Resources << /ProcSet [/PDF /ImageC] /XObject << /Im1 {img_obj_num} 0 R >> >> "
            f"/Contents {content_obj_num} 0 R >>"
        ).encode("ascii")
        print(f"  Page {p_idx}: {os.path.basename(img_p)} ({w_px}x{h_px} px -> {w_pt:.1f}x{h_pt:.1f} pt @ {ref_dpi:.0f} dpi)")

    kids_str = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Count {len(page_obj_nums)} /Kids [ {kids_str} ] >>".encode("ascii")

    buf = bytearray(b"%PDF-1.4\r\n%\xe2\xe3\xcf\xd3\r\n")
    offsets = {}
    for onum in sorted(objects.keys()):
        offsets[onum] = len(buf)
        buf.extend(f"{onum} 0 obj\r\n".encode("ascii") + objects[onum] + b"\r\nendobj\r\n")

    xref_pos = len(buf)
    buf.extend(f"xref\r\n0 {next_num}\r\n0000000000 65535 f \r\n".encode("ascii"))
    for onum in range(1, next_num):
        buf.extend(f"{offsets[onum]:010d} 00000 n \r\n".encode("ascii"))

    buf.extend(
        f"trailer\r\n<< /Size {next_num} /Root 1 0 R >>\r\nstartxref\r\n{xref_pos}\r\n%%EOF\r\n".encode("ascii")
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_pdf)), exist_ok=True)
    with open(out_pdf, "wb") as f:
        f.write(buf)
    print(f"Saved wrapped PDF ({len(page_obj_nums)} pages) to {out_pdf}")


def get_pdf_page_sizes_pt(pdf_path, ref_dpi=200.0):
    """Return list of (width_pt, height_pt) for each 1-indexed page in pdf_path (or image file)."""
    if _is_image_file(pdf_path):
        Image, _, _, _ = _ensure_pil()
        im = Image.open(pdf_path)
        return [(im.width * 72.0 / ref_dpi, im.height * 72.0 / ref_dpi)]

    fitz = _get_fitz()
    if fitz is not None:
        doc = fitz.open(pdf_path)
        return [(pg.rect.width, pg.rect.height) for pg in doc]

    pdfium = _get_pdfium()
    if pdfium is not None:
        doc = pdfium.PdfDocument(pdf_path)
        return [doc.get_page_size(i) for i in range(len(doc))]

    pdfinfo = _find_binary("pdfinfo")
    if pdfinfo:
        out = subprocess.check_output([pdfinfo, pdf_path], text=True, errors="replace")
        m_pages = re.search(r"Pages:\s+(\d+)", out)
        n_pages = int(m_pages.group(1)) if m_pages else 1
        out_all = subprocess.check_output([pdfinfo, "-f", "1", "-l", str(n_pages), pdf_path], text=True, errors="replace")
        sizes = []
        for m in re.finditer(r"Page\s+\d+\s+size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", out_all):
            sizes.append((float(m.group(1)), float(m.group(2))))
        if not sizes:
            m_single = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", out)
            if m_single:
                sizes = [(float(m_single.group(1)), float(m_single.group(2)))] * n_pages
        if sizes:
            return sizes

    # Fallback: Pure-Python PDF /MediaBox parser (zero external tools needed!)
    sizes = _parse_pdf_page_sizes_pure_python(pdf_path)
    if sizes:
        return sizes
    sys.exit("ERROR: Could not determine PDF page sizes. Run `render_figs.py check-env` for help.")


def render_page_to_pil(pdf_path, page_1idx, dpi, ref_dpi=200.0):
    """Render a 1-indexed PDF page (or standalone image file) at `dpi` and return a PIL RGB Image."""
    Image, _, _, _ = _ensure_pil()

    if _is_image_file(pdf_path):
        im = _open_image_white_bg(pdf_path)
        if abs(dpi - ref_dpi) > 1e-3:
            scale = dpi / ref_dpi
            im = im.resize((max(1, int(round(im.width * scale))), max(1, int(round(im.height * scale)))), Image.LANCZOS)
        return im

    # Backend 1: PyMuPDF (`fitz`)
    fitz = _get_fitz()
    if fitz is not None:
        doc = fitz.open(pdf_path)
        pg = doc[page_1idx - 1]
        pix = pg.get_pixmap(dpi=dpi)
        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    # Backend 2: pypdfium2
    pdfium = _get_pdfium()
    if pdfium is not None:
        doc = pdfium.PdfDocument(pdf_path)
        page = doc[page_1idx - 1]
        bitmap = page.render(scale=dpi / 72.0)
        return bitmap.to_pil().convert("RGB")

    # Backend 3: Poppler `pdftoppm`
    pdftoppm = _find_binary("pdftoppm")
    if pdftoppm:
        with tempfile.TemporaryDirectory() as tmpd:
            prefix = os.path.join(tmpd, "p")
            subprocess.check_call([
                pdftoppm, "-png", "-r", str(dpi),
                "-f", str(page_1idx), "-l", str(page_1idx),
                "-singlefile", pdf_path, prefix
            ])
            im = Image.open(prefix + ".png").convert("RGB")
            im.load()
            return im

    # Backend 4: Poppler `pdftocairo`
    pdftocairo = _find_binary("pdftocairo")
    if pdftocairo:
        with tempfile.TemporaryDirectory() as tmpd:
            prefix = os.path.join(tmpd, "p")
            subprocess.check_call([
                pdftocairo, "-png", "-r", str(dpi),
                "-f", str(page_1idx), "-l", str(page_1idx),
                "-singlefile", pdf_path, prefix
            ])
            im = Image.open(prefix + ".png").convert("RGB")
            im.load()
            return im

    # Backend 5: Ghostscript `gs`
    gs = _find_binary("gs")
    if gs:
        with tempfile.TemporaryDirectory() as tmpd:
            out_png = os.path.join(tmpd, "p.png")
            subprocess.check_call([
                gs, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET",
                "-sDEVICE=png16m", f"-r{dpi}",
                f"-dFirstPage={page_1idx}", f"-dLastPage={page_1idx}",
                f"-sOutputFile={out_png}", pdf_path
            ])
            im = Image.open(out_png).convert("RGB")
            im.load()
            return im

    # Backend 6: ImageMagick (`magick` or `convert`)
    magick = _find_binary("magick") or _find_binary("convert")
    if magick:
        with tempfile.TemporaryDirectory() as tmpd:
            out_png = os.path.join(tmpd, "p.png")
            subprocess.check_call([
                magick, "-density", str(dpi),
                f"{pdf_path}[{page_1idx - 1}]",
                "-background", "white", "-alpha", "remove", out_png
            ])
            im = Image.open(out_png).convert("RGB")
            im.load()
            return im

    sys.exit(
        "ERROR: No PDF rendering backend found (checked PyMuPDF, pypdfium2, pdftoppm, pdftocairo, gs, ImageMagick).\n"
        "  Install one with:  pip install pymupdf pillow\n"
        "                or:  sudo apt-get install poppler-utils python3-pil\n"
        "                or:  brew install poppler\n"
        "  Offline fallback:  python3 scripts/render_figs.py extract-embedded FILE.pdf OUTDIR"
    )


def draw_grid_overlay(im, step_px=100):
    """Return a copy of `im` with a labeled pixel coordinate grid for easy cropping."""
    Image, ImageDraw, _, _ = _ensure_pil()
    out = im.copy()
    draw = ImageDraw.Draw(out)
    w, h = out.size
    for x in range(0, w, step_px):
        color = (220, 40, 40) if x % 200 == 0 else (180, 180, 220)
        draw.line([(x, 0), (x, h)], fill=color, width=1)
        draw.text((x + 3, 4), str(x), fill=(200, 0, 0))
    for y in range(0, h, step_px):
        color = (220, 40, 40) if y % 200 == 0 else (180, 180, 220)
        draw.line([(0, y), (w, y)], fill=color, width=1)
        draw.text((4, y + 2), str(y), fill=(200, 0, 0))
    return out


def parse_pages(spec, n):
    if not spec:
        return list(range(1, n + 1))
    out = set()
    for part in spec.split(','):
        if '-' in part:
            a, b = part.split('-')
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(p for p in out if 1 <= p <= n)


_PUA_SYMBOL_MAP = str.maketrans({
    "\uf06c": "λ",
    "\uf06d": "µ",
    "\uf0b1": "±",
    "\uf044": "Δ",
    "\uf061": "α",
    "\uf062": "β",
    "\uf067": "γ",
    "\uf0b0": "°",
    "\uf0d7": "×",
    "\uf0ae": "→",
    "\uf0ac": "←",
    "\uf0ab": "↔",
})


def _normalize_pdf_pua(s):
    return s.translate(_PUA_SYMBOL_MAP) if s else ""


def _extract_words_with_bboxes_pt(pdf_path):
    """Extract per-page list of `(word_str, x0_pt, y0_pt, x1_pt, y1_pt)` using PyMuPDF or Poppler `pdftotext -bbox`."""
    if _is_image_file(pdf_path):
        return []
    fitz = _get_fitz()
    if fitz is not None:
        try:
            doc = fitz.open(pdf_path)
            pages_words = []
            for pg in doc:
                wlist = []
                for w_tup in pg.get_text("words"):
                    # w_tup: (x0, y0, x1, y1, word_str, block_no, line_no, word_no)
                    w_s = _normalize_pdf_pua(str(w_tup[4])).strip()
                    if w_s:
                        wlist.append((w_s, float(w_tup[0]), float(w_tup[1]), float(w_tup[2]), float(w_tup[3])))
                pages_words.append(wlist)
            return pages_words
        except Exception:
            pass

    pdftotext = _find_binary("pdftotext")
    if pdftotext:
        try:
            out = subprocess.check_output([pdftotext, "-bbox", pdf_path, "-"], text=True, errors="replace")
            pages_words = []
            for m_pg in re.finditer(r"<page\b[^>]*>(.*?)</page>", out, re.S):
                pg_xml = m_pg.group(1)
                wlist = []
                for m_w in re.finditer(
                    r'<word\s+xMin="([-\d.]+)"\s+yMin="([-\d.]+)"\s+xMax="([-\d.]+)"\s+yMax="([-\d.]+)"[^>]*>(.*?)</word>',
                    pg_xml,
                    re.S,
                ):
                    x0, y0, x1, y1 = map(float, m_w.groups()[:4])
                    raw_w = m_w.group(5).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
                    w_s = _normalize_pdf_pua(raw_w).strip()
                    if w_s:
                        wlist.append((w_s, x0, y0, x1, y1))
                pages_words.append(wlist)
            return pages_words
        except Exception:
            pass

    return []


def extract_pdf_page_texts(pdf_path):
    """Extract text per page from a PDF using PyMuPDF, pdftotext, pypdf, or pure-Python Flate stream parsing."""
    if _is_image_file(pdf_path):
        return [""]
    fitz = _get_fitz()
    if fitz is not None:
        doc = fitz.open(pdf_path)
        return [_normalize_pdf_pua(pg.get_text("text") or "") for pg in doc]

    pdftotext = _find_binary("pdftotext")
    if pdftotext:
        try:
            out = subprocess.check_output([pdftotext, pdf_path, "-"], text=True, errors="replace")
            pages = [_normalize_pdf_pua(p) for p in out.split("\x0c")]
            if pages and pages[-1] == "":
                pages.pop()
            return pages
        except Exception:
            pass

    for mod_name in ("pypdf", "PyPDF2"):
        try:
            mod = __import__(mod_name)
            reader = mod.PdfReader(pdf_path)
            return [_normalize_pdf_pua(pg.extract_text() or "") for pg in reader.pages]
        except Exception:
            pass

    # Pure-Python fallback: parse /Contents streams per page object
    data = open(pdf_path, "rb").read()
    obj_map = {}
    for m in re.finditer(rb"(?:^|\r|\n)(\d+)\s+0\s+obj\b(.*?)endobj", data, re.S):
        obj_map[int(m.group(1))] = m.group(2).strip()

    def decode_stream_text(obj_bytes):
        m_st = re.search(rb"stream\r?\n(.*?)\r?\nendstream", obj_bytes, re.S)
        if not m_st:
            return ""
        raw = m_st.group(1)
        if b"/FlateDecode" in obj_bytes:
            try:
                raw = zlib.decompress(raw)
            except Exception:
                return ""
        chunks = []
        for m_tj in re.finditer(rb"\(([^()\\]*(?:\\.[^()\\]*)*)\)\s*Tj|\[(.*?)\]\s*TJ", raw, re.S):
            if m_tj.group(1) is not None:
                chunks.append(m_tj.group(1).decode("latin1", errors="ignore"))
            elif m_tj.group(2) is not None:
                parts = re.findall(rb"\(([^()\\]*(?:\\.[^()\\]*)*)\)", m_tj.group(2))
                chunks.append("".join(p.decode("latin1", errors="ignore") for p in parts))
        return _normalize_pdf_pua(" ".join(chunks))

    page_texts = []
    for onum in sorted(obj_map.keys()):
        body = obj_map[onum]
        if re.search(rb"/Type\s*/Page\b(?!s)", body):
            c_refs = [int(x) for x in re.findall(rb"/Contents\s+(?:\[\s*)?(\d+)\s+0\s+R", body)]
            txt = " ".join(decode_stream_text(obj_map.get(r, b"")) for r in c_refs)
            page_texts.append(txt)
    return page_texts


def _edit_dist_le1(a, b):
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [k for k in range(len(a)) if a[k] != b[k]]
        if len(diffs) == 1:
            return True
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]:
            return True
        return False
    # Insertion/deletion of 1 char
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    i = j = diffs = 0
    while i < len(longer) and j < len(shorter):
        if longer[i] == shorter[j]:
            i += 1
            j += 1
        else:
            diffs += 1
            i += 1
            if diffs > 1:
                return False
    return True


_ID_TOKEN_PAT = re.compile(
    r"\b(?:[A-Za-z]{1,6}[_-]?\d{2,6}[A-Za-z0-9_-]*|[A-Z]{2,5}_[A-Za-z]{0,3}\d{2,6}[A-Za-z0-9_-]*|[\u0394\u2206][A-Za-z0-9_-]{2,}|P[a-z]{3,4}[A-Z]-[A-Za-z0-9_-]+|[a-z]{3}[A-Z]\b|[a-z]{2,4}[A-Z]{2,5}\d*\b|[A-Z][a-z]{1,4}-[A-Z]{2,5}\b)"
)


def _extract_docx_all_text_stdlib(docx_path):
    """Extract all plain text from a .docx file using pure Python stdlib."""
    import zipfile as _zf
    import xml.etree.ElementTree as _ET
    try:
        with _zf.ZipFile(docx_path, "r") as dz:
            root = _ET.fromstring(dz.read("word/document.xml"))
        w_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
        return " ".join(n.text or "" for n in root.iter(w_t))
    except Exception:
        return ""


def _lookup_bbox_str_for_token(page_words, token, ref_dpi=200.0):
    """Find `@200dpi` pixel bounding box(es) for `token` among `page_words` `(word_str, x0, y0, x1, y1)`."""
    if not page_words:
        return ""
    scale = ref_dpi / 72.0
    hits = []
    t_low = token.lower()
    for w_s, x0, y0, x1, y1 in page_words:
        if t_low in w_s.lower():
            bx = (int(round(x0 * scale)), int(round(y0 * scale)), int(round(x1 * scale)), int(round(y1 * scale)))
            hits.append(f"[{bx[0]}, {bx[1]}, {bx[2]}, {bx[3]}]")
    if hits:
        return f" @200dpi_px={hits[0]}" + (f" (+{len(hits)-1} more)" if len(hits) > 1 else "")
    return ""


def _detect_axis_tick_anomalies(p_words, page_clean_text, scale=200.0 / 72.0):
    """Scan aligned numeric labels on a PDF page for non-monotonic axis ticks, 10x typo outliers in
    geometric/arithmetic tick progressions (e.g. 0, 2000, 100, 50, 25, 12.5), and negative ticks on linear fold-change axes."""
    warnings = []
    if not p_words:
        return warnings
    num_items = []
    for w_s, x0, y0, x1, y1 in p_words:
        s_clean = w_s.strip().replace("\u2212", "-")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s_clean):
            try:
                val = float(s_clean)
                num_items.append((val, s_clean, x0, y0, x1, y1))
            except ValueError:
                pass
    if len(num_items) < 4:
        return warnings

    # Group into horizontal alignments (same y0 within 4 pt) and vertical alignments (same x0 within 6 pt or x1 within 6 pt)
    clusters = []
    # Horizontal clusters
    by_y = sorted(num_items, key=lambda t: (round(t[3] / 4.0), t[2]))
    cur = []
    for item in by_y:
        if not cur or abs(item[3] - cur[-1][3]) <= 4.5:
            cur.append(item)
        else:
            if len(cur) >= 4:
                clusters.append(("horizontal", sorted(cur, key=lambda t: t[2])))
            cur = [item]
    if len(cur) >= 4:
        clusters.append(("horizontal", sorted(cur, key=lambda t: t[2])))

    # Vertical clusters (left-aligned or right-aligned y-axis ticks)
    by_x = sorted(num_items, key=lambda t: (round(t[4] / 6.0), t[3]))
    cur = []
    for item in by_x:
        if not cur or abs(item[4] - cur[-1][4]) <= 6.5 or abs(item[2] - cur[-1][2]) <= 6.5:
            cur.append(item)
        else:
            if len(cur) >= 4:
                clusters.append(("vertical", sorted(cur, key=lambda t: t[3])))
            cur = [item]
    if len(cur) >= 4:
        clusters.append(("vertical", sorted(cur, key=lambda t: t[3])))

    has_linear_fc = bool(
        re.search(r"\b(?:fold[\s-]+change|FC)\b", page_clean_text, re.I)
        and not re.search(r"\b(?:log2?|ln)\s*(?:fold[\s-]+change|FC)\b", page_clean_text, re.I)
    )
    seen_seqs = set()
    for orient, seq in clusters:
        # Split sub-sequences if gap is huge (> 160 pt)
        sub_seqs = []
        sub = [seq[0]]
        for item in seq[1:]:
            prev = sub[-1]
            dist = abs(item[2] - prev[4]) if orient == "horizontal" else abs(item[3] - prev[5])
            if dist <= 140.0:
                sub.append(item)
            else:
                if len(sub) >= 4:
                    sub_seqs.append(sub)
                sub = [item]
        if len(sub) >= 4:
            sub_seqs.append(sub)

        for sseq in sub_seqs:
            vals = [t[0] for t in sseq]
            strs = [t[1] for t in sseq]
            seq_key = (orient, tuple(strs))
            if seq_key in seen_seqs:
                continue
            seen_seqs.add(seq_key)
            bx0 = int(round(min(t[2] for t in sseq) * scale))
            by0 = int(round(min(t[3] for t in sseq) * scale))
            bx1 = int(round(max(t[4] for t in sseq) * scale))
            by1 = int(round(max(t[5] for t in sseq) * scale))
            bbox_s = f"@200dpi_px=[{bx0}, {by0}, {bx1}, {by1}]"

            # Check 1: Non-monotonic sequence where all but 1 step go in the same direction (e.g. 0, 2000, 100, 50, 25, 12.5)
            pos_vals = [v for v in vals if v > 0]
            if len(vals) >= 5:
                diffs = [vals[k + 1] - vals[k] for k in range(len(vals) - 1)]
                n_pos = sum(1 for d in diffs if d > 0)
                n_neg = sum(1 for d in diffs if d < 0)
                if (n_pos == 1 and n_neg >= 3) or (n_neg == 1 and n_pos >= 3):
                    warnings.append(
                        f"Non-monotonic {orient} axis tick sequence [{', '.join(strs)}] {bbox_s} — "
                        f"verify whether an axis tick label is out of order or has an extra digit!"
                    )

            # Check 2: Geometric progression outlier (e.g. 2000, 100, 50, 25, 12.5 where ratios are 20, 2, 2, 2)
            if len(pos_vals) >= 4:
                ratios = [pos_vals[k] / pos_vals[k + 1] for k in range(len(pos_vals) - 1) if pos_vals[k + 1] != 0]
                if len(ratios) >= 3:
                    from collections import Counter
                    rounded_r = [round(r, 3) for r in ratios]
                    mode_r, mode_cnt = Counter(rounded_r).most_common(1)[0]
                    if mode_cnt >= len(ratios) - 1 and mode_cnt >= 2 and mode_r not in (0.0, 1.0):
                        outliers = [r for r in rounded_r if abs(r - mode_r) > 0.25 * abs(mode_r)]
                        if len(outliers) == 1:
                            warnings.append(
                                f"Disproportionate step in {orient} axis tick sequence [{', '.join(strs)}] {bbox_s} "
                                f"(consecutive step ratios={['%.2g' % r for r in ratios]}; expected constant factor {mode_r:g}x) — "
                                f"check for a 10x typo (e.g. '2000' instead of '200')!"
                            )

            # Check 3: Negative ticks on a linear Fold Change (FC) axis
            if has_linear_fc and any(v < 0 for v in vals) and any(v > 0 for v in vals):
                warnings.append(
                    f"Axis tick sequence [{', '.join(strs)}] {bbox_s} includes negative values on a page with "
                    f"'Fold change (FC)' — linear fold change is strictly positive (unless log-transformed or signed change)!"
                )
    return warnings


def print_pdf_page_text_map(pdf_path, compare_txt_path=None, extra_txt_paths=None):
    """Print a concise per-page figure label and text map for a PDF, plus optional cross-check against manuscript text."""
    pages = extract_pdf_page_texts(pdf_path)
    sizes = get_pdf_page_sizes_pt(pdf_path)
    pages_words = _extract_words_with_bboxes_pt(pdf_path)
    fitz = _get_fitz()
    fitz_doc = fitz.open(pdf_path) if fitz is not None else None
    n_pages = max(len(pages), len(sizes))
    fig_pat = re.compile(r"\b(?:Supplementary\s+|Supplemental\s+|Extended\s+Data\s+)?(?:Figure|Fig\.?|Table|Scheme)\s*(?:EV|ED|S)?\d+[A-Za-z]?\b", re.I)

    if not compare_txt_path and os.path.isfile("scratch_audit/manuscript_indexed.txt"):
        compare_txt_path = "scratch_audit/manuscript_indexed.txt"

    ms_text_parts = []
    if compare_txt_path and os.path.isfile(compare_txt_path):
        ms_text_parts.append(open(compare_txt_path, encoding="utf-8", errors="replace").read())
    for ep in (extra_txt_paths or []):
        if ep and os.path.isfile(ep):
            if ep.lower().endswith(".docx"):
                ms_text_parts.append(_extract_docx_all_text_stdlib(ep))
            else:
                ms_text_parts.append(open(ep, encoding="utf-8", errors="replace").read())

    # Auto-discover companion .docx files in the same folder as pdf_path so strain/reagent tables (e.g. Reagents_Table.docx) are included
    pdf_dir = os.path.dirname(os.path.abspath(pdf_path))
    if os.path.isdir(pdf_dir) and ms_text_parts:
        for fn in sorted(os.listdir(pdf_dir)):
            if fn.lower().endswith(".docx") and not fn.startswith("~$"):
                ms_text_parts.append(_extract_docx_all_text_stdlib(os.path.join(pdf_dir, fn)))

    ms_text = "\n".join(ms_text_parts)
    ms_alnum_ids = set()
    ms_words = set()
    fig_legends_by_id = {}  # normalized fig id like '1', 'S3', 'EV2' -> legend text
    if ms_text:
        for tok in _ID_TOKEN_PAT.findall(ms_text):
            ms_alnum_ids.add(tok)
        for w in re.findall(r"\b[a-zA-Z]{6,}\b", ms_text):
            ms_words.add(w.lower())
        # Parse per-figure legends so we can cross-check short reporter/construct tokens per figure
        cur_fig_id = None
        for ln in ms_text.splitlines():
            s_ln = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", ln).strip()
            m_fl = re.match(
                r"^(?:Supplementary\s+|Supplemental\s+|Extended\s+Data\s+)?(?:Figure|Fig\.?)\s*((?:EV|ED|S)?\d+)\b[.:–—\s-]",
                s_ln,
                re.I,
            )
            if m_fl and len(s_ln) > 25:
                cur_fig_id = m_fl.group(1).upper()
                if re.match(r"^(?:Supplementary|Supplemental)\s+(?:Figure|Fig)", s_ln, re.I) and not cur_fig_id.startswith("S"):
                    cur_fig_id = "S" + cur_fig_id
                fig_legends_by_id.setdefault(cur_fig_id, []).append(s_ln)
            elif cur_fig_id and s_ln and len(fig_legends_by_id.get(cur_fig_id, [])) < 12:
                if re.match(r"^(?:Table|References|Bibliography|Materials|Methods|Supplementary\s+Table)\b", s_ln, re.I):
                    cur_fig_id = None
                else:
                    fig_legends_by_id[cur_fig_id].append(s_ln)

    _REPORTER_TOKS = ("mNG", "mGL", "mNeonGreen", "mGreenLantern", "EGFP", "eGFP", "GFP", "mCherry", "mScarlet", "RFP", "YFP", "CFP", "tdTomato")

    print(f"=== PDF PAGE-TO-FIGURE TEXT MAP: {os.path.basename(pdf_path)} ({n_pages} page(s)) ===")
    for i in range(n_pages):
        txt = pages[i] if i < len(pages) else ""
        w_pt, h_pt = sizes[i] if i < len(sizes) else (0.0, 0.0)
        p_words = pages_words[i] if i < len(pages_words) else []

        # Detect garbled / unmapped /ToUnicode PDF font glyphs (e.g. Reporting Summary or Supplementary PDFs with broken font encodings)
        non_ws = [c for c in txt if not c.isspace()]
        bad_chars = sum(
            1 for c in non_ws
            if ord(c) < 32 or ord(c) == 0xFFFD or (0xE000 <= ord(c) <= 0xF8FF) or ord(c) == 0x7F
        )
        garbled_font = len(non_ws) >= 10 and (bad_chars / float(len(non_ws)) > 0.10)
        printable_txt = "".join(c if (ord(c) >= 32 and ord(c) != 0x7F and ord(c) != 0xFFFD) else " " for c in txt)
        clean = re.sub(r"\s+", " ", printable_txt).strip()

        labels = []
        for m in fig_pat.finditer(clean):
            lbl = m.group(0)
            if lbl not in labels:
                labels.append(lbl)
        lbl_str = ", ".join(labels[:6]) if labels else "(no explicit 'Figure X' text string in PDF layer)"
        print(f"Page {i+1:2d} ({w_pt:.0f}x{h_pt:.0f} pt | @200dpi={int(round(w_pt*200/72))}x{int(round(h_pt*200/72))} px) | Detected labels: {lbl_str}")
        if garbled_font:
            bad_pct = 100.0 * bad_chars / max(1, len(non_ws))
            print(
                f"         !! [UNMAPPED / GARBLED PDF FONT ENCODING WARNING] Page {i+1} text stream contains "
                f"{bad_pct:.0f}% control/unmapped characters (missing /ToUnicode CMap) — PDF text extraction is "
                f"unreliable on this page; MUST visually inspect rendered PNG at >=200 DPI!"
            )
        if clean:
            print(f"         Text preview   : {clean[:180]}{'...' if len(clean) > 180 else ''}")
        else:
            print("         Text preview   : (raster-only / outlined text — inspect rendered PNG)")

        n_vec_words = len(p_words) if p_words else len(clean.split())
        n_raster_imgs = 0
        if fitz_doc is not None and i < len(fitz_doc):
            try:
                n_raster_imgs = len(fitz_doc[i].get_images(full=True))
            except Exception:
                n_raster_imgs = 0

        if n_vec_words < 15:
            print(
                f"         !! [RASTER-HEAVY PAGE WARNING] Page {i+1} contains only {n_vec_words} vector word(s); "
                f"labels/axes are rasterized or outlined — MUST visually inspect rendered PNG at >=200 DPI!"
            )
        elif n_raster_imgs >= 1:
            print(
                f"         !! [MIXED VECTOR + RASTER PAGE WARNING] Page {i+1} has {n_vec_words} vector words AND "
                f"{n_raster_imgs} embedded raster image(s) — text inside rasterized sub-panels (e.g. heatmaps, "
                f"micrographs, or exported sub-figures) does NOT appear in the PDF text layer; visually inspect all panels!"
            )

        # Check numeric axis tick sequences on this page
        for ax_warn in _detect_axis_tick_anomalies(p_words, clean):
            print(f"         !! [AXIS TICK SEQUENCE ANOMALY] Page {i+1}: {ax_warn}")

        if ms_text and clean and not garbled_font:
            # 1. Check PDF strain/construct/gene/FP tokens that are absent from manuscript text but within edit-distance 1 of a manuscript token
            pdf_ids = set(_ID_TOKEN_PAT.findall(clean))
            for pid in sorted(pdf_ids):
                if pid not in ms_alnum_ids and pid not in ms_text:
                    near = [mid for mid in sorted(ms_alnum_ids) if _edit_dist_le1(pid, mid)]
                    if near:
                        bbox_s = _lookup_bbox_str_for_token(p_words, pid)
                        print(f"         !! PDF-vs-DOCX ID MISMATCH on Page {i+1}: PDF has '{pid}'{bbox_s} (0x in DOCX), whereas DOCX has {near}!")
            # 2. Check for baked-in spelling typos in >=6-letter words inside the PDF
            pdf_words = set(re.findall(r"\b[a-zA-Z]{6,}\b", clean))
            for pw in sorted(pdf_words):
                pw_low = pw.lower()
                if pw_low not in ms_words:
                    near_w = [
                        mw for mw in ms_words
                        if _edit_dist_le1(pw_low, mw)
                        and pw_low[:3] == mw[:3]
                        and not (pw_low + "s" == mw or mw + "s" == pw_low or pw_low + "ed" == mw or mw + "ed" == pw_low)
                    ]
                    if near_w:
                        bbox_s = _lookup_bbox_str_for_token(p_words, pw)
                        print(f"         !! SUSPECTED IN-FIGURE TYPO on Page {i+1}: PDF text has '{pw}'{bbox_s} (0x in DOCX; compare DOCX '{near_w[0]}')")
            # 3. Per-figure legend vs in-figure fluorescent reporter / tag label cross-check (e.g. Fig S3 has 'mNG' in figure vs 'mGL' in legend)
            page_fig_id = None
            for lbl in labels:
                m_fid = re.search(r"(?:Figure|Fig\.?)\s*((?:EV|ED|S)?\d+)", lbl, re.I)
                if m_fid:
                    page_fig_id = m_fid.group(1).upper()
                    if re.search(r"Supplementary|Supplemental", lbl, re.I) and not page_fig_id.startswith("S"):
                        page_fig_id = "S" + page_fig_id
                    break
            if not page_fig_id:
                pdf_base_low = os.path.basename(pdf_path).lower()
                if "supp" in pdf_base_low and f"S{i+1}" in fig_legends_by_id:
                    page_fig_id = f"S{i+1}"
                elif "main" in pdf_base_low and f"{i+1}" in fig_legends_by_id:
                    page_fig_id = f"{i+1}"
            if page_fig_id and page_fig_id in fig_legends_by_id:
                leg_blob = " ".join(fig_legends_by_id[page_fig_id])
                page_reps = [rt for rt in _REPORTER_TOKS if re.search(r"\b" + re.escape(rt) + r"\b", clean)]
                leg_reps = [rt for rt in _REPORTER_TOKS if re.search(r"\b" + re.escape(rt) + r"\b", leg_blob)]
                if page_reps and leg_reps:
                    for pr in page_reps:
                        if pr not in leg_reps and not any(pr.lower() in lr.lower() or lr.lower() in pr.lower() for lr in leg_reps):
                            bbox_s = _lookup_bbox_str_for_token(p_words, pr)
                            print(
                                f"         !! [FIGURE-VS-LEGEND REPORTER MISMATCH] Page {i+1} (Figure {page_fig_id}): "
                                f"figure text layer contains '{pr}'{bbox_s}, whereas the Figure {page_fig_id} legend "
                                f"only mentions {leg_reps}!"
                            )


def _is_video_file(path):
    return os.path.splitext(path)[1].lower() in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")


def _probe_video_pure_python(vpath):
    """Best-effort pure-Python container metadata parser for MP4/MOV and AVI when ffprobe is absent.
    Returns `(width, height, duration, codec, has_audio, total_frames, fps)`."""
    width, height, duration, codec, has_audio, total_frames, fps = 0, 0, 0.0, "unknown", False, 0, 0.0
    try:
        fsize = os.path.getsize(vpath)
        with open(vpath, "rb") as f:
            if fsize <= 32 * 1024 * 1024:
                blob = f.read()
            else:
                head = f.read(4 * 1024 * 1024)
                f.seek(max(0, fsize - 4 * 1024 * 1024))
                blob = head + f.read()
        # AVI container ('RIFF....AVI ')
        if blob[:4] == b"RIFF" and blob[8:12] == b"AVI ":
            codec = "avi"
            idx = blob.find(b"avih")
            if idx != -1 and idx + 48 <= len(blob):
                usec_per_frame, _, _, _, n_fr, _, _, _, w, h = struct.unpack("<10I", blob[idx + 8:idx + 48])
                if w > 0 and h > 0:
                    width, height = w, h
                if n_fr > 0:
                    total_frames = n_fr
                if usec_per_frame > 0:
                    fps = 1e6 / float(usec_per_frame)
                    if total_frames > 0:
                        duration = (usec_per_frame * total_frames) / 1e6
            if b"auds" in blob:
                has_audio = True
            return width, height, duration, codec, has_audio, total_frames, fps

        # MP4 / MOV container: scan for mvhd, mdhd, stsz, tkhd, hdlr, and common video sample entry fourccs
        for fourcc in (b"avc1", b"hev1", b"hvc1", b"mp4v", b"apcn", b"apch", b"jpeg", b"MJPG", b"png ", b"vp09", b"av01"):
            if fourcc in blob:
                codec = fourcc.decode("ascii", errors="ignore").strip()
                break
        m_mvhd = blob.find(b"mvhd")
        if m_mvhd != -1 and m_mvhd + 28 <= len(blob):
            ver = blob[m_mvhd + 4]
            if ver == 0 and m_mvhd + 24 <= len(blob):
                timescale, dur_units = struct.unpack(">II", blob[m_mvhd + 16:m_mvhd + 24])
                if timescale > 0 and dur_units < 0xFFFFFFFF:
                    duration = dur_units / float(timescale)
            elif ver == 1 and m_mvhd + 36 <= len(blob):
                timescale, dur_units = struct.unpack(">IQ", blob[m_mvhd + 24:m_mvhd + 36])
                if timescale > 0 and dur_units < 0xFFFFFFFFFFFFFFFF:
                    duration = dur_units / float(timescale)
        if duration == 0.0:
            for m_md in re.finditer(rb"mdhd", blob):
                pos = m_md.start()
                if pos + 24 <= len(blob):
                    ver = blob[pos + 4]
                    if ver == 0:
                        ts, du = struct.unpack(">II", blob[pos + 16:pos + 24])
                        if ts > 0 and 0 < du < 0xFFFFFFFF:
                            duration = max(duration, du / float(ts))
                    elif ver == 1 and pos + 36 <= len(blob):
                        ts, du = struct.unpack(">IQ", blob[pos + 24:pos + 36])
                        if ts > 0 and 0 < du < 0xFFFFFFFFFFFFFFFF:
                            duration = max(duration, du / float(ts))
        for m_stsz in re.finditer(rb"stsz", blob):
            pos = m_stsz.start()
            if pos + 16 <= len(blob):
                _, scnt = struct.unpack(">II", blob[pos + 8:pos + 16])
                if 1 <= scnt <= 1000000 and scnt > total_frames:
                    total_frames = scnt
        if duration > 0 and total_frames > 0:
            fps = total_frames / duration
        for m_tk in re.finditer(rb"tkhd", blob):
            pos = m_tk.start()
            if pos + 88 > len(blob):
                continue
            ver = blob[pos + 4]
            wh_off = pos + 80 if ver == 0 else pos + 92
            if wh_off + 8 <= len(blob):
                w_fixed, h_fixed = struct.unpack(">II", blob[wh_off:wh_off + 8])
                w_px, h_px = w_fixed >> 16, h_fixed >> 16
                if 16 <= w_px <= 16384 and 16 <= h_px <= 16384:
                    width, height = w_px, h_px
                    break
        if re.search(rb"hdlr.{4,16}soun", blob, re.S):
            has_audio = True
    except Exception:
        pass
    return width, height, duration, codec, has_audio, total_frames, fps


def _extract_avi_frames_pure_python(vpath, outdir, stem, n_frames, duration):
    """Extract evenly spaced frames from an uncompressed DIB or MJPEG AVI container using pure Python + PIL."""
    Image, _, _, _ = _ensure_pil()
    extracted = []
    try:
        blob = open(vpath, "rb").read()
        if blob[:4] != b"RIFF" or blob[8:12] != b"AVI ":
            return extracted
        idx = blob.find(b"strf")
        if idx == -1 or idx + 32 > len(blob):
            return extracted
        _, biW, biH, _, biBitCount, biComp, _ = struct.unpack("<IiiHHII", blob[idx + 8:idx + 32])
        abs_h = abs(biH)
        if biW <= 0 or abs_h <= 0:
            return extracted
        # Locate frame chunks ('00db' or '00dc')
        frame_chunks = []
        for m_ch in re.finditer(rb"00d[bc]", blob):
            pos = m_ch.start()
            if pos + 8 <= len(blob):
                csz = struct.unpack("<I", blob[pos + 4:pos + 8])[0]
                if csz > 100 and pos + 8 + csz <= len(blob):
                    frame_chunks.append((pos + 8, csz))
        if not frame_chunks:
            return extracted
        fracs = [i / max(1, n_frames - 1) * 0.94 for i in range(n_frames)] if n_frames > 1 else [0.0]
        for idx_f, frac in enumerate(fracs, 1):
            f_i = min(len(frame_chunks) - 1, int(round(frac * (len(frame_chunks) - 1))))
            off, csz = frame_chunks[f_i]
            raw = blob[off:off + csz]
            ts = (duration * (f_i / max(1, len(frame_chunks) - 1))) if duration > 0 else float(f_i)
            frame_jpg = os.path.join(outdir, f"{stem}_frame_{idx_f:02d}_{ts:.1f}s.jpg")
            im = None
            if biComp == 0 and biBitCount == 24 and len(raw) >= biW * abs_h * 3:
                im = Image.frombytes("RGB", (biW, abs_h), raw[:biW * abs_h * 3], "raw", "BGR", 0, -1 if biH > 0 else 1)
            elif biComp == 0 and biBitCount == 8 and len(raw) >= biW * abs_h:
                im = Image.frombytes("L", (biW, abs_h), raw[:biW * abs_h], "raw", "L", 0, -1 if biH > 0 else 1).convert("RGB")
            elif raw[:2] == b"\xff\xd8":
                import io
                im = Image.open(io.BytesIO(raw)).convert("RGB")
            if im is not None:
                im.save(frame_jpg, quality=90)
                extracted.append((ts, frame_jpg))
    except Exception:
        pass
    return extracted


def find_text_in_pdf(pdf_path, query, units="px", ref_dpi=200.0):
    """Locate exact bounding-box coordinates for `query` across all pages of `pdf_path` (supports PyMuPDF or Poppler `pdftotext -bbox`)."""
    fitz = _get_fitz()
    sizes = get_pdf_page_sizes_pt(pdf_path)
    scale = (ref_dpi / 72.0) if units == "px" else 1.0
    print(f"=== FIND-TEXT IN PDF: '{query}' in {os.path.basename(pdf_path)} (units={units}, ref_dpi={ref_dpi:.0f}) ===")
    total_hits = 0

    if fitz is not None:
        doc = fitz.open(pdf_path)
        for page_idx, page in enumerate(doc):
            rects = page.search_for(query)
            for r in rects:
                total_hits += 1
                if units == "frac":
                    x0, y0 = r.x0 / page.rect.width, r.y0 / page.rect.height
                    x1, y1 = r.x1 / page.rect.width, r.y1 / page.rect.height
                    print(f"  Page {page_idx + 1}: box=[{x0:.4f}, {y0:.4f}, {x1:.4f}, {y1:.4f}]")
                else:
                    x0, y0 = int(round(r.x0 * scale)), int(round(r.y0 * scale))
                    x1, y1 = int(round(r.x1 * scale)), int(round(r.y1 * scale))
                    pw_px = int(round(page.rect.width * scale))
                    ph_px = int(round(page.rect.height * scale))
                    pad_x, pad_y = int(round(20 * scale)), int(round(14 * scale))
                    px0, py0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
                    px1, py1 = min(pw_px, x1 + pad_x), min(ph_px, y1 + pad_y)
                    print(
                        f"  Page {page_idx + 1}: box=[{x0}, {y0}, {x1}, {y1}] "
                        f"| padded_crop_box=[{px0}, {py0}, {px1}, {py1}] "
                        f"(pt=[{r.x0:.1f}, {r.y0:.1f}, {r.x1:.1f}, {r.y1:.1f}])"
                    )
        if total_hits > 0:
            return

    # Fallback: Poppler `pdftotext -bbox` word/sequence matching
    pages_words = _extract_words_with_bboxes_pt(pdf_path)
    if not pages_words:
        sys.exit("ERROR: `find-text` requires PyMuPDF (`fitz`) or Poppler `pdftotext`. Use `render_figs.py render --grid` instead.")

    q_parts = [p.lower() for p in query.strip().split() if p.strip()]
    for page_idx, wlist in enumerate(pages_words):
        w_pt, h_pt = sizes[page_idx] if page_idx < len(sizes) else (612.0, 792.0)
        pw_px, ph_px = int(round(w_pt * scale)), int(round(h_pt * scale))
        pad_x, pad_y = int(round(20 * scale)), int(round(14 * scale))
        if len(q_parts) == 1:
            q_low = q_parts[0]
            for w_s, rx0, ry0, rx1, ry1 in wlist:
                if q_low in w_s.lower():
                    total_hits += 1
                    if units == "frac":
                        print(f"  Page {page_idx + 1}: box=[{rx0/w_pt:.4f}, {ry0/h_pt:.4f}, {rx1/w_pt:.4f}, {ry1/h_pt:.4f}] ('{w_s}')")
                    else:
                        x0, y0 = int(round(rx0 * scale)), int(round(ry0 * scale))
                        x1, y1 = int(round(rx1 * scale)), int(round(ry1 * scale))
                        px0, py0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
                        px1, py1 = min(pw_px, x1 + pad_x), min(ph_px, y1 + pad_y)
                        print(
                            f"  Page {page_idx + 1}: box=[{x0}, {y0}, {x1}, {y1}] "
                            f"| padded_crop_box=[{px0}, {py0}, {px1}, {py1}] "
                            f"(pt=[{rx0:.1f}, {ry0:.1f}, {rx1:.1f}, {ry1:.1f}]) ('{w_s}')"
                        )
        elif len(q_parts) > 1:
            k = len(q_parts)
            for idx_w in range(len(wlist) - k + 1):
                window = wlist[idx_w:idx_w + k]
                if all(q_parts[j] in window[j][0].lower() for j in range(k)):
                    total_hits += 1
                    rx0 = min(w[1] for w in window)
                    ry0 = min(w[2] for w in window)
                    rx1 = max(w[3] for w in window)
                    ry1 = max(w[4] for w in window)
                    matched_s = " ".join(w[0] for w in window)
                    if units == "frac":
                        print(f"  Page {page_idx + 1}: box=[{rx0/w_pt:.4f}, {ry0/h_pt:.4f}, {rx1/w_pt:.4f}, {ry1/h_pt:.4f}] ('{matched_s}')")
                    else:
                        x0, y0 = int(round(rx0 * scale)), int(round(ry0 * scale))
                        x1, y1 = int(round(rx1 * scale)), int(round(ry1 * scale))
                        px0, py0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
                        px1, py1 = min(pw_px, x1 + pad_x), min(ph_px, y1 + pad_y)
                        print(
                            f"  Page {page_idx + 1}: box=[{x0}, {y0}, {x1}, {y1}] "
                            f"| padded_crop_box=[{px0}, {py0}, {px1}, {py1}] "
                            f"(pt=[{rx0:.1f}, {ry0:.1f}, {rx1:.1f}, {ry1:.1f}]) ('{matched_s}')"
                        )
    if total_hits == 0:
        print(f"  (No text-layer match found for '{query}')")


def inspect_and_storyboard_videos(target, outdir, n_frames=6, compare_txt_path=None):
    """Inspect video metadata (auto-extracting .zip movie archives and companion legends) and extract an N-frame storyboard per video."""
    import zipfile as _zipfile
    import xml.etree.ElementTree as _ET
    os.makedirs(outdir, exist_ok=True)
    video_files = []
    legend_texts_by_num = {}  # v_num_str -> list of legend strings

    if not compare_txt_path and os.path.isfile("scratch_audit/manuscript_indexed.txt"):
        compare_txt_path = "scratch_audit/manuscript_indexed.txt"
    if compare_txt_path and os.path.isfile(compare_txt_path):
        for ln in open(compare_txt_path, encoding="utf-8", errors="replace"):
            m_vl = re.search(r"\b(?:Supplementary\s+|Extended\s+Data\s+)?(?:Video|Movie)\s*(?:EV|ED|S)?\s*(\d+)\b[.:–—-]\s*(.*)", ln, re.I)
            if m_vl:
                legend_texts_by_num.setdefault(m_vl.group(1), []).append(ln.strip())

    def _extract_docx_legend_to_txt(docx_p):
        txt_lines = []
        try:
            with _zipfile.ZipFile(docx_p, "r") as dz:
                root = _ET.fromstring(dz.read("word/document.xml"))
            w_p = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
            w_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
            for p_i, p_el in enumerate(root.iter(w_p), 1):
                t = "".join(n.text or "" for n in p_el.iter(w_t)).strip()
                if t:
                    txt_lines.append(f"[P{p_i}] {t}")
        except Exception:
            pass
        return txt_lines

    def _collect_from_zip(zpath):
        extract_root = os.path.join(outdir, "_unzipped_videos")
        legend_root = os.path.join(outdir, "_unzipped_legends")
        os.makedirs(extract_root, exist_ok=True)
        os.makedirs(legend_root, exist_ok=True)
        try:
            with _zipfile.ZipFile(zpath, "r") as zf:
                for member in sorted(zf.namelist(), key=_natural_sort_key):
                    base = os.path.basename(member)
                    if not base or base.startswith(".") or "__MACOSX" in member:
                        continue
                    ext_low = os.path.splitext(base)[1].lower()
                    if _is_video_file(base):
                        dst = os.path.join(extract_root, f"{os.path.splitext(os.path.basename(zpath))[0]}__{base}")
                        with zf.open(member) as src, open(dst, "wb") as out_f:
                            shutil.copyfileobj(src, out_f)
                        print(f"  [UNZIP VIDEO]  Extracted {member} from {os.path.basename(zpath)} -> {dst}")
                        video_files.append(dst)
                    elif ext_low in (".docx", ".txt", ".md", ".rtf", ".pdf"):
                        dst_leg = os.path.join(legend_root, base)
                        with zf.open(member) as src, open(dst_leg, "wb") as out_f:
                            shutil.copyfileobj(src, out_f)
                        print(f"  [UNZIP LEGEND] Extracted companion file {member} -> {dst_leg}")
                        m_vnum = re.search(r"(?:Movie|Video|SV)[_\s-]*(?:EV|ED|S)?\s*(\d+)", base, re.I)
                        if ext_low == ".docx":
                            lines = _extract_docx_legend_to_txt(dst_leg)
                            if lines:
                                txt_dst = os.path.splitext(dst_leg)[0] + ".txt"
                                with open(txt_dst, "w", encoding="utf-8") as tf:
                                    tf.write("\n".join(lines) + "\n")
                                for ln in lines:
                                    print(f"    Legend text ({base}): {ln}")
                                if m_vnum:
                                    legend_texts_by_num.setdefault(m_vnum.group(1), []).append(" ".join(lines))
                        elif ext_low in (".txt", ".md"):
                            content = open(dst_leg, encoding="utf-8", errors="replace").read().strip()
                            if content:
                                print(f"    Legend text ({base}): {content[:300]}")
                                if m_vnum:
                                    legend_texts_by_num.setdefault(m_vnum.group(1), []).append(content)
        except Exception as e:
            print(f"  [WARN] Could not inspect zip archive {zpath}: {e}")

    if os.path.isdir(target):
        for fn in sorted(os.listdir(target), key=_natural_sort_key):
            full = os.path.join(target, fn)
            if os.path.isfile(full):
                if _is_video_file(full):
                    video_files.append(full)
                elif full.lower().endswith(".zip"):
                    _collect_from_zip(full)
    elif os.path.isfile(target):
        if _is_video_file(target):
            video_files.append(target)
        elif target.lower().endswith(".zip"):
            _collect_from_zip(target)
    if not video_files:
        sys.exit(f"ERROR: No video files (.mp4/.mov/.avi/.mkv/.webm or .zip containing videos) found at {target}")

    ffmpeg = _find_binary("ffmpeg")
    ffprobe = _find_binary("ffprobe")
    Image, ImageDraw, _, _ = _ensure_pil()

    print(f"=== VIDEO METADATA & STORYBOARD EXTRACTION ({len(video_files)} file(s)) ===")
    if not ffmpeg:
        print(
            "  !! [VIDEO AUDIT GAP — NO FFMPEG]: `ffmpeg` is not installed on this host.\n"
            "     Container dimensions, duration, frame count, and FPS were inspected via pure-Python fallback\n"
            "     (and uncompressed/MJPEG AVI frames are extracted natively in Python). For H.264/MP4 files,\n"
            "     document this host limitation in the HTML report."
        )
    for vpath in video_files:
        vname = os.path.basename(vpath)
        stem = os.path.splitext(vname)[0]
        fsize_mb = os.path.getsize(vpath) / (1024 * 1024)
        duration = 0.0
        width, height = 0, 0
        codec = "unknown"
        fps_str = "unknown"
        has_audio = False
        total_frames = 0
        fps_val = 0.0

        if ffprobe:
            try:
                raw_json = subprocess.check_output([
                    ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", vpath
                ], text=True, errors="replace")
                info = json.loads(raw_json)
                fmt = info.get("format", {})
                duration = float(fmt.get("duration", 0.0) or 0.0)
                for st in info.get("streams", []):
                    if st.get("codec_type") == "video" and width == 0:
                        width = int(st.get("width", 0) or 0)
                        height = int(st.get("height", 0) or 0)
                        codec = st.get("codec_name", "unknown")
                        fps_str = st.get("r_frame_rate", "unknown")
                        if "/" in fps_str:
                            num, den = fps_str.split("/", 1)
                            if float(den) > 0:
                                fps_val = float(num) / float(den)
                        total_frames = int(st.get("nb_frames", 0) or 0)
                        if not duration and st.get("duration"):
                            duration = float(st.get("duration", 0.0))
                    elif st.get("codec_type") == "audio":
                        has_audio = True
            except Exception as e:
                print(f"  [WARN] ffprobe failed on {vname}: {e}")

        if width == 0 and height == 0:
            width, height, duration, codec, has_audio, total_frames, fps_val = _probe_video_pure_python(vpath)
            if fps_val > 0:
                fps_str = f"{fps_val:.2f} fps ({total_frames} frames)"

        print(f"\nVideo: {vname} ({fsize_mb:.2f} MB)")
        print(
            f"  Resolution : {width}x{height} px | Codec: {codec} | FPS: {fps_str} | "
            f"Frames: {total_frames or 'unknown'} | Duration: {duration:.2f}s | Audio track: {'Yes' if has_audio else 'No'}"
        )
        if has_audio:
            print("  !! NOTE: Video contains an audio track — verify whether unintentional microphone/room audio is present.")

        # Print companion or manuscript video legend text (if discovered) so the reviewer can compare FPS, frame count, and duration
        m_vn = re.search(r"(?:Movie|Video|SV)[_\s-]*(?:EV|ED|S)?\s*(\d+)", vname, re.I)
        if m_vn and m_vn.group(1) in legend_texts_by_num:
            for leg_s in legend_texts_by_num[m_vn.group(1)]:
                print(f"  Legend text: {leg_s[:320]}")

        extracted_frames = []
        if ffmpeg:
            fracs = [i / max(1, n_frames - 1) * 0.94 for i in range(n_frames)] if n_frames > 1 else [0.0]
            for idx, frac in enumerate(fracs, 1):
                ts = max(0.0, duration * frac) if duration > 0 else float(idx - 1)
                frame_jpg = os.path.join(outdir, f"{stem}_frame_{idx:02d}_{ts:.1f}s.jpg")
                try:
                    subprocess.check_call([
                        ffmpeg, "-y", "-v", "error", "-ss", f"{ts:.2f}",
                        "-i", vpath, "-frames:v", "1", "-q:v", "2", frame_jpg
                    ])
                    if os.path.isfile(frame_jpg):
                        extracted_frames.append((ts, frame_jpg))
                except Exception as e:
                    print(f"  [WARN] Frame extraction at {ts:.2f}s failed: {e}")
        elif vname.lower().endswith(".avi"):
            extracted_frames = _extract_avi_frames_pure_python(vpath, outdir, stem, n_frames, duration)

        if extracted_frames:
            cols = min(3, len(extracted_frames))
            rows = (len(extracted_frames) + cols - 1) // cols
            cell_w, cell_h = 480, 340
            sheet = Image.new("RGB", (cols * cell_w, rows * cell_h + 36), (242, 244, 247))
            draw = ImageDraw.Draw(sheet)
            draw.text(
                (12, 10),
                f"{vname} — {width}x{height}px, {duration:.2f}s, {fps_str}, codec={codec}, audio={'yes' if has_audio else 'no'}",
                fill=(15, 23, 42),
            )
            prev_gray_small = None
            prev_ts = 0.0
            frame_deltas = []
            for idx, (ts, fp) in enumerate(extracted_frames):
                r, c = divmod(idx, cols)
                im = _open_image_white_bg(fp)
                try:
                    g_small = im.convert("L").resize((64, 64))
                    px_vals = list(g_small.getdata())
                    if prev_gray_small is not None and len(px_vals) == len(prev_gray_small):
                        mad = sum(abs(a - b) for a, b in zip(px_vals, prev_gray_small)) / float(len(px_vals))
                        frame_deltas.append((idx, prev_ts, ts, mad))
                    prev_gray_small = px_vals
                    prev_ts = ts
                except Exception:
                    pass
                im.thumbnail((cell_w - 20, cell_h - 36))
                x = c * cell_w + 10
                y = 36 + r * cell_h + 26
                sheet.paste(im, (x, y))
                draw.rectangle([x - 1, y - 1, x + im.width + 1, y + im.height + 1], outline=(30, 64, 175), width=2)
                draw.text((x, y - 18), f"Frame #{idx} @ t = {ts:.2f}s ({int(round(ts/duration*100)) if duration > 0 else 0}%)", fill=(30, 41, 59))
            sb_path = os.path.join(outdir, f"{stem}_storyboard.jpg")
            sheet.save(sb_path, quality=90)
            print(f"  Storyboard : {sb_path} ({len(extracted_frames)} frames sampled)")
            if len(frame_deltas) >= 3:
                sorted_mads = sorted(d[3] for d in frame_deltas)
                med_mad = max(1.5, sorted_mads[len(sorted_mads) // 2])
                for f_idx, ts0, ts1, mad in frame_deltas:
                    if mad > max(18.0, 2.8 * med_mad):
                        print(
                            f"  !! [ABRUPT VIDEO FRAME TRANSITION / SCENE CUT] Frame #{f_idx-1} ({ts0:.2f}s) -> "
                            f"Frame #{f_idx} ({ts1:.2f}s) shows an abrupt visual jump (mean pixel delta={mad:.1f} vs "
                            f"median {med_mad:.1f}) — inspect storyboard for spliced scenes, mid-movie contrast shifts, or dropped frames!"
                        )


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def _opt_all(name):
    out = []
    args = sys.argv[1:]
    for idx, a in enumerate(args):
        if a == name and idx + 1 < len(args):
            out.append(args[idx + 1])
    return out


def _validate_crop_content(cropped_im, out_path, x0, y0, x1, y1):
    """Warn immediately if a saved crop is blank/near-blank (<0.5% non-white pixels) or clips dark ink along borders."""
    try:
        small = cropped_im.convert("RGB")
        if small.width > 240 or small.height > 240:
            small = small.resize((min(240, small.width), min(240, small.height)))
        pxs = list(small.getdata())
        if not pxs:
            return
        non_white = sum(1 for r, g, b in pxs if r < 245 or g < 245 or b < 245)
        nw_frac = non_white / float(len(pxs))
        if nw_frac < 0.005:
            print(
                f"  !! [BLANK / NEAR-BLANK CROP WARNING] {out_path} is {100.0 * (1.0 - nw_frac):.1f}% empty/white! "
                f"Coordinates ({x0:g},{y0:g},{x1:g},{y1:g}) missed the figure panel. "
                f"Re-locate with `render_figs.py find-text` or `render --grid` and re-crop!"
            )
            return
        # Check if dark text/ink (<165 luminance) touches outer 1px borders extensively
        w_s, h_s = small.width, small.height
        if w_s >= 20 and h_s >= 20:
            def _is_dark(rgb):
                return (rgb[0] + rgb[1] + rgb[2]) < 495
            top_dark = sum(1 for x in range(w_s) if _is_dark(pxs[x])) / float(w_s)
            bot_dark = sum(1 for x in range(w_s) if _is_dark(pxs[(h_s - 1) * w_s + x])) / float(w_s)
            left_dark = sum(1 for y in range(h_s) if _is_dark(pxs[y * w_s])) / float(h_s)
            right_dark = sum(1 for y in range(h_s) if _is_dark(pxs[y * w_s + (w_s - 1)])) / float(h_s)
            clipped = []
            if top_dark > 0.18:
                clipped.append("top")
            if bot_dark > 0.18:
                clipped.append("bottom")
            if left_dark > 0.18:
                clipped.append("left")
            if right_dark > 0.18:
                clipped.append("right")
            if clipped and len(clipped) <= 3:
                print(
                    f"  [NOTE: Crop border check] {out_path} has dark pixels along {', '.join(clipped)} edge(s) — "
                    f"verify that panel headers or axis labels are not clipped (pass `--pad 15` to add padding)."
                )
    except Exception:
        pass


def _perform_single_crop(im, w_pt, h_pt, x0, y0, x1, y1, dpi, units, ref_dpi, out_path, page_1idx, explicit_units=False, pad_px=0):
    eff_units = units
    if not explicit_units and units == "pt" and (x1 > w_pt * 1.05 or y1 > h_pt * 1.05):
        eff_units = "px"
        print(
            f"  [AUTO-DETECTED units=px @ {ref_dpi:.0f} dpi because coordinates ({x0:g},{y0:g},{x1:g},{y1:g}) "
            f"exceed page pt bounds ({w_pt:.0f}x{h_pt:.0f} pt)]"
        )
    if eff_units == 'pt':
        scale_x = im.width / w_pt
        scale_y = im.height / h_pt
    elif eff_units == 'px':
        scale_x = dpi / ref_dpi
        scale_y = dpi / ref_dpi
    elif eff_units == 'frac':
        scale_x = im.width
        scale_y = im.height
    else:
        sys.exit(f"Unknown --units '{eff_units}' (use pt, px, or frac)")

    pad_render = int(round(pad_px * (dpi / ref_dpi))) if pad_px > 0 else 0
    box = (
        max(0, int(round(x0 * scale_x)) - pad_render),
        max(0, int(round(y0 * scale_y)) - pad_render),
        min(im.width, int(round(x1 * scale_x)) + pad_render),
        min(im.height, int(round(y1 * scale_y)) + pad_render),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        sys.exit(
            f"ERROR: Invalid/out-of-bounds crop box ({x0:g}, {y0:g}, {x1:g}, {y1:g}) with units={eff_units} "
            f"on page {page_1idx} ({w_pt:.0f}x{h_pt:.0f} pt | @200dpi={int(round(w_pt*200/72))}x{int(round(h_pt*200/72))} px). "
            f"Did you mean `--units px`?"
        )
    cropped = im.crop(box)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if out_path.lower().endswith(('.jpg', '.jpeg')):
        cropped.save(out_path, format="JPEG", quality=90, optimize=True)
    else:
        cropped.save(out_path)
    print(f"{out_path}: {cropped.width}x{cropped.height} px (cropped from page {page_1idx} @ {dpi} dpi, units={eff_units})")
    _validate_crop_content(cropped, out_path, x0, y0, x1, y1)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        sys.exit(__doc__)

    cmd = sys.argv[1]

    if cmd in ('check-env', '--check-env'):
        check_environment()
        return

    if len(sys.argv) < 3:
        sys.exit(__doc__)

    if cmd == 'extract-embedded':
        pdf, outdir = sys.argv[2], sys.argv[3]
        extract_embedded_images_pure_python(pdf, outdir)
        return

    if cmd in ('wrap-images', 'images-to-pdf'):
        dpi = float(opt('--dpi', 200))
        pos_args = []
        skip_next = False
        for a in sys.argv[2:]:
            if skip_next:
                skip_next = False
                continue
            if a == '--dpi':
                skip_next = True
                continue
            pos_args.append(a)
        if len(pos_args) < 2:
            sys.exit("Usage: render_figs.py wrap-images IMG_OR_DIR [IMG2 ...] OUT.pdf [--dpi 200]")
        wrap_images_to_pdf(pos_args[:-1], pos_args[-1], ref_dpi=dpi)
        return

    if cmd == 'size':
        pdf = sys.argv[2]
        sizes = get_pdf_page_sizes_pt(pdf)
        if len(sys.argv) >= 4:
            p = int(sys.argv[3])
            w, h = sizes[p - 1]
            print(f"page {p}: {w:.1f} x {h:.1f} pt | @150dpi={int(w*150/72)}x{int(h*150/72)} px | @200dpi={int(w*200/72)}x{int(h*200/72)} px")
        else:
            for idx, (w, h) in enumerate(sizes, 1):
                print(f"page {idx}: {w:.1f} x {h:.1f} pt | @200dpi={int(w*200/72)}x{int(h*200/72)} px")
        return

    if cmd in ('page-text', 'text-map'):
        pdf = sys.argv[2]
        cmp_txt = opt('--compare-text', None) or opt('--manuscript-txt', None)
        extras = _opt_all('--extra-txt')
        print_pdf_page_text_map(pdf, compare_txt_path=cmp_txt, extra_txt_paths=extras)
        return

    if cmd in ('find-text', 'search-text'):
        if len(sys.argv) < 4:
            sys.exit("Usage: render_figs.py find-text PDF_FILE QUERY [--units px --ref-dpi 200]")
        pdf, query = sys.argv[2], sys.argv[3]
        units = opt('--units', 'px')
        ref_dpi = float(opt('--ref-dpi', 200))
        find_text_in_pdf(pdf, query, units=units, ref_dpi=ref_dpi)
        return

    if cmd in ('video', 'videos', 'extract-frames'):
        if len(sys.argv) < 4:
            sys.exit("Usage: render_figs.py video VIDEO_OR_DIR OUTDIR [--frames 6] [--compare-text manuscript_indexed.txt]")
        target, outdir = sys.argv[2], sys.argv[3]
        n_frames = int(opt('--frames', 6))
        cmp_txt = opt('--compare-text', None) or opt('--manuscript-txt', None)
        inspect_and_storyboard_videos(target, outdir, n_frames=n_frames, compare_txt_path=cmp_txt)
        return

    if cmd == 'render':
        Image, _, _, ImageChops = _ensure_pil()
        pdf, outdir = sys.argv[2], sys.argv[3]
        dpi = int(opt('--dpi', 200))
        prefix = opt('--prefix', 'page')
        want_grid = '--grid' in sys.argv
        sizes = get_pdf_page_sizes_pt(pdf)
        pages = parse_pages(opt('--pages'), len(sizes))
        os.makedirs(outdir, exist_ok=True)
        for p in pages:
            im = render_page_to_pil(pdf, p, dpi)
            out_path = os.path.join(outdir, f"{prefix}_{p}.png")
            im.save(out_path)
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bbox = ImageChops.difference(im, bg).getbbox()
            w_pt, h_pt = sizes[p - 1]
            print(f"{out_path}: {im.width}x{im.height} px (page {w_pt:.0f}x{h_pt:.0f} pt, content_bbox_px={bbox})")
            if want_grid:
                grid_path = os.path.join(outdir, f"{prefix}_{p}_grid.png")
                draw_grid_overlay(im).save(grid_path)
                print(f"  + grid overlay: {grid_path}")
        return

    if cmd == 'crop':
        pdf = sys.argv[2]
        page_1idx = int(sys.argv[3])
        dpi = int(opt('--dpi', 400))
        pad_px = int(opt('--pad', 0))
        explicit_units = '--units' in sys.argv
        units = opt('--units', 'pt')
        ref_dpi = float(opt('--ref-dpi', 200))
        out = opt('--out', None)
        if ',' in sys.argv[4]:
            arg4 = sys.argv[4]
            if ':' in arg4:
                coords_s, out_inline = arg4.split(':', 1)
                if not out:
                    out = out_inline
            else:
                coords_s = arg4
            x0, y0, x1, y1 = map(float, coords_s.split(','))
            if not out:
                if len(sys.argv) >= 6 and not sys.argv[5].startswith('-'):
                    out = sys.argv[5]
                else:
                    out = 'crop.jpg'
        else:
            x0, y0, x1, y1 = map(float, sys.argv[4:8])
            if not out:
                if len(sys.argv) >= 9 and not sys.argv[8].startswith('-'):
                    out = sys.argv[8]
                else:
                    out = 'crop.jpg'

        sizes = get_pdf_page_sizes_pt(pdf)
        w_pt, h_pt = sizes[page_1idx - 1]
        im = render_page_to_pil(pdf, page_1idx, dpi)
        _perform_single_crop(im, w_pt, h_pt, x0, y0, x1, y1, dpi, units, ref_dpi, out, page_1idx, explicit_units=explicit_units, pad_px=pad_px)
        return

    if cmd == 'batch-crop':
        dpi = int(opt('--dpi', 400))
        pad_px = int(opt('--pad', 0))
        explicit_units = '--units' in sys.argv
        units = opt('--units', 'px')
        ref_dpi = float(opt('--ref-dpi', 200))
        if sys.argv[2].lower().endswith('.json') and os.path.isfile(sys.argv[2]):
            specs = json.load(open(sys.argv[2], encoding='utf-8'))
            # Group by (pdf, page, dpi) so each page is rendered only once
            grouped = {}
            for sp in specs:
                k = (sp['pdf'], int(sp.get('page', 1)), int(sp.get('dpi', dpi)))
                grouped.setdefault(k, []).append(sp)
            for (pdf_p, pg_i, r_dpi), items in grouped.items():
                sizes = get_pdf_page_sizes_pt(pdf_p)
                w_pt, h_pt = sizes[pg_i - 1]
                im = render_page_to_pil(pdf_p, pg_i, r_dpi)
                for sp in items:
                    bx = sp['box']
                    u = sp.get('units', units)
                    rf = float(sp.get('ref_dpi', ref_dpi))
                    sp_pad = int(sp.get('pad', pad_px))
                    _perform_single_crop(im, w_pt, h_pt, float(bx[0]), float(bx[1]), float(bx[2]), float(bx[3]), r_dpi, u, rf, sp['out'], pg_i, explicit_units=('units' in sp or explicit_units), pad_px=sp_pad)
            return
        # CLI forms supported:
        # 1) Single-page: render_figs.py batch-crop FILE.pdf PAGE X0,Y0,X1,Y1:OUT1.jpg [X0,Y0,X1,Y1:OUT2.jpg ...]
        # 2) Multi-page inline: render_figs.py batch-crop FILE.pdf PAGE1@X0,Y0,X1,Y1:OUT1.jpg PAGE2@X0,Y0,X1,Y1:OUT2.jpg ...
        pdf = sys.argv[2]
        sizes = get_pdf_page_sizes_pt(pdf)
        default_page = None
        start_idx = 3
        if re.fullmatch(r"\d+", sys.argv[3]):
            default_page = int(sys.argv[3])
            start_idx = 4

        # Group specs by page so each page is rendered at most once
        by_page_specs = {}
        skip_next = False
        for a in sys.argv[start_idx:]:
            if skip_next:
                skip_next = False
                continue
            if a in ('--dpi', '--units', '--ref-dpi', '--pad'):
                skip_next = True
                continue
            if re.fullmatch(r"\d+", a):
                default_page = int(a)
                continue
            if ':' in a:
                left_s, out_p = a.split(':', 1)
                if '@' in left_s:
                    pg_s, coords_s = left_s.split('@', 1)
                    pg_i = int(pg_s)
                else:
                    if default_page is None:
                        sys.exit("ERROR: Specify page number either as `batch-crop FILE.pdf PAGE X0,Y0,X1,Y1:OUT.jpg` or `PAGE@X0,Y0,X1,Y1:OUT.jpg`.")
                    pg_i = default_page
                    coords_s = left_s
                x0, y0, x1, y1 = map(float, coords_s.split(','))
                by_page_specs.setdefault(pg_i, []).append((x0, y0, x1, y1, out_p))

        for pg_i, items in by_page_specs.items():
            w_pt, h_pt = sizes[pg_i - 1]
            im = render_page_to_pil(pdf, pg_i, dpi)
            for x0, y0, x1, y1, out_p in items:
                _perform_single_crop(im, w_pt, h_pt, x0, y0, x1, y1, dpi, units, ref_dpi, out_p, pg_i, explicit_units=explicit_units, pad_px=pad_px)
        return

    if cmd == 'contact-sheet':
        Image, ImageDraw, _, _ = _ensure_pil()
        crops_dir = sys.argv[2]
        out = opt('--out', os.path.join(crops_dir, 'contact_sheet.png'))
        cols = int(opt('--cols', 3))
        files = sorted(
            f for f in os.listdir(crops_dir)
            if f.lower().endswith(('.png', '.jpg', '.jpeg')) and not f.startswith('contact_sheet')
        )
        if not files:
            sys.exit(f"No images found in {crops_dir}")
        cell_w, cell_h = 480, 360
        rows = (len(files) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (242, 244, 247))
        draw = ImageDraw.Draw(sheet)
        for idx, fname in enumerate(files):
            r, c = divmod(idx, cols)
            im = _open_image_white_bg(os.path.join(crops_dir, fname))
            orig_w, orig_h = im.size
            im.thumbnail((cell_w - 24, cell_h - 42))
            x = c * cell_w + 12
            y = r * cell_h + 30
            sheet.paste(im, (x, y))
            draw.rectangle([x - 1, y - 1, x + im.width + 1, y + im.height + 1], outline=(200, 30, 30), width=2)
            draw.text((c * cell_w + 12, r * cell_h + 8), f"{fname} ({orig_w}x{orig_h})", fill=(15, 23, 42))
        sheet.save(out)
        print(f"Saved contact sheet with {len(files)} crops to {out} ({sheet.width}x{sheet.height} px)")
        return

    if cmd == 'compare':
        Image, ImageDraw, ImageFont, _ = _ensure_pil()
        img1_path, img2_path, out_path = sys.argv[2], sys.argv[3], sys.argv[4]
        im1 = _open_image_white_bg(img1_path)
        im2 = _open_image_white_bg(img2_path)
        label1 = opt('--label1', os.path.basename(img1_path))
        label2 = opt('--label2', os.path.basename(img2_path))
        box1 = opt('--box1')
        box2 = opt('--box2')

        pad = 24
        top_h = 44
        w = im1.width + im2.width + pad * 3
        h = max(im1.height, im2.height) + top_h + pad
        canvas = Image.new("RGB", (w, h), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        canvas.paste(im1, (pad, top_h))
        x2_off = pad * 2 + im1.width
        canvas.paste(im2, (x2_off, top_h))

        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
        except Exception:
            font = ImageFont.load_default()

        draw.text((pad, 12), label1, fill=(185, 28, 28), font=font)
        draw.text((x2_off, 12), label2, fill=(21, 128, 61), font=font)

        if box1:
            bx0, by0, bx1, by1 = map(int, box1.split(','))
            draw.rectangle([pad + bx0, top_h + by0, pad + bx1, top_h + by1], outline=(220, 20, 20), width=4)
        if box2:
            bx0, by0, bx1, by1 = map(int, box2.split(','))
            draw.rectangle([x2_off + bx0, top_h + by0, x2_off + bx1, top_h + by1], outline=(0, 140, 60), width=4)

        canvas.save(out_path, quality=92)
        print(f"Saved side-by-side comparison to {out_path} ({canvas.width}x{canvas.height} px)")
        return

    if cmd == 'pdf-to-indexed-txt':
        pdf_path = sys.argv[2]
        out_txt = opt('--out')
        pages = extract_pdf_page_texts(pdf_path)
        paras = [f"# MANUSCRIPT-AUDIT EXTRACT | file={os.path.basename(pdf_path)} | path={os.path.abspath(pdf_path)}"]
        p_idx = 0
        start_new_re = re.compile(
            r'^(?:(?:Supplementary|Supplemental|Expanded\s+View|Extended\s+Data)\s+(?:Figure|Fig\.?|Table|Note|Methods?|Text|Video|Movie|References?)|'
            r'(?:Figure|Fig\.?|Table)\s+[A-Za-z0-9]+|(?:\[[sS]?\d{1,3}\]|\([sS]?\d{1,3}\)|[sS]?\d{1,3}[.)])\s+[A-Z])',
            re.I,
        )
        for pg_no, pg_txt in enumerate(pages, 1):
            cur_buf = []
            for raw_ln in pg_txt.splitlines():
                ln = raw_ln.strip()
                if not ln or re.match(r'^(?:page\s+)?\d+\s*(?:of\s+\d+)?$', ln, re.I):
                    if cur_buf:
                        p_idx += 1
                        paras.append(f"[P{p_idx}] " + " ".join(cur_buf))
                        cur_buf = []
                    continue
                if cur_buf and (start_new_re.match(ln) or (len(cur_buf[-1]) < 55 and not cur_buf[-1].endswith(('.', ',', ';', ':')))):
                    p_idx += 1
                    paras.append(f"[P{p_idx}] " + " ".join(cur_buf))
                    cur_buf = [ln]
                else:
                    if cur_buf and cur_buf[-1].endswith('-') and ln and ln[0].islower():
                        cur_buf[-1] = cur_buf[-1][:-1] + ln
                    else:
                        cur_buf.append(ln)
            if cur_buf:
                p_idx += 1
                paras.append(f"[P{p_idx}] " + " ".join(cur_buf))
        full_out = "\n".join(paras) + "\n"
        if out_txt:
            with open(out_txt, "w", encoding="utf-8") as f:
                f.write(full_out)
        else:
            sys.stdout.write(full_out)
        return

    sys.exit(__doc__)


if __name__ == '__main__':
    main()
