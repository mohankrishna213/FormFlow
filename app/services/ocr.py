"""Local text source: embedded PDF text first, PaddleOCR/Tesseract OCR as fallback.

No cloud OCR. Local execution only.
Supports:
- Structured regions with geometry and confidence
- Lazy-loaded, reusable engine abstractions (PaddleOCR primary, Tesseract fallback)
- Selective preprocessing pipeline: conservative baseline with selective retry on poor images
- Full provenance tracking (engine, model version, preprocessing steps)
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

from ..config import FormFlowConfig
from . import ocr_paddle

_COMMON_WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


class OcrUnavailableError(Exception):
    """OCR engine is not installed/configured. Install it, then retry."""


class OcrError(Exception):
    """OCR was attempted but failed on this file (recoverable)."""


@dataclass
class OcrRegion:
    text: str
    bbox: tuple[int, int, int, int] | None  # (x0, y0, x1, y1) in pixel coords
    confidence: float  # 0.0 to 1.0
    reading_order: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bbox": list(self.bbox) if self.bbox else None,
            "confidence": round(self.confidence, 4),
            "reading_order": self.reading_order,
        }


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


def preprocess_image_pipeline(
    img: Image.Image,
    binarize: bool = False,
    upscale: bool = False,
    upscale_min_height: int = 1500,
) -> tuple[Image.Image, list[str]]:
    """Conservative cleanup, in memory only: orientation, gray, contrast.

    Returns (processed_image, list_of_applied_steps).
    Small phone photos get 2x Lanczos upscaling first -- reads
    small print far better at ~300 DPI equivalent. Original file untouched.
    """
    transforms = ["exif_transpose"]
    work = ImageOps.exif_transpose(img)
    if upscale and work.height < upscale_min_height:
        work = work.resize((work.width * 2, work.height * 2), Image.LANCZOS)
        transforms.append("lanczos_upscale_2x")
    if work.mode != "L":
        work = work.convert("L")
        transforms.append("convert_gray")
    work = ImageOps.autocontrast(work, cutoff=1)
    transforms.append("autocontrast_cutoff_1")
    if binarize:
        work = work.point(lambda v: 255 if v > 128 else 0, mode="L")
        transforms.append("binarize_128")
    return work, transforms


def preprocess_image(
    img: Image.Image,
    binarize: bool = False,
    upscale: bool = False,
    upscale_min_height: int = 1500,
) -> Image.Image:
    """Backwards-compatible wrapper returning the processed image."""
    work, _ = preprocess_image_pipeline(img, binarize, upscale, upscale_min_height)
    return work


def tesseract_ocr_full(
    img: Image.Image,
    cfg: FormFlowConfig,
    lang: str = "eng",
    psm: int = 6,
) -> tuple[str, float, list[dict[str, Any]], str | None]:
    """Execute Tesseract and extract text, mean confidence, and region geometry."""
    exe = tesseract_exe(cfg)
    try:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = exe
        config = f"--psm {int(psm)}"
        data = pytesseract.image_to_data(
            img, lang=lang, config=config, output_type=pytesseract.Output.DICT
        )
        text = pytesseract.image_to_string(img, lang=lang, config=config) or ""
    except Exception as e:
        name = type(e).__name__
        if "TesseractNotFound" in name:
            raise OcrUnavailableError(
                f"Tesseract disappeared at '{exe}'. Reinstall or fix the path, then retry."
            ) from e
        raise OcrError(f"OCR failed: {e}") from e

    regions: list[dict[str, Any]] = []
    confs: list[int] = []
    n_boxes = len(data.get("text", []))
    reading_idx = 0
    for i in range(n_boxes):
        w_text = (data["text"][i] or "").strip()
        conf_val = int(data["conf"][i]) if i < len(data.get("conf", [])) else -1
        if conf_val >= 0:
            confs.append(conf_val)
        if w_text:
            x = int(data["left"][i])
            y = int(data["top"][i])
            w = int(data["width"][i])
            h = int(data["height"][i])
            regions.append({
                "text": w_text,
                "bbox": [x, y, x + w, y + h],
                "confidence": max(0.0, min(1.0, float(conf_val) / 100.0)) if conf_val >= 0 else 0.5,
                "reading_order": reading_idx,
            })
            reading_idx += 1
    mean_conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    try:
        ver = str(pytesseract.get_tesseract_version())
    except Exception:
        ver = None
    return text, mean_conf, regions, ver


def recognize_image(
    img: Image.Image,
    cfg: FormFlowConfig,
    lang: str = "eng",
    psm: int = 6,
    ocr_fn: Callable[[Image.Image], Any] | None = None,
    backend: str | None = None,
) -> tuple[str, float, list[dict[str, Any]], str, str | None]:
    """Execute OCR returning (text, mean_confidence, regions, engine_name, model_version).

    Engine selection order:
    1. ocr_fn (if explicitly provided, e.g. for testing)
    2. PaddleOCR (if available and not forced to tesseract)
    3. Tesseract (standard robust fallback)
    """
    if ocr_fn is not None:
        res = ocr_fn(img)
        if isinstance(res, tuple):
            if len(res) == 2:
                text, conf = res
                return str(text), float(conf), [], "mock", None
            if len(res) >= 3:
                text, conf, regions = res[0], res[1], res[2]
                engine = res[3] if len(res) > 3 else "mock"
                ver = res[4] if len(res) > 4 else None
                return str(text), float(conf), list(regions), str(engine), ver
        return str(res), 0.5, [], "mock", None

    selected_backend = backend or getattr(cfg, "ocr_backend", "tesseract")

    # Check if PaddleOCR should be used (opt-in engine due to Windows CPU runner stability)
    use_paddle = False
    if selected_backend == "paddleocr" and ocr_paddle.available() and not getattr(cfg, "force_tesseract", False):
        use_paddle = True

    if use_paddle:
        try:
            paddle_lang = "en" if lang.startswith("eng") else lang
            text, conf, regions, ver = ocr_paddle.run_paddle_ocr_full(img, lang=paddle_lang)
            return text, conf, regions, "paddleocr", ver
        except (ocr_paddle.PaddleUnavailableError, Exception):
            # Fall back to Tesseract cleanly
            pass

    # Tesseract execution
    text, conf, regions, ver = tesseract_ocr_full(img, cfg, lang=lang, psm=psm)
    return text, conf, regions, "tesseract", ver


def ocr_image(
    img: Image.Image,
    cfg: FormFlowConfig,
    lang: str = "eng",
    psm: int = 6,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
) -> tuple[str, float]:
    """Backwards-compatible Tesseract wrapper returning (text, mean_confidence)."""
    if ocr_fn is not None:
        return ocr_fn(img)
    text, conf, _, _ = tesseract_ocr_full(img, cfg, lang=lang, psm=psm)
    return text, conf


def extract_document_text(
    src: Path,
    cfg: FormFlowConfig,
    dpi: int = 300,
    lang: str = "eng",
    min_page_chars: int = 30,
    binarize: bool = False,
    psm: int = 6,
    upscale: bool = False,
    upscale_min_height: int = 1500,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
    selective_retry: bool = True,
    backend: str | None = None,
) -> dict[str, Any]:
    """Extract text for every page with structured regions, engine, and preprocessing provenance.

    Returns:
    {
        'pages': [
            {
                'page': int,
                'text': str,
                'method': 'text' | 'ocr',
                'engine': 'embedded' | 'tesseract' | 'paddleocr' | 'mock',
                'model_version': str | None,
                'preprocessing': list[str],
                'regions': list[dict],
                'confidence': float,
            }
        ],
        'source': 'text' | 'ocr' | 'mixed',
        'engine': str,
        'retry_path': str | None,
    }
    """
    ext = src.suffix.lower()

    # Validate Tesseract setup if Tesseract is selected or as baseline fallback validator
    selected_backend = backend or getattr(cfg, "ocr_backend", "auto")
    if ocr_fn is None and (selected_backend == "tesseract" or not ocr_paddle.available()):
        tesseract_exe(cfg)
    elif ocr_fn is None and selected_backend != "paddleocr":
        # Always validate that fallback Tesseract exists
        tesseract_exe(cfg)

    if ext == ".pdf":
        embedded = pdf_page_texts(src)
        pages: list[dict[str, Any]] = []
        overall_engine = "embedded"
        retry_used = None

        for num, text in embedded:
            if len(text.strip()) >= min_page_chars:
                pages.append({
                    "page": num,
                    "text": text,
                    "method": "text",
                    "engine": "embedded",
                    "model_version": None,
                    "preprocessing": [],
                    "regions": [],
                    "confidence": 1.0,
                })
            else:
                rendered = render_pdf_page(src, num, dpi)
                cleaned, transforms = preprocess_image_pipeline(
                    rendered, binarize, upscale, upscale_min_height
                )
                otext, conf, regions, eng, ver = recognize_image(
                    cleaned, cfg, lang, psm, ocr_fn, backend=selected_backend
                )
                pages.append({
                    "page": num,
                    "text": otext,
                    "method": "ocr",
                    "engine": eng,
                    "model_version": ver,
                    "preprocessing": transforms,
                    "regions": regions,
                    "confidence": conf,
                })
                overall_engine = eng

        methods = {p["method"] for p in pages}
        source = "text" if methods == {"text"} else ("ocr" if methods == {"ocr"} else "mixed")
        return {
            "pages": pages,
            "source": source,
            "engine": overall_engine,
            "retry_path": retry_used,
        }

    if ext in (".jpg", ".jpeg", ".png"):
        with Image.open(src) as im:
            im.load()
            work_im = im.copy()

        cleaned, transforms = preprocess_image_pipeline(
            work_im, binarize, upscale, upscale_min_height
        )
        otext, conf, regions, eng, ver = recognize_image(
            cleaned, cfg, lang, psm, ocr_fn, backend=selected_backend
        )
        retry_path = None

        # Selective retry for poor OCR results (only for live OCR)
        if selective_retry and ocr_fn is None and (len(otext.strip()) < min_page_chars or conf < 0.40):
            try:
                from .preprocess import opencv_provenance, preprocess_variant
                if opencv_provenance().get("available"):
                    gray_im, _ = preprocess_variant(work_im, "opencv-gray")
                    g_text, g_conf, g_regions, g_eng, g_ver = recognize_image(
                        gray_im, cfg, lang, psm, ocr_fn, backend=selected_backend
                    )
                    if len(g_text.strip()) > len(otext.strip()) or g_conf > conf + 0.1:
                        otext, conf, regions, eng, ver = g_text, g_conf, g_regions, g_eng, g_ver
                        transforms = ["opencv-gray"]
                        retry_path = "opencv-gray"
                        # If still poor, try adaptive thresholding
                        if len(otext.strip()) < min_page_chars or conf < 0.40:
                            thresh_im, _ = preprocess_variant(work_im, "opencv-thresh")
                            t_text, t_conf, t_regions, t_eng, t_ver = recognize_image(
                                thresh_im, cfg, lang, psm, ocr_fn, backend=selected_backend
                            )
                            if len(t_text.strip()) > len(otext.strip()):
                                otext, conf, regions, eng, ver = t_text, t_conf, t_regions, t_eng, t_ver
                                transforms = ["opencv-thresh"]
                                retry_path = "opencv-thresh"
            except Exception:
                pass

        page_result = {
            "page": 1,
            "text": otext,
            "method": "ocr",
            "engine": eng,
            "model_version": ver,
            "preprocessing": transforms,
            "regions": regions,
            "confidence": conf,
        }
        return {
            "pages": [page_result],
            "source": "ocr",
            "engine": eng,
            "retry_path": retry_path,
        }

    raise OcrError(f"unsupported extension for text extraction: '{ext or '(none)'}'")
