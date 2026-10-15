"""Phase 2 OpenCV preprocessing experiments (optional path).

The baseline path in ``app/services/ocr.py`` is untouched: this module
adds two independent, truthful preprocessing variants that run the
SAME Tesseract engine, so comparisons isolate preprocessing changes:

* ``opencv-gray`` -- EXIF orientation, page-quad detection with
  perspective rectification (only when confidence is adequate),
  deskew, illumination normalization + CLAHE. No thresholding.
* ``opencv-thresh`` -- everything ``opencv-gray`` does, plus adaptive
  Gaussian thresholding as the final step (an independent candidate,
  never a mandatory transformation).

Rules followed here:
* Original files are never modified: all work happens on in-memory
  copies (numpy arrays / PIL images). Callers pass an opened PIL
  image; nothing in this module writes to the source path.
* Every geometric step has a safe fallback. Uncertain page detection,
  suspicious rectification output, large deskew angles and collapsed
  illumination statistics all fall back to the pre-step image, with
  the reason recorded in the returned diagnostics.
* Diagnostics are redacted by construction: step names, applied
  booleans, reasons and small numbers only. No pixels, no OCR text,
  no applicant values.
* ``cv2``/``numpy`` are lazy imports. When they are missing, variant
  entry points raise :class:`PreprocessUnavailableError` (a distinct
  failure kind in benchmark reports) while the baseline keeps working.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

VARIANTS = ("opencv-gray", "opencv-thresh")


class PreprocessUnavailableError(Exception):
    """OpenCV/numpy is not installed. Install it, then retry the variant."""


def _require_cv2():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as e:
        raise PreprocessUnavailableError(
            "OpenCV (cv2) and numpy are required for the 'opencv-*' "
            "preprocessing variants but could not be imported. Install with "
            '\".venv\\Scripts\\python -m pip install -r requirements.txt\" '
            "(opencv-python-headless + numpy) and retry. The 'baseline' "
            "variant keeps working without them."
        ) from e
    return cv2, np


_DEFAULT_OPENCV_CFG: dict[str, Any] = {
    "canny_low": 50,
    "canny_high": 150,
    "min_area_ratio": 0.15,
    "max_area_ratio": 0.98,
    "epsilon_ratio": 0.02,
    "min_confidence": 0.6,
    "deskew_max_angle_deg": 15.0,
    "deskew_min_text_frac": 0.005,
    "deskew_max_text_frac": 0.5,
    "clahe_clip_limit": 2.0,
    "adaptive_block_size": 51,
    "adaptive_c": 9,
    "rectify_min_dim": 200,
    "rectify_min_area_ratio": 0.10,
}


def opencv_provenance() -> dict[str, Any]:
    """Availability snapshot for reports. No image data."""
    try:
        cv2, np = _require_cv2()
        return {"available": True, "cv2_version": cv2.__version__,
                "numpy_version": np.__version__, "error": None}
    except PreprocessUnavailableError as e:
        return {"available": False, "cv2_version": None,
                "numpy_version": None, "error": str(e)[:200]}


def file_opencv_block(cfg) -> dict[str, Any]:
    """Read the ``opencv`` tuning block straight from ocr_rules.json.

    ``fields.load_field_rules`` deliberately ignores this block (the
    baseline must be unaffected), so it is loaded here instead. Any
    read/parse problem falls back to code defaults.
    """
    try:
        import json

        raw = json.loads(Path(cfg.ocr_rules_path).read_text(encoding="utf-8"))
        block = raw.get("opencv", {})
        return dict(block) if isinstance(block, dict) else {}
    except Exception:
        return {}


def opencv_cfg(rules: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(_DEFAULT_OPENCV_CFG)
    raw = rules.get("opencv", {})
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in cfg and v is not None:
                cfg[k] = v
    bs = int(cfg["adaptive_block_size"])
    if bs < 3:
        bs = 3
    if bs % 2 == 0:
        bs += 1
    cfg["adaptive_block_size"] = bs
    return cfg


def pil_to_gray_np(img: Image.Image, np) -> Any:
    work = ImageOps.exif_transpose(img)
    if work.mode != "L":
        work = work.convert("L")
    import numpy as _np  # same object as np; keeps type checkers calm

    return _np.array(work)


def _order_points(pts, np) -> Any:
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def detect_page_quad(gray, cfg: dict[str, Any]) -> tuple[Any | None, dict[str, Any]]:
    """Find the page quadrilateral. Returns (quad_4x2_or_None, diagnostics).

    Confidence combines quad area share, contour-vs-quad rectangularity
    and corner squareness. Nothing here proves document identity; it is
    a geometric observation used only to gate rectification.
    """
    cv2, np = _require_cv2()
    h, w = gray.shape[:2]
    diag: dict[str, Any] = {"op": "page-detect", "attempted": True,
                            "found": False, "confidence": 0.0, "reason": ""}
    try:
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, int(cfg["canny_low"]), int(cfg["canny_high"]))
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:10]
        diag["contours_considered"] = len(contours)
        img_area = float(h * w)
        best_ratio = 0.0
        for c in contours:
            area = float(cv2.contourArea(c))
            ratio = area / img_area
            best_ratio = max(best_ratio, round(ratio, 4))
            if not (cfg["min_area_ratio"] <= ratio <= cfg["max_area_ratio"]):
                continue
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, float(cfg["epsilon_ratio"]) * peri, True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue
            quad = _order_points(approx.reshape(4, 2).astype("float32"), np)
            quad_area = max(1.0, float(cv2.contourArea(quad)))
            rect_score = min(area, quad_area) / max(area, quad_area)
            devs = []
            for i in range(4):
                p0 = quad[i]
                p1 = quad[(i + 1) % 4]
                p2 = quad[(i + 3) % 4]
                v1 = p1 - p0
                v2 = p2 - p0
                cos_a = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-9))
                ang = math.degrees(math.acos(max(-1.0, min(1.0, cos_a))))
                devs.append(abs(ang - 90.0))
            angle_score = max(0.0, 1.0 - (sum(devs) / len(devs)) / 30.0)
            area_score = max(0.0, min(1.0, (ratio - cfg["min_area_ratio"])
                                          / max(1e-6, 0.60 - cfg["min_area_ratio"])))
            conf = 0.40 * area_score + 0.35 * rect_score + 0.25 * angle_score
            if conf >= float(cfg["min_confidence"]):
                diag.update(found=True, confidence=round(conf, 3),
                            reason="quad-accepted",
                            area_ratio=round(ratio, 4))
                return quad, diag
        diag["best_area_ratio"] = best_ratio
        diag["reason"] = "no-confident-quad"
        return None, diag
    except Exception as e:
        diag.update(found=False, confidence=0.0,
                    reason=f"error:{type(e).__name__}")
        return None, diag


def rectify_perspective(gray, quad, cfg: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Warp the quad to a frontal rectangle, with sanity guards."""
    cv2, np = _require_cv2()
    h, w = gray.shape[:2]
    diag: dict[str, Any] = {"op": "perspective", "attempted": True,
                            "applied": False, "reason": ""}
    try:
        (tl, tr, br, bl) = quad
        width_a = float(np.linalg.norm(br - bl))
        width_b = float(np.linalg.norm(tr - tl))
        height_a = float(np.linalg.norm(tr - br))
        height_b = float(np.linalg.norm(tl - bl))
        dst_w = max(1, int(round(max(width_a, width_b))))
        dst_h = max(1, int(round(max(height_a, height_b))))
        diag["dst_size"] = [dst_w, dst_h]
        if min(dst_w, dst_h) < int(cfg["rectify_min_dim"]):
            diag["reason"] = "fallback:rect-too-small"
            return gray, diag
        if (dst_w * dst_h) / float(h * w) < float(cfg["rectify_min_area_ratio"]):
            diag["reason"] = "fallback:rect-area-suspicious"
            return gray, diag
        dst = np.array([[0, 0], [dst_w - 1, 0], [dst_w - 1, dst_h - 1],
                        [0, dst_h - 1]], dtype="float32")
        mat = cv2.getPerspectiveTransform(quad.astype("float32"), dst)
        warped = cv2.warpPerspective(gray, mat, (dst_w, dst_h),
                                     flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_REPLICATE)
        diag.update(applied=True, reason="rectified")
        return warped, diag
    except Exception as e:
        diag.update(applied=False, reason=f"error:{type(e).__name__}")
        return gray, diag


