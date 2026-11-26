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

    selected_backend = (backend or getattr(cfg, "ocr_backend", "auto") or "auto").strip().lower()

    # PaddleOCR (RapidOCR ONNXRuntime or native paddleocr) is the primary
    # recommended engine; Tesseract is automatic fallback or explicit opt-in.
    use_paddle = (
        selected_backend in ("auto", "paddleocr")
        and ocr_paddle.available()
        and not getattr(cfg, "force_tesseract", False)
    )

    if use_paddle:
        try:
            paddle_lang = "en" if lang.startswith("eng") else lang
            text, conf, regions, ver = ocr_paddle.run_paddle_ocr_full(img, lang=paddle_lang)
            if text.strip() or selected_backend == "paddleocr":
                return text, conf, regions, "paddleocr", ver
            # In 'auto' mode, if PaddleOCR detects zero text (e.g. sparse 1-line PDF),
            # try Tesseract fallback before returning empty.
            try:
                t_text, t_conf, t_regions, t_ver = tesseract_ocr_full(img, cfg, lang=lang, psm=psm)
                if t_text.strip():
                    return t_text, t_conf, t_regions, "tesseract", t_ver
            except Exception:
                pass
            return text, conf, regions, "paddleocr", ver
        except (ocr_paddle.PaddleUnavailableError, Exception):
            # Fall back to Tesseract cleanly
            pass

    # Tesseract execution (fallback or explicit backend='tesseract')
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


_DOC_ORIENT_KEYWORDS = (
    "government", "aadhaar", "aadhar", "uidai", "income tax", "permanent account",
    "driving", "learner", "licence", "license", "secondary", "school", "board",
    "certificate", "temporary", "registration", "dob", "birth", "father", "address",
)


def _auto_orient_for_ocr(
    work_im: Image.Image,
    base_text: str,
    base_conf: float,
    cfg: FormFlowConfig,
    lang: str,
    psm: int,
    selected_backend: str,
) -> tuple[Image.Image, int, str | None, float | None, list[dict[str, Any]] | None, str | None, str | None]:
    """Check 90/270/180 rotations when 0-degree OCR is weak (sideways phone photos).

    Returns (best_im, best_rot_deg, text_or_None, conf, regions, eng, ver).
    """
    base_alnum = sum(1 for ch in base_text if ch.isalnum())
    base_low = base_text.lower()
    base_kws = sum(1 for k in _DOC_ORIENT_KEYWORDS if k in base_low)
    if base_alnum >= 45 and (base_conf >= 0.45 or base_kws >= 2):
        return work_im, 0, None, None, None, None, None

    # Probe quickly at capped resolution (~1100px max dim)
    probe_src = work_im
    if max(work_im.size) > 1100:
        scale = 1100.0 / max(work_im.size)
        probe_src = work_im.resize(
            (max(1, int(work_im.width * scale)), max(1, int(work_im.height * scale))),
            Image.LANCZOS,
        )

    best_deg = 0
    best_score = base_alnum + base_kws * 40 + (base_conf * 20.0)
    for deg in (90, 270, 180):
        rot_probe = probe_src.rotate(-deg, expand=True)
        cl, _ = preprocess_image_pipeline(rot_probe, binarize=False, upscale=False)
        try:
            rt, rc, _, _, _ = recognize_image(cl, cfg, lang, psm, None, backend="tesseract")
        except Exception:
            continue
        r_alnum = sum(1 for ch in rt if ch.isalnum())
        r_low = rt.lower()
        r_kws = sum(1 for k in _DOC_ORIENT_KEYWORDS if k in r_low)
        r_score = r_alnum + r_kws * 40 + (rc * 20.0)
        if r_score > best_score * 1.45 and (r_alnum >= 35 or r_kws >= 1):
            best_score = r_score
            best_deg = deg

    if best_deg == 0:
        return work_im, 0, None, None, None, None, None

    upright = work_im.rotate(-best_deg, expand=True)
    use_paddle_primary = (
        selected_backend in ("auto", "paddleocr")
        and ocr_paddle.available()
        and not getattr(cfg, "force_tesseract", False)
    )
    if use_paddle_primary:
        t_full, c_full, r_full, e_full, v_full = recognize_image(
            upright, cfg, lang, psm, None, backend=selected_backend
        )
    else:
        cl_full, _ = preprocess_image_pipeline(
            upright, binarize=False, upscale=(upright.height < 900)
        )
        t_full, c_full, r_full, e_full, v_full = recognize_image(
            cl_full, cfg, lang, psm, None, backend=selected_backend
        )
    return upright, best_deg, t_full, c_full, r_full, e_full, v_full


