"""Local text source: embedded PDF text first, Tesseract OCR as fallback.

No cloud OCR. The Tesseract executable path comes from configuration;
a missing binary is a recoverable setup error, never a crash.
Raw page text is returned to the caller for sidecar storage only --
never written to normal logs.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

from ..config import FormFlowConfig

_COMMON_WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


class OcrUnavailableError(Exception):
    """Tesseract is not installed/configured. Install it, then retry."""


class OcrError(Exception):
    """OCR was attempted but failed on this file (recoverable)."""


def tesseract_exe(cfg: FormFlowConfig) -> str:
    cmd = (cfg.tesseract_cmd or "tesseract").strip()
    p = Path(cmd)
    if p.is_absolute() or os.sep in cmd or "/" in cmd:
        if p.is_file():
            return str(p)
    else:
        found = shutil.which(cmd)
        if found:
            return found
    for cand in _COMMON_WINDOWS_PATHS:
        if Path(cand).is_file():
            return cand
    raise OcrUnavailableError(
        f"Tesseract executable not found (configured as '{cmd}'). "
        "Install Tesseract-OCR for Windows (https://tesseract-ocr.github.io/tessdoc/Installation.html), "
        "add it to PATH, or set FORMFLOW_TESSERACT_CMD / config.local.json "
        "'tesseract_cmd' to its full path (e.g. "
        r"'C:\\Program Files\\Tesseract-OCR\\tesseract.exe'). "
        "Text-based PDFs keep working without it; image OCR will retry after install."
    )


def pdf_page_texts(src: Path) -> list[tuple[int, str]]:
    """Embedded text per 1-based page number. Raises OcrError if unreadable."""
    import pymupdf

    try:
        pages: list[tuple[int, str]] = []
        with pymupdf.open(src) as doc:
            for i in range(doc.page_count):
                pages.append((i + 1, doc[i].get_text() or ""))
        return pages
    except Exception as e:
        raise OcrError(f"cannot read PDF text: {e}") from e


def render_pdf_page(src: Path, page_num: int, dpi: int) -> Image.Image:
    """Render one 1-based PDF page to an in-memory image (original untouched)."""
    import pymupdf

    try:
        with pymupdf.open(src) as doc:
            pix = doc[page_num - 1].get_pixmap(dpi=dpi)
        mode = "RGB" if pix.n < 4 else "RGBA"
        return Image.frombytes(mode, (pix.width, pix.height), pix.samples).copy()
    except Exception as e:
        raise OcrError(f"cannot render PDF page {page_num}: {e}") from e


def preprocess_image(img: Image.Image, binarize: bool = False) -> Image.Image:
    """Conservative cleanup, in memory only: orientation, gray, contrast."""
    work = ImageOps.exif_transpose(img)
    if work.mode != "L":
        work = work.convert("L")
    work = ImageOps.autocontrast(work, cutoff=1)
    if binarize:
        work = work.point(lambda v: 255 if v > 128 else 0, mode="L")
    return work


def ocr_image(
    img: Image.Image,
    cfg: FormFlowConfig,
    lang: str = "eng",
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
) -> tuple[str, float]:
    """Return (text, mean word confidence 0..1). Raises OcrUnavailableError/OcrError."""
    if ocr_fn is not None:
        return ocr_fn(img)
    exe = tesseract_exe(cfg)
    try:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = exe
        data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
        text = pytesseract.image_to_string(img, lang=lang) or ""
    except Exception as e:
        name = type(e).__name__
        if "TesseractNotFound" in name:
            raise OcrUnavailableError(
                f"Tesseract disappeared at '{exe}'. Reinstall or fix the path, then retry."
            ) from e
        raise OcrError(f"OCR failed: {e}") from e
    confs = [int(c) for c in data.get("conf", []) if int(c) >= 0]
    mean_conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    return text, mean_conf


def extract_document_text(
    src: Path,
    cfg: FormFlowConfig,
    dpi: int = 300,
    lang: str = "eng",
    min_page_chars: int = 30,
    binarize: bool = False,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
) -> dict:
    """Text for every page. Source per page: 'text' or 'ocr'.

    Images always need OCR (missing binary -> OcrUnavailableError).
    Returns {'pages': [{'page', 'text', 'method'}], 'source': 'text'|'ocr'|'mixed'}.
    """
    ext = src.suffix.lower()
    if ext == ".pdf":
        embedded = pdf_page_texts(src)
        pages: list[dict] = []
        for num, text in embedded:
            if len(text.strip()) >= min_page_chars:
                pages.append({"page": num, "text": text, "method": "text"})
            else:
                rendered = preprocess_image(render_pdf_page(src, num, dpi), binarize)
                otext, _ = ocr_image(rendered, cfg, lang, ocr_fn)
                pages.append({"page": num, "text": otext, "method": "ocr"})
        methods = {p["method"] for p in pages}
        source = "text" if methods == {"text"} else ("ocr" if methods == {"ocr"} else "mixed")
        return {"pages": pages, "source": source}
    if ext in (".jpg", ".jpeg", ".png"):
        with Image.open(src) as im:
            im.load()
            cleaned = preprocess_image(im.copy(), binarize)
        otext, _ = ocr_image(cleaned, cfg, lang, ocr_fn)
        return {"pages": [{"page": 1, "text": otext, "method": "ocr"}], "source": "ocr"}
    raise OcrError(f"unsupported extension for text extraction: '{ext or '(none)'}'")
