"""Local PaddleOCR / RapidOCR engine adapter (primary CPU OCR backend).

Design rules:
  * Prefers ``rapidocr`` (official PaddleOCR PP-OCRv5/v6 models on ONNXRuntime
    CPU) for fast, high-accuracy local recognition on Windows; falls back to
    ``paddleocr`` / ``paddlepaddle`` when installed, or raises
    :class:`PaddleUnavailableError` so :mod:`app.services.ocr` falls back to
    Tesseract transparently.
  * Heavy ML/numpy imports happen lazily on first use so startup stays instant
    and environments without RapidOCR/PaddleOCR keep working seamlessly.
  * Output is normalized to ``(text, mean_confidence_0_to_1)`` with visual
    line regrouping (:func:`_group_entries_into_lines`, including 2-column
    splitting for full e-Aadhaar letters) so downstream label/value parsers
    receive clean reading-order lines.
  * Strictly offline after initial local model cache download; zero cloud APIs.
"""
from __future__ import annotations

import importlib.util
import platform
import sys
from typing import Any, Callable


class PaddleUnavailableError(Exception):
    """PaddleOCR is not installed/importable. Install it, then retry."""


_ENGINE: Any = None  # cached native paddleocr engine instance (per process)
_RAPID_ENGINE: Any = None  # cached RapidOCR (PaddleOCR ONNXRuntime) engine
_RAPID_BACKEND_KIND: str | None = None  # 'rapidocr' (v3) or 'rapidocr_onnxruntime'


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return None


def _rapid_available() -> bool:
    """True when RapidOCR (PaddleOCR models via ONNXRuntime) is importable."""
    for pkg in ("rapidocr", "rapidocr_onnxruntime"):
        try:
            if importlib.util.find_spec(pkg) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


def _native_paddle_available() -> bool:
    """True when the native paddleocr package is importable."""
    try:
        return importlib.util.find_spec("paddleocr") is not None
    except (ImportError, ValueError):
        return False


def available() -> bool:
    """True when either RapidOCR (PaddleOCR ONNX) or native paddleocr is importable."""
    return _rapid_available() or _native_paddle_available()


def paddle_provenance() -> dict[str, Any]:
    """Availability snapshot for reports. No image data, no model files."""
    ok = available()
    rapid_ver = _package_version("rapidocr") or _package_version("rapidocr-onnxruntime")
    return {
        "available": ok,
        "rapidocr_version": rapid_ver if _rapid_available() else None,
        "paddleocr_version": _package_version("paddleocr") if _native_paddle_available() else None,
        "paddlepaddle_version": _package_version("paddlepaddle") if _native_paddle_available() else None,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "error": None if ok else (
            "PaddleOCR / RapidOCR not importable. Install rapidocr + onnxruntime "
            "or paddleocr in .venv311; Tesseract remains available as fallback."),
    }


def _reset_engine_cache() -> None:
    """Test helper: drop cached engine instances."""
    global _ENGINE, _RAPID_ENGINE, _RAPID_BACKEND_KIND
    _ENGINE = None
    _RAPID_ENGINE = None
    _RAPID_BACKEND_KIND = None


def _rapid_engine_for(lang: str = "en"):
    """Lazy-load RapidOCR running official English PP-OCRv5/v4 models on ONNXRuntime CPU."""
    global _RAPID_ENGINE, _RAPID_BACKEND_KIND
    if _RAPID_ENGINE is not None:
        return _RAPID_ENGINE, _RAPID_BACKEND_KIND

    # Prefer modern rapidocr v3 (supports official en_PP-OCRv5_rec_mobile + PP-OCRv6_det)
    try:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR  # type: ignore

        params: dict[str, Any] = {
            "Global.use_cls": False,
            "Det.limit_side_len": 1280,
            "Det.limit_type": "max",
        }
        if (lang or "en").lower().startswith("en"):
            params.update({
                "Rec.lang_type": LangRec.EN,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
            })
        try:
            _RAPID_ENGINE = RapidOCR(params=params)
        except Exception:
            # Offline fallback if PP-OCRv5 EN isn't cached yet: try PP-OCRv4 EN, then defaults
            try:
                params["Rec.ocr_version"] = OCRVersion.PPOCRV4
                _RAPID_ENGINE = RapidOCR(params=params)
            except Exception:
                _RAPID_ENGINE = RapidOCR(params={"Global.use_cls": False})
        _RAPID_BACKEND_KIND = "rapidocr"
        return _RAPID_ENGINE, _RAPID_BACKEND_KIND
    except Exception:
        pass

    # Fallback to rapidocr_onnxruntime (bundled offline PP-OCRv4 ONNX models)
    try:
        from rapidocr_onnxruntime import RapidOCR as RapidONNX  # type: ignore

        _RAPID_ENGINE = RapidONNX(use_cls=False, limit_side_len=1280, limit_type="max")
        _RAPID_BACKEND_KIND = "rapidocr_onnxruntime"
        return _RAPID_ENGINE, _RAPID_BACKEND_KIND
    except Exception as e:
        raise PaddleUnavailableError(f"RapidOCR unavailable: {e}") from e


