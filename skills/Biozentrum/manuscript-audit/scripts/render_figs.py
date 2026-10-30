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
  render_figs.py page-text        FILE.pdf
  render_figs.py render           FILE.pdf|IMG OUTDIR [--dpi 200] [--pages 1-3,5] [--prefix page] [--grid]
  render_figs.py crop             FILE.pdf|IMG PAGE X0 Y0 X1 Y1 [--dpi 400] [--units pt|px|frac] [--ref-dpi 200] [--out crop.jpg]
  render_figs.py video            VIDEO_OR_DIR OUTDIR [--frames 6]
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


def _natural_sort_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def wrap_images_to_pdf(inputs, out_pdf, ref_dpi=200.0):
    """Wrap one or more image files (or directories of images) into an uncompressed /Page PDF
    compatible with `annotate_pdf.py` (where 1 image pixel == 1 px at `--ref-dpi`)."""
    Image, _, _, _ = _ensure_pil()
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
        im = Image.open(img_p).convert("RGB")
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
        im = Image.open(pdf_path).convert("RGB")
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


def extract_pdf_page_texts(pdf_path):
    """Extract text per page from a PDF using PyMuPDF, pdftotext, pypdf, or pure-Python Flate stream parsing."""
    fitz = _get_fitz()
    if fitz is not None:
        doc = fitz.open(pdf_path)
        return [pg.get_text("text") or "" for pg in doc]

    pdftotext = _find_binary("pdftotext")
    if pdftotext:
        try:
            out = subprocess.check_output([pdftotext, pdf_path, "-"], text=True, errors="replace")
            pages = out.split("\x0c")
            if pages and pages[-1] == "":
                pages.pop()
            return pages
        except Exception:
            pass

    for mod_name in ("pypdf", "PyPDF2"):
        try:
            mod = __import__(mod_name)
            reader = mod.PdfReader(pdf_path)
            return [pg.extract_text() or "" for pg in reader.pages]
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
        return " ".join(chunks)

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


def print_pdf_page_text_map(pdf_path, compare_txt_path=None):
    """Print a concise per-page figure label and text map for a PDF, plus optional cross-check against manuscript text."""
    pages = extract_pdf_page_texts(pdf_path)
    sizes = get_pdf_page_sizes_pt(pdf_path)
    n_pages = max(len(pages), len(sizes))
    fig_pat = re.compile(r"\b(?:Supplementary\s+|Supplemental\s+|Extended\s+Data\s+)?(?:Figure|Fig\.?|Table|Scheme)\s*S?\d+[A-Za-z]?\b", re.I)

    if not compare_txt_path and os.path.isfile("scratch_audit/manuscript_indexed.txt"):
        compare_txt_path = "scratch_audit/manuscript_indexed.txt"

    ms_text = ""
    ms_alnum_ids = set()
    ms_words = set()
    if compare_txt_path and os.path.isfile(compare_txt_path):
        ms_text = open(compare_txt_path, encoding="utf-8", errors="replace").read()
        # Collect strain/gene/construct/compound codes from manuscript text
        for tok in re.findall(r"\b(?:[A-Za-z]{1,6}-?\d{2,6}[A-Za-z0-9_-]*|[\u0394\u2206][A-Za-z0-9_-]+|P[a-z]{3,4}[A-Z]-[A-Za-z0-9_-]+|[a-z]{3}[A-Z]\b)", ms_text):
            ms_alnum_ids.add(tok)
        for w in re.findall(r"\b[a-zA-Z]{6,}\b", ms_text):
            ms_words.add(w.lower())

    print(f"=== PDF PAGE-TO-FIGURE TEXT MAP: {os.path.basename(pdf_path)} ({n_pages} page(s)) ===")
    for i in range(n_pages):
        txt = pages[i] if i < len(pages) else ""
        w_pt, h_pt = sizes[i] if i < len(sizes) else (0.0, 0.0)
        clean = re.sub(r"\s+", " ", txt).strip()
        labels = []
        for m in fig_pat.finditer(clean):
            lbl = m.group(0)
            if lbl not in labels:
                labels.append(lbl)
        lbl_str = ", ".join(labels[:6]) if labels else "(no explicit 'Figure X' text string in PDF layer)"
        print(f"Page {i+1:2d} ({w_pt:.0f}x{h_pt:.0f} pt | @200dpi={int(round(w_pt*200/72))}x{int(round(h_pt*200/72))} px) | Detected labels: {lbl_str}")
        if clean:
            print(f"         Text preview   : {clean[:180]}{'...' if len(clean) > 180 else ''}")
        else:
            print("         Text preview   : (raster-only / outlined text — inspect rendered PNG)")

        if ms_text and clean:
            # 1. Check PDF strain/construct/gene tokens that are absent from manuscript text but within edit-distance 1 of a manuscript token
            pdf_ids = set(re.findall(r"\b(?:[A-Za-z]{1,6}-?\d{2,6}[A-Za-z0-9_-]*|[\u0394\u2206][A-Za-z0-9_-]+|P[a-z]{3,4}[A-Z]-[A-Za-z0-9_-]+|[a-z]{3}[A-Z]\b)", clean))
            for pid in sorted(pdf_ids):
                if pid not in ms_alnum_ids and pid not in ms_text:
                    near = [mid for mid in sorted(ms_alnum_ids) if _edit_dist_le1(pid, mid)]
                    if near:
                        print(f"         !! PDF-vs-DOCX ID MISMATCH on Page {i+1}: PDF has '{pid}' (0x in DOCX), whereas DOCX has {near}!")
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
                        print(f"         !! SUSPECTED IN-FIGURE TYPO on Page {i+1}: PDF text has '{pw}' (0x in DOCX; compare DOCX '{near_w[0]}')")