def _ocr_quality_score(text: str, conf: float, cfg: FormFlowConfig) -> tuple[int, float]:
    """Compare OCR outputs using extracted core field count + confidence."""
    try:
        from .fields import extract_candidates, load_field_rules

        rules = load_field_rules(cfg)
        cands = extract_candidates([{"page": 1, "text": text, "method": "ocr"}], rules)
        core = {
            c["field_key"]
            for c in cands
            if c["field_key"] in (
                "full_name", "father_name", "dob", "gender",
                "aadhaar_no", "pan_no", "licence_no", "vehicle_reg_no",
                "application_no", "address", "qualification",
            )
        }
        n_core = len(core)
    except Exception:
        n_core = 0
    alnum = min(600, sum(1 for ch in text if ch.isalnum()))
    score = n_core * 50.0 + conf * 35.0 + alnum * 0.05
    return n_core, score


def _extract_pdf_text(
    src: Path,
    cfg: FormFlowConfig,
    dpi: int,
    lang: str,
    min_page_chars: int,
    binarize: bool,
    psm: int,
    upscale: bool,
    upscale_min_height: int,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None,
    selected_backend: str,
) -> dict[str, Any]:
    embedded = pdf_page_texts(src)
    pages: list[dict[str, Any]] = []
    overall_engine = "embedded"

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
        "retry_path": None,
    }


def _retry_tesseract_opencv(
    ocr_source_im: Image.Image,
    otext: str,
    conf: float,
    regions: list[dict[str, Any]],
    eng: str,
    ver: str | None,
    pre_transforms: list[str],
    transforms: list[str],
    retry_path: str | None,
    cfg: FormFlowConfig,
    lang: str,
    psm: int,
    min_page_chars: int,
    upscale: bool,
    upscale_min_height: int,
    selected_backend: str,
):
    """Selective OpenCV gray/threshold retry when Tesseract produces weak output."""
    try:
        from .preprocess import file_opencv_block, opencv_provenance, prepare_variant_image

        if not opencv_provenance().get("available"):
            return otext, conf, regions, eng, ver, transforms, retry_path
        cv_rules = {
            "upscale": upscale,
            "upscale_min_height": upscale_min_height,
            "opencv": file_opencv_block(cfg),
        }
        gray_im, _ = prepare_variant_image(ocr_source_im, "opencv-gray", cv_rules)
        g_text, g_conf, g_regions, g_eng, g_ver = recognize_image(
            gray_im, cfg, lang, psm, None, backend=selected_backend
        )
        cur_alnum = sum(1 for ch in otext if ch.isalnum())
        g_alnum = sum(1 for ch in g_text if ch.isalnum())
        if (g_alnum > cur_alnum + 10) or (g_alnum >= cur_alnum and g_conf > conf + 0.08):
            otext, conf, regions, eng, ver = g_text, g_conf, g_regions, g_eng, g_ver
            transforms = pre_transforms + ["opencv-gray"]
            retry_path = "opencv-gray"
            cur_alnum = g_alnum
        if len(otext.strip()) < min_page_chars or conf < 0.45:
            thresh_im, _ = prepare_variant_image(ocr_source_im, "opencv-thresh", cv_rules)
            t_text, t_conf, t_regions, t_eng, t_ver = recognize_image(
                thresh_im, cfg, lang, psm, None, backend=selected_backend
            )
            t_alnum = sum(1 for ch in t_text if ch.isalnum())
            if t_alnum > cur_alnum + 15 and t_conf >= conf - 0.05:
                otext, conf, regions, eng, ver = t_text, t_conf, t_regions, t_eng, t_ver
                transforms = pre_transforms + ["opencv-thresh"]
                retry_path = "opencv-thresh"
    except Exception:
        pass
    return otext, conf, regions, eng, ver, transforms, retry_path