def _engine_for(lang: str):
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    import os
    if platform.system() == "Windows":
        os.environ.setdefault("FLAGS_enable_pir_api", "0")
    try:
        from paddleocr import PaddleOCR  # type: ignore
    except Exception as e:  # ImportError or env problems
        raise PaddleUnavailableError(
            "PaddleOCR is not installed in this environment. This backend is "
            "optional and disabled by default; Tesseract remains the engine "
            "for the application. Install per docs/phase3_ocr_evaluation.md "
            "and retry, or keep using --backend tesseract."
        ) from e
    # PaddleOCR 3.x renamed the orientation flag; try the modern signature
    # first, then fall back to the 2.x name. Both are instantiation-only
    # differences; output parsing below handles both result shapes.
    #
    # enable_mkldnn=False: on Windows CPU, PaddlePaddle 3.3.1 crashes in
    # the oneDNN instruction path for these OCR models
    # ("ConvertPirAttribute2RuntimeAttribute not support
    # [pir::ArrayAttribute<pir::DoubleAttribute>]"); the plain "paddle"
    # run mode is unaffected. Measured 2026-10-15 with paddleocr 3.7.0 /
    # paddlepaddle 3.3.1 (see docs/phase3_ocr_evaluation.md).
    threads = max(1, min(4, os.cpu_count() or 4))
    try:
        _ENGINE = PaddleOCR(
            lang=lang,
            use_textline_orientation=False,
            use_doc_unwarping=False,
            use_doc_orientation_classify=False,
            enable_mkldnn=False,
            cpu_threads=threads,
        )
    except TypeError:
        try:
            _ENGINE = PaddleOCR(lang=lang, use_textline_orientation=False,
                                enable_mkldnn=False)
        except TypeError:
            _ENGINE = PaddleOCR(lang=lang, use_angle_cls=True,
                                enable_mkldnn=False)
    return _ENGINE


def _bbox_from_poly(poly: Any) -> tuple[float, float, float, float] | None:
    """Bounding box of a quad (Nx2 points) or flat box (x1,y1,x2,y2)."""
    try:
        pts = [(float(p[0]), float(p[1])) for p in poly]
    except (TypeError, IndexError, ValueError):
        try:
            vals = [float(v) for v in poly]
        except (TypeError, ValueError):
            return None
        if len(vals) == 4:
            x1, y1, x2, y2 = vals
            return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _entries_from_result(
    result: Any,
) -> list[tuple[tuple[float, float, float, float] | None, str, float | None]]:
    """Flatten PaddleOCR result shapes into (bbox, text, score) entries.

    Handles 3.x ``predict()`` dicts (``rec_texts``/``rec_scores`` plus
    ``rec_polys``/``rec_boxes``) and 2.x ``ocr()`` lists of
    ``[box, (text, score)]`` entries. ``bbox`` is None when the result
    carries no geometry.
    """
    entries: list[
        tuple[tuple[float, float, float, float] | None, str, float | None]
    ] = []

    def _walk(obj: Any) -> None:
        if obj is None:
            return
        if isinstance(obj, dict):
            rec_texts = obj.get("rec_texts")
            if isinstance(rec_texts, (list, tuple)):
                rec_scores = obj.get("rec_scores")
                polys = obj.get("rec_polys")
                if polys is None:
                    polys = obj.get("rec_boxes")
                for i, t in enumerate(rec_texts):
                    if t is None or not str(t).strip():
                        continue
                    score = None
                    try:
                        score = float(rec_scores[i])
                    except (TypeError, IndexError, ValueError):
                        pass
                    box = None
                    try:
                        box = _bbox_from_poly(polys[i])
                    except (TypeError, IndexError, ValueError):
                        pass
                    entries.append((box, str(t), score))
                return
            for v in obj.values():
                _walk(v)
            return
        if isinstance(obj, (list, tuple)):
            if (len(obj) == 2 and isinstance(obj[1], (list, tuple))
                    and len(obj[1]) == 2 and isinstance(obj[1][0], str)):
                score = None
                try:
                    score = float(obj[1][1])
                except (TypeError, ValueError):
                    pass
                entries.append((_bbox_from_poly(obj[0]), obj[1][0], score))
                return
            for v in obj:
                _walk(v)

    _walk(result)
    return entries