def _is_video_file(path):
    return os.path.splitext(path)[1].lower() in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")


def _probe_video_pure_python(vpath):
    """Best-effort pure-Python container metadata parser for MP4/MOV and AVI when ffprobe is absent."""
    width, height, duration, codec, has_audio = 0, 0, 0.0, "unknown", False
    try:
        with open(vpath, "rb") as f:
            head = f.read(2 * 1024 * 1024)
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            tail = b""
            if fsize > len(head):
                f.seek(max(0, fsize - 2 * 1024 * 1024))
                tail = f.read()
        blob = head + tail
        # AVI container ('RIFF....AVI ')
        if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
            codec = "avi"
            idx = blob.find(b"avih")
            if idx != -1 and idx + 48 <= len(blob):
                usec_per_frame, _, _, _, total_frames, _, _, _, w, h = struct.unpack("<10I", blob[idx + 8:idx + 48])
                if w > 0 and h > 0:
                    width, height = w, h
                if usec_per_frame > 0 and total_frames > 0:
                    duration = (usec_per_frame * total_frames) / 1e6
            if b"auds" in blob:
                has_audio = True
            return width, height, duration, codec, has_audio

        # MP4 / MOV container: scan for mvhd, tkhd, hdlr, and common video sample entry fourccs
        for fourcc in (b"avc1", b"hev1", b"hvc1", b"mp4v", b"apcn", b"apch", b"jpeg", b"vp09", b"av01"):
            if fourcc in blob:
                codec = fourcc.decode("ascii", errors="ignore")
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
    return width, height, duration, codec, has_audio


def find_text_in_pdf(pdf_path, query, units="px", ref_dpi=200.0):
    """Locate exact bounding-box coordinates for `query` across all pages of `pdf_path`."""
    try:
        import fitz
    except ImportError:
        sys.exit("ERROR: `find-text` requires PyMuPDF (`fitz`). Use `render_figs.py render --grid` when PyMuPDF is unavailable.")

    doc = fitz.open(pdf_path)
    scale = (ref_dpi / 72.0) if units == "px" else 1.0
    print(f"=== FIND-TEXT IN PDF: '{query}' in {os.path.basename(pdf_path)} (units={units}, ref_dpi={ref_dpi:.0f}) ===")
    total_hits = 0
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
                print(f"  Page {page_idx + 1}: box=[{x0}, {y0}, {x1}, {y1}] (pt=[{r.x0:.1f}, {r.y0:.1f}, {r.x1:.1f}, {r.y1:.1f}])")
    if total_hits == 0:
        print(f"  (No exact text-layer match found for '{query}')")