def _escalate_to_paddle(
    ocr_source_im: Image.Image,
    otext: str,
    conf: float,
    regions: list[dict[str, Any]],
    eng: str,
    ver: str | None,
    pre_transforms: list[str],
    transforms: list[str],
    retry_path: str | None,
    cfg: FormFlowConfig,
    lang: str,
):
    """Escalate tough Tesseract reads (e.g. colored SSC memos) to local PaddleOCR."""
    try:
        cur_alnum = sum(1 for ch in otext if ch.isalnum())
        cur_core, cur_score = _ocr_quality_score(otext, conf, cfg)
        needs_paddle = (
            (cur_alnum >= 25 and cur_core < 2)
            or (cur_alnum >= 25 and cur_core == 2 and conf < 0.45)
            or (10 <= cur_alnum < 60 and conf < 0.50 and cur_core < 2)
        )
        if needs_paddle:
            paddle_lang = "en" if lang.startswith("eng") else lang
            p_text, p_conf, p_regions, p_ver = ocr_paddle.run_paddle_ocr_full(
                ocr_source_im, lang=paddle_lang
            )
            p_core, p_score = _ocr_quality_score(p_text, p_conf, cfg)
            if p_core > cur_core or p_score > cur_score + 5.0:
                otext, conf, regions, eng, ver = p_text, p_conf, p_regions, "paddleocr", p_ver
                transforms = pre_transforms + ["paddleocr-hybrid"]
                retry_path = "paddleocr-hybrid"
    except Exception:
        pass
    return otext, conf, regions, eng, ver, transforms, retry_path