def _group_entries_into_lines(
    entries: list[
        tuple[tuple[float, float, float, float] | None, str, float | None]
    ],
    img_width: int | None = None,
) -> list[str]:
    """Rebuild visual text lines from per-box OCR results.

    Boxes whose vertical overlap is at least half the smaller box height
    are treated as one visual line and joined left-to-right; lines are
    ordered top-to-bottom. When ``img_width`` is provided and the page is
    a clean two-column layout (such as a full e-Aadhaar letter with Front on
    the left and Back/Address on the right), left and right columns are kept
    separate so fields never mix across the vertical gutter. Entries without
    geometry keep their original order (one line each), so mocked/legacy
    results stay unchanged.
    """
    if any(box is None for box, _, _ in entries):
        return [t for _, t, _ in entries]

    # Check for clean two-column page layout (e.g. e-Aadhaar letter) when img_width is known.
    if img_width and img_width >= 200 and len(entries) >= 10:
        w = float(img_width)
        col_rows: list[dict[str, Any]] = []
        for box, text, _score in entries:
            assert box is not None
            x0, y0, x1, y1 = box
            box_h = max(1.0, y1 - y0)
            for row in col_rows:
                row_h = max(1.0, row["y1"] - row["y0"])
                if max(box_h, row_h) > 2.2 * min(box_h, row_h):
                    continue
                inter = min(y1, row["y1"]) - max(y0, row["y0"])
                min_h = min(box_h, row_h)
                if min_h > 0 and inter >= 0.5 * min_h:
                    h_gap = min(
                        max(0.0, x0 - rx1, rx0 - x1)
                        for rx0, rx1, _ in row["items"]
                    )
                    if h_gap <= max(w * 0.10, box_h * 3.5):
                        row["items"].append((x0, x1, text))
                        row["y0"] = min(y0, row["y0"])
                        row["y1"] = max(y1, row["y1"])
                        row["x0"] = min(x0, row["x0"])
                        row["x1"] = max(x1, row["x1"])
                        break
            else:
                col_rows.append({
                    "y0": y0, "y1": y1, "x0": x0, "x1": x1,
                    "items": [(x0, x1, text)],
                })
        mid = w * 0.50
        col_l = [r for r in col_rows if r["x1"] <= mid + w * 0.05]
        col_r = [r for r in col_rows if r["x0"] >= mid - w * 0.05 and r not in col_l]
        col_s = [r for r in col_rows if r not in col_l and r not in col_r]
        if len(col_l) >= 5 and len(col_r) >= 5 and len(col_s) <= max(4, int(len(col_rows) * 0.12)):
            ordered = (
                sorted(col_s, key=lambda r: (r["y0"], r["x0"]))
                + sorted(col_l, key=lambda r: (r["y0"], r["x0"]))
                + sorted(col_r, key=lambda r: (r["y0"], r["x0"]))
            )
            return [
                " ".join(t for _, _, t in sorted(row["items"], key=lambda it: it[0]))
                for row in ordered
            ]

    rows: list[dict[str, Any]] = []
    for box, text, _score in entries:
        assert box is not None  # guaranteed by the early return above
        x0, y0, x1, y1 = box
        box_h = max(1.0, y1 - y0)
        for row in rows:
            row_h = max(1.0, row["y1"] - row["y0"])
            if max(box_h, row_h) > 2.2 * min(box_h, row_h):
                continue
            inter = min(y1, row["y1"]) - max(y0, row["y0"])
            min_h = min(box_h, row_h)
            if min_h > 0 and inter >= 0.5 * min_h:
                row["items"].append((x0, text))
                row["y0"] = min(y0, row["y0"])
                row["y1"] = max(y1, row["y1"])
                break
        else:
            rows.append({"y0": y0, "y1": y1, "items": [(x0, text)]})
    rows.sort(key=lambda r: (r["y0"], min(x for x, _ in r["items"])))
    return [" ".join(t for _, t in sorted(row["items"])) for row in rows]


def _extract_lines(result: Any, img_width: int | None = None) -> tuple[list[str], list[float]]:
    """Normalize PaddleOCR result shapes into (lines, scores)."""
    entries = _entries_from_result(result)
    lines = _group_entries_into_lines(entries, img_width=img_width)
    scores = [s for _, _, s in entries if s is not None]
    return lines, scores