def inspect_and_storyboard_videos(target, outdir, n_frames=6):
    """Inspect video metadata (auto-extracting .zip movie archives and companion legends) and extract an N-frame storyboard per video."""
    import zipfile as _zipfile
    import xml.etree.ElementTree as _ET
    os.makedirs(outdir, exist_ok=True)
    video_files = []
    legend_files = []

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
                        legend_files.append(dst_leg)
                        if ext_low == ".docx":
                            lines = _extract_docx_legend_to_txt(dst_leg)
                            if lines:
                                txt_dst = os.path.splitext(dst_leg)[0] + ".txt"
                                with open(txt_dst, "w", encoding="utf-8") as tf:
                                    tf.write("\n".join(lines) + "\n")
                                for ln in lines:
                                    print(f"    Legend text ({base}): {ln}")
                        elif ext_low in (".txt", ".md"):
                            content = open(dst_leg, encoding="utf-8", errors="replace").read().strip()
                            if content:
                                print(f"    Legend text ({base}): {content[:300]}")
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
    Image, ImageDraw = None, None
    if ffmpeg:
        Image, ImageDraw, _, _ = _ensure_pil()

    print(f"=== VIDEO METADATA & STORYBOARD EXTRACTION ({len(video_files)} file(s)) ===")
    if not ffmpeg:
        print(
            "  !! [VIDEO AUDIT GAP — NO FFMPEG]: `ffmpeg` is not installed on this host.\n"
            "     Container dimensions/duration were inspected via pure-Python fallback, but visual\n"
            "     storyboard frame extraction was skipped. Document this limitation in the HTML report."
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
                        if not duration and st.get("duration"):
                            duration = float(st.get("duration", 0.0))
                    elif st.get("codec_type") == "audio":
                        has_audio = True
            except Exception as e:
                print(f"  [WARN] ffprobe failed on {vname}: {e}")

        if width == 0 and height == 0:
            width, height, duration, codec, has_audio = _probe_video_pure_python(vpath)

        print(f"\nVideo: {vname} ({fsize_mb:.2f} MB)")
        print(f"  Resolution : {width}x{height} px | Codec: {codec} | FPS: {fps_str} | Duration: {duration:.2f}s | Audio track: {'Yes' if has_audio else 'No'}")
        if has_audio:
            print("  !! NOTE: Video contains an audio track — verify whether unintentional microphone/room audio is present.")

        if not ffmpeg:
            continue

        fracs = [i / max(1, n_frames - 1) * 0.94 for i in range(n_frames)] if n_frames > 1 else [0.0]
        extracted_frames = []
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

        if extracted_frames:
            cols = min(3, len(extracted_frames))
            rows = (len(extracted_frames) + cols - 1) // cols
            cell_w, cell_h = 480, 340
            sheet = Image.new("RGB", (cols * cell_w, rows * cell_h + 36), (242, 244, 247))
            draw = ImageDraw.Draw(sheet)
            draw.text(
                (12, 10),
                f"{vname} — {width}x{height}px, {duration:.2f}s, codec={codec}, audio={'yes' if has_audio else 'no'}",
                fill=(15, 23, 42),
            )
            for idx, (ts, fp) in enumerate(extracted_frames):
                r, c = divmod(idx, cols)
                im = Image.open(fp).convert("RGB")
                im.thumbnail((cell_w - 20, cell_h - 36))
                x = c * cell_w + 10
                y = 36 + r * cell_h + 26
                sheet.paste(im, (x, y))
                draw.rectangle([x - 1, y - 1, x + im.width + 1, y + im.height + 1], outline=(30, 64, 175), width=2)
                draw.text((x, y - 18), f"Frame #{idx} @ t = {ts:.2f}s ({int(round(ts/duration*100)) if duration > 0 else 0}%)", fill=(30, 41, 59))
            sb_path = os.path.join(outdir, f"{stem}_storyboard.jpg")
            sheet.save(sb_path, quality=90)
            print(f"  Storyboard : {sb_path} ({len(extracted_frames)} frames sampled)")


def opt(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


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
        # Strip optional flags from positional args
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
        cmp_txt = opt('--compare-text', None)
        print_pdf_page_text_map(pdf, compare_txt_path=cmp_txt)
        return

    if cmd in ('find-text', 'search-text'):
        if len(sys.argv) < 4:
            sys.exit("Usage: render_figs.py find-text PDF_FILE QUERY [--units px --ref-dpi 200]")
        pdf, query = sys.argv[2], sys.argv[3]
        units = opt('--units', 'px')
        ref_dpi = float(opt('--ref-dpi', 200))
        find_text_in_pdf(pdf, query, units=units, ref_dpi=ref_dpi)
        return

    if cmd in ('video', 'videos'):
        if len(sys.argv) < 4:
            sys.exit("Usage: render_figs.py video VIDEO_OR_DIR OUTDIR [--frames 6]")
        target, outdir = sys.argv[2], sys.argv[3]
        n_frames = int(opt('--frames', 6))
        inspect_and_storyboard_videos(target, outdir, n_frames=n_frames)
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
        x0, y0, x1, y1 = map(float, sys.argv[4:8])
        dpi = int(opt('--dpi', 400))
        units = opt('--units', 'pt')
        ref_dpi = float(opt('--ref-dpi', 200))
        out = opt('--out', 'crop.jpg')

        sizes = get_pdf_page_sizes_pt(pdf)
        w_pt, h_pt = sizes[page_1idx - 1]
        im = render_page_to_pil(pdf, page_1idx, dpi)

        if units == 'pt':
            scale_x = im.width / w_pt
            scale_y = im.height / h_pt
        elif units == 'px':
            scale_x = dpi / ref_dpi
            scale_y = dpi / ref_dpi
        elif units == 'frac':
            scale_x = im.width
            scale_y = im.height
        else:
            sys.exit(f"Unknown --units '{units}' (use pt, px, or frac)")

        box = (
            max(0, int(round(x0 * scale_x))),
            max(0, int(round(y0 * scale_y))),
            min(im.width, int(round(x1 * scale_x))),
            min(im.height, int(round(y1 * scale_y))),
        )
        cropped = im.crop(box)
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        if out.lower().endswith(('.jpg', '.jpeg')):
            cropped.save(out, format="JPEG", quality=90, optimize=True)
        else:
            cropped.save(out)
        print(f"{out}: {cropped.width}x{cropped.height} px (cropped from page {page_1idx} @ {dpi} dpi, units={units})")
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
            im = Image.open(os.path.join(crops_dir, fname)).convert("RGB")
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
        im1 = Image.open(img1_path).convert("RGB")
        im2 = Image.open(img2_path).convert("RGB")
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

    sys.exit(__doc__)


if __name__ == '__main__':
    main()