def _extract_image_text(
    src: Path,
    cfg: FormFlowConfig,
    lang: str,
    min_page_chars: int,
    binarize: bool,
    psm: int,
    upscale: bool,
    upscale_min_height: int,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None,
    selective_retry: bool,
    selected_backend: str,
    use_paddle_primary: bool,
) -> dict[str, Any]:
    with Image.open(src) as im:
        im.load()
        # Always start from EXIF-oriented untouched original (never the compressed/resized prepared copy).
        work_im = ImageOps.exif_transpose(im).copy()

    ocr_source_im = work_im
    card_cropped = False
    pre_transforms: list[str] = ["exif_transpose"]
    if ocr_fn is None:
        try:
            from .document_cv import crop_document_card

            cropped_im, crop_diag = crop_document_card(work_im)
            if crop_diag.get("crop_applied"):
                ocr_source_im = cropped_im
                card_cropped = True
                pre_transforms.append(f"card_crop:{crop_diag.get('method', 'auto')}")
        except Exception:
            pass

    if use_paddle_primary:
        paddle_im = ocr_source_im
        transforms = list(pre_transforms) + ["native_rgb"]
        # Small cropped hand-held cards (e.g. PAN/Aadhaar < 1100px) read dramatically
        # cleaner in RapidOCR after a gentle Lanczos upscale (capped at 2x).
        if max(paddle_im.size) < 1100 and min(paddle_im.size) >= 120:
            sc = min(2.0, 1400.0 / float(max(paddle_im.size)))
            if sc > 1.15:
                paddle_im = paddle_im.resize(
                    (max(1, int(round(paddle_im.width * sc))), max(1, int(round(paddle_im.height * sc)))),
                    Image.LANCZOS,
                )
                transforms.append(f"lanczos_upscale_{sc:.1f}x")
        otext, conf, regions, eng, ver = recognize_image(
            paddle_im, cfg, lang, psm, ocr_fn, backend=selected_backend
        )
        # If card-crop missed key fields (e.g. tight warp clipped a label), compare against uncropped original
        if card_cropped and selective_retry:
            try:
                cur_core, cur_score = _ocr_quality_score(otext, conf, cfg)
                if cur_core < 3:
                    raw_t, raw_c, raw_r, raw_e, raw_v = recognize_image(
                        work_im, cfg, lang, psm, ocr_fn, backend=selected_backend
                    )
                    raw_core, raw_score = _ocr_quality_score(raw_t, raw_c, cfg)
                    if raw_core > cur_core or (raw_core == cur_core and raw_score > cur_score + 8.0):
                        ocr_source_im = work_im
                        otext, conf, regions, eng, ver = raw_t, raw_c, raw_r, raw_e, raw_v
                        transforms = ["exif_transpose", "native_rgb_uncropped_fallback"]
            except Exception:
                pass
    else:
        should_upscale = bool(upscale) and (
            ocr_fn is not None or ocr_source_im.height < 900
        )
        cleaned, pipe_transforms = preprocess_image_pipeline(
            ocr_source_im, binarize, upscale=should_upscale,
            upscale_min_height=min(upscale_min_height, 900) if ocr_fn is None else upscale_min_height,
        )
        transforms = pre_transforms + [t for t in pipe_transforms if t != "exif_transpose"]
        otext, conf, regions, eng, ver = recognize_image(
            cleaned, cfg, lang, psm, ocr_fn, backend=selected_backend
        )

    retry_path = None
    detected_rotation = 0

    if ocr_fn is None:
        try:
            up_im, deg, ut, uc, ur, ue, uv = _auto_orient_for_ocr(
                ocr_source_im, otext, conf, cfg, lang, psm, selected_backend
            )
            if deg != 0 and ut is not None:
                ocr_source_im = up_im
                detected_rotation = deg
                otext, conf, regions, eng, ver = ut, float(uc or 0.0), ur or [], str(ue or eng), uv
                transforms = pre_transforms + [f"auto_rotate_{deg}"]
                retry_path = f"auto_rotate_{deg}"
        except Exception:
            pass

    if (
        selective_retry
        and ocr_fn is None
        and eng == "tesseract"
        and (len(otext.strip()) < min_page_chars or conf < 0.50)
    ):
        otext, conf, regions, eng, ver, transforms, retry_path = _retry_tesseract_opencv(
            ocr_source_im, otext, conf, regions, eng, ver,
            pre_transforms, transforms, retry_path,
            cfg, lang, psm, min_page_chars, upscale, upscale_min_height, selected_backend,
        )

    if (
        selective_retry
        and ocr_fn is None
        and eng != "paddleocr"
        and getattr(cfg, "ocr_hybrid_paddle", True)
        and not getattr(cfg, "force_tesseract", False)
        and ocr_paddle.available()
    ):
        otext, conf, regions, eng, ver, transforms, retry_path = _escalate_to_paddle(
            ocr_source_im, otext, conf, regions, eng, ver,
            pre_transforms, transforms, retry_path, cfg, lang,
        )

    return {
        "pages": [{
            "page": 1,
            "text": otext,
            "method": "ocr",
            "engine": eng,
            "model_version": ver,
            "preprocessing": transforms,
            "regions": regions,
            "confidence": conf,
            "detected_rotation": detected_rotation,
        }],
        "source": "ocr",
        "engine": eng,
        "retry_path": retry_path,
    }


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
    """Extract text for every page with structured regions, engine, and preprocessing provenance."""
    ext = src.suffix.lower()
    selected_backend = (backend or getattr(cfg, "ocr_backend", "auto") or "auto").strip().lower()
    use_paddle_primary = (
        ocr_fn is None
        and selected_backend in ("auto", "paddleocr")
        and ocr_paddle.available()
        and not getattr(cfg, "force_tesseract", False)
    )
    if ocr_fn is None and not use_paddle_primary:
        tesseract_exe(cfg)

    if ext == ".pdf":
        return _extract_pdf_text(
            src, cfg, dpi, lang, min_page_chars, binarize, psm,
            upscale, upscale_min_height, ocr_fn, selected_backend,
        )
    if ext in (".jpg", ".jpeg", ".png"):
        return _extract_image_text(
            src, cfg, lang, min_page_chars, binarize, psm,
            upscale, upscale_min_height, ocr_fn, selective_retry,
            selected_backend, use_paddle_primary,
        )
    raise OcrError(f"unsupported extension for text extraction: '{ext or '(none)'}'")