def deskew(gray, cfg: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Rotate small skews back to horizontal. Large angles are skipped.

    The angle comes from a median of near-horizontal Hough segments
    (text baselines), estimated on the central region so photo
    surrounds and page borders do not dominate. Translation of the
    estimation window does not affect the angle.
    """
    cv2, np = _require_cv2()
    diag: dict[str, Any] = {"op": "deskew", "attempted": True,
                            "applied": False, "reason": ""}
    try:
        import math as _math

        h, w = gray.shape[:2]
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        edges = cv2.Canny(blur, 50, 150)
        mh, mw = int(h * 0.10), int(w * 0.10)
        core = edges[mh:h - mh, mw:w - mw]
        lines = cv2.HoughLinesP(core, 1, _math.pi / 180, 80,
                                minLineLength=max(40, int(w * 0.15)),
                                maxLineGap=15)
        # cv2 4.x returns (N,1,4); 5.x returns (N,4): normalize.
        segs = np.asarray(lines).reshape(-1, 4) if lines is not None else None
        if segs is None or len(segs) < 6:
            diag["reason"] = "fallback:too-few-line-segments"
            diag["segments"] = 0 if segs is None else len(segs)
            return gray, diag
        angles = []
        for x1, y1, x2, y2 in segs:
            if x2 == x1:
                continue
            a = _math.degrees(_math.atan2(y2 - y1, x2 - x1))
            if abs(a) <= 45.0:
                angles.append(a)
        diag["segments"] = len(angles)
        if len(angles) < 6:
            diag["reason"] = "fallback:too-few-horizontal-segments"
            return gray, diag
        angles.sort()
        med = angles[len(angles) // 2]
        diag["detected_angle"] = round(med, 2)
        if abs(med) < 0.25:
            diag["reason"] = "skipped:already-horizontal"
            return gray, diag
        if abs(med) > float(cfg["deskew_max_angle_deg"]):
            diag["reason"] = "fallback:angle-too-large"
            return gray, diag
        mat = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), med, 1.0)
        fixed = cv2.warpAffine(gray, mat, (w, h),
                               flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)
        diag.update(applied=True, reason="deskewed")
        return fixed, diag
    except Exception as e:
        diag.update(applied=False, reason=f"error:{type(e).__name__}")
        return gray, diag


def normalize_illumination(gray, cfg: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Divide by a blurred background estimate, then mild CLAHE."""
    cv2, np = _require_cv2()
    diag: dict[str, Any] = {"op": "illumination", "attempted": True,
                            "applied": False, "reason": ""}
    try:
        h, w = gray.shape[:2]
        sigma = max(15.0, min(h, w) / 30.0)
        bg = cv2.GaussianBlur(gray, (0, 0), sigma)
        bg = np.maximum(bg, 30)
        flat = cv2.divide(gray, bg, scale=255)
        clahe = cv2.createCLAHE(clipLimit=float(cfg["clahe_clip_limit"]),
                                tileGridSize=(8, 8))
        out = clahe.apply(flat)
        mean, std = float(out.mean()), float(out.std())
        diag["mean"] = round(mean, 1)
        diag["std"] = round(std, 1)
        if not (40.0 <= mean <= 215.0) or std < 8.0:
            diag["reason"] = "fallback:stats-suspicious"
            return gray, diag
        diag.update(applied=True, reason="normalized")
        return out, diag
    except Exception as e:
        diag.update(applied=False, reason=f"error:{type(e).__name__}")
        return gray, diag


def adaptive_threshold_image(gray, cfg: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    cv2, np = _require_cv2()
    diag: dict[str, Any] = {"op": "adaptive-threshold", "attempted": True,
                            "applied": False, "reason": ""}
    try:
        out = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, int(cfg["adaptive_block_size"]),
            int(cfg["adaptive_c"]))
        diag.update(applied=True, reason="thresholded")
        return out, diag
    except Exception as e:
        diag.update(applied=False, reason=f"error:{type(e).__name__}")
        return gray, diag


def prepare_variant_image(
    img: Image.Image,
    variant: str,
    rules: dict[str, Any],
) -> tuple[Image.Image, dict[str, Any]]:
    """Run one OpenCV variant on an in-memory copy. Never touches disk."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown OpenCV variant '{variant}'. "
                         f"Supported: {list(VARIANTS)}.")
    cv2, np = _require_cv2()
    cfg = opencv_cfg(rules)
    steps: list[dict[str, Any]] = []
    cur = pil_to_gray_np(img, np)
    quad, qd = detect_page_quad(cur, cfg)
    steps.append({k: qd[k] for k in
                  ("op", "attempted", "found", "confidence", "reason")
                  if k in qd})
    if quad is not None and qd.get("found"):
        cur, rd = rectify_perspective(cur, quad, cfg)
        steps.append({k: rd[k] for k in
                      ("op", "attempted", "applied", "reason") if k in rd})
    cur, dd = deskew(cur, cfg)
    steps.append({k: dd[k] for k in
                  ("op", "attempted", "applied", "reason") if k in dd})
    # Scale treatment matches the baseline rule (2x when short), applied
    # BEFORE the photometric steps so threshold output stays binary
    # (upscaling a binary image would reintroduce gray edge pixels).
    upscale = bool(rules.get("upscale", False))
    min_h = int(rules.get("upscale_min_height", 1500))
    pil_mid = Image.fromarray(cur.astype("uint8"), mode="L")
    if upscale and pil_mid.height < min_h:
        pil_mid = pil_mid.resize((pil_mid.width * 2, pil_mid.height * 2),
                                 Image.LANCZOS)
        steps.append({"op": "upscale", "attempted": True,
                      "applied": True, "reason": "small-input-2x"})
        cur = np.array(pil_mid)
    else:
        steps.append({"op": "upscale", "attempted": True,
                      "applied": False, "reason": "tall-enough"})
    cur, idg = normalize_illumination(cur, cfg)
    steps.append({k: idg[k] for k in
                  ("op", "attempted", "applied", "reason") if k in idg})
    if variant == "opencv-thresh":
        cur, td = adaptive_threshold_image(cur, cfg)
        steps.append({k: td[k] for k in
                      ("op", "attempted", "applied", "reason") if k in td})
    out = Image.fromarray(cur.astype("uint8"), mode="L")
    return out, {"variant": variant, "steps": steps}


def extract_document_text_with_variant(
    src: Path,
    cfg,
    variant: str,
    dpi: int = 300,
    lang: str = "eng",
    min_page_chars: int = 30,
    psm: int = 6,
    upscale: bool = False,
    upscale_min_height: int = 1500,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
) -> dict:
    """Same contract as ``ocr.extract_document_text`` for one variant.

    Text PDFs keep the embedded-text path (no preprocessing, no OCR);
    scanned pages and images go through the real OpenCV variant, then
    the same Tesseract engine. Raises OcrUnavailableError/OcrError like
    the baseline, plus PreprocessUnavailableError when cv2 is missing.
    ``src`` is only read. Returns {"pages", "source", "variant",
    "preprocess_per_page"} where each per-page entry holds redacted
    step diagnostics (or {"variant_applied": False} for text pages).
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown OpenCV variant '{variant}'.")
    _require_cv2()
    from .ocr import OcrError, ocr_image, pdf_page_texts, render_pdf_page

    # File-backed rules supply the opencv tuning block (loaded
    # directly because fields.load_field_rules ignores it, keeping the
    # baseline unaffected); the caller-held upscale args decide the 2x
    # rule so scale treatment matches the baseline call.
    void_rules = {"upscale": upscale, "upscale_min_height": upscale_min_height,
                  "opencv": file_opencv_block(cfg)}

    ext = src.suffix.lower()
    per_page_diag: list[dict] = []

    def _clean(im: Image.Image) -> Image.Image:
        cleaned, diag = prepare_variant_image(im, variant, void_rules)
        per_page_diag.append(diag)
        return cleaned

    if ext == ".pdf":
        embedded = pdf_page_texts(src)
        pages: list[dict] = []
        for num, text in embedded:
            if len(text.strip()) >= min_page_chars:
                pages.append({"page": num, "text": text, "method": "text"})
                per_page_diag.append({"variant": variant, "variant_applied": False,
                                      "reason": "embedded-text-used"})
            else:
                rendered = _clean(render_pdf_page(src, num, dpi))
                otext, _ = ocr_image(rendered, cfg, lang, psm, ocr_fn)
                pages.append({"page": num, "text": otext, "method": "ocr"})
        methods = {p["method"] for p in pages}
        source = "text" if methods == {"text"} else ("ocr" if methods == {"ocr"} else "mixed")
        return {"pages": pages, "source": source, "variant": variant,
                "preprocess_per_page": per_page_diag}
    if ext in (".jpg", ".jpeg", ".png"):
        with Image.open(src) as im:
            im.load()
            cleaned = _clean(im.copy())
        otext, _ = ocr_image(cleaned, cfg, lang, psm, ocr_fn)
        return {"pages": [{"page": 1, "text": otext, "method": "ocr"}],
                "source": "ocr", "variant": variant,
                "preprocess_per_page": per_page_diag}
    raise OcrError(f"unsupported extension for text extraction: '{ext or '(none)'}'")


def write_preprocess_images(
    out_dir: Path,
    seed: str,
    images: dict[str, Image.Image],
) -> list[Path]:
    """Save opt-in variant preview PNGs under an ignored out dir.

    ``images`` maps variant label -> PIL image. File names use only
    the opaque ``seed`` hash and known variant labels. Same-second
    collisions get incrementing suffixes; nothing is overwritten.
    """
    from ..config import REPO_ROOT

    p = Path(out_dir)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    else:
        p = p.resolve()
    # Enforce ignored-dir rule: inside repo only under data/.
    try:
        p.relative_to(REPO_ROOT.resolve())
        try:
            p.relative_to((REPO_ROOT / "data").resolve())
        except ValueError:
            raise ValueError(
                f"refusing to write previews inside repo but outside "
                f"gitignored data/: {p}.")
    except ValueError:
        pass  # outside repo (e.g. %TEMP%): allowed
    p.mkdir(parents=True, exist_ok=True)
    clean_seed = "".join(ch for ch in (seed or "") if ch.isalnum())[:16] or "case"
    written: list[Path] = []
    for variant, im in images.items():
        clean_var = "".join(ch for ch in variant if ch.isalnum() or ch in ("-", "_"))[:24]
        dest = p / f"preprocess-{clean_var}-{clean_seed}.png"
        n = 1
        while dest.exists():
            n += 1
            dest = p / f"preprocess-{clean_var}-{clean_seed}-{n}.png"
            if n > 1000:
                raise OSError("preprocess preview namespace exhausted")
        im.save(dest, "PNG")
        written.append(dest)
    return written