def _entries_from_rapid_output(
    out: Any, kind: str | None
) -> list[tuple[tuple[float, float, float, float] | None, str, float | None]]:
    """Normalize RapidOCR v3 or rapidocr_onnxruntime outputs into (bbox, text, score)."""
    entries: list[
        tuple[tuple[float, float, float, float] | None, str, float | None]
    ] = []
    if out is None:
        return entries
    if kind == "rapidocr":
        boxes = getattr(out, "boxes", None)
        txts = getattr(out, "txts", None)
        scores = getattr(out, "scores", None)
        if txts is None:
            return entries
        for i, txt in enumerate(txts):
            if txt is None or not str(txt).strip():
                continue
            box = None
            if boxes is not None and i < len(boxes):
                box = _bbox_from_poly(boxes[i])
            sc = None
            if scores is not None and i < len(scores):
                try:
                    sc = float(scores[i])
                except (TypeError, ValueError):
                    pass
            entries.append((box, str(txt), sc))
        return entries
    # rapidocr_onnxruntime returns (result_list, elapse)
    res_list = out[0] if isinstance(out, tuple) else out
    for item in (res_list or []):
        try:
            poly, txt, sc = item[0], item[1], item[2]
        except (TypeError, IndexError):
            continue
        if txt is None or not str(txt).strip():
            continue
        try:
            score = float(sc)
        except (TypeError, ValueError):
            score = None
        entries.append((_bbox_from_poly(poly), str(txt), score))
    return entries


def _run_engine(engine: Any, img) -> tuple[str, float]:
    import numpy as np  # lazy; already a dependencies of the opencv variants

    arr = np.array(img.convert("RGB") if img.mode != "RGB" else img)
    if hasattr(engine, "predict"):
        result = engine.predict(arr)  # PaddleOCR 3.x
    else:
        result = engine.ocr(arr, cls=True)  # PaddleOCR 2.x
    texts, scores = _extract_lines(result, img_width=img.width)
    mean = (sum(scores) / len(scores)) if scores else 0.0
    return "\n".join(texts), mean


def make_paddle_ocr_fn(lang: str = "en") -> Callable[[Any], tuple[str, float]]:
    """Return an ``ocr_fn`` compatible with ``ocr.ocr_image(..., ocr_fn=...)``.

    The engine is created on the first call, not at import time, so the
    benchmark can record a clean ``unavailable`` failure per case when
    PaddleOCR is missing instead of crashing the suite.
    """

    def _fn(img) -> tuple[str, float]:
        engine = _engine_for(lang)
        return _run_engine(engine, img)

    return _fn


def run_paddle_ocr_full(
    img: Any, lang: str = "en"
) -> tuple[str, float, list[dict[str, Any]], str | None]:
    """Execute PaddleOCR (RapidOCR ONNXRuntime primary, native PaddleOCR fallback).

    Always runs on the native-resolution RGB image passed in (no downscaling or
    blurring) and returns (text, mean_confidence, regions, version).
    """
    import numpy as np

    rgb_im = img.convert("RGB") if img.mode != "RGB" else img
    arr = np.array(rgb_im)
    entries: list[tuple[tuple[float, float, float, float] | None, str, float | None]] = []
    ver: str | None = None

    # Honor explicitly injected/cached native _ENGINE (e.g. unit tests) first.
    if _ENGINE is None and _rapid_available():
        try:
            r_eng, r_kind = _rapid_engine_for(lang)
            out = r_eng(arr)
            entries = _entries_from_rapid_output(out, r_kind)
            r_ver = _package_version("rapidocr") or _package_version("rapidocr-onnxruntime")
            ver = f"{r_kind}-{r_ver}" if r_ver else r_kind
        except Exception:
            entries = []
            ver = None

    if ver is None:
        engine = _engine_for(lang)
        if hasattr(engine, "predict"):
            result = engine.predict(arr)
        else:
            result = engine.ocr(arr, cls=True)
        entries = _entries_from_result(result)
        ver = _package_version("paddleocr")

    lines = _group_entries_into_lines(entries, img_width=rgb_im.width)
    scores = [s for _, _, s in entries if s is not None]
    mean = (sum(scores) / len(scores)) if scores else 0.0
    regions: list[dict[str, Any]] = []
    for idx, (box, text, score) in enumerate(entries):
        regions.append({
            "text": text,
            "bbox": [int(round(v)) for v in box] if box else None,
            "confidence": float(score) if score is not None else 0.0,
            "reading_order": idx,
        })
    return "\n".join(lines), mean, regions, ver

