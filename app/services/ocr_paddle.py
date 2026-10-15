"""Optional PaddleOCR backend adapter (Phase 3 challenger, opt-in only).

Design rules:
  * Tesseract stays the default engine. This module is imported lazily and
    never imported by the baseline pipeline; a missing PaddleOCR install
    cannot break app startup, extraction, or the Tesseract benchmark.
  * ``paddleocr``/``numpy`` imports happen only inside :func:`_engine_for`
    when a run is actually requested. Absence raises
    :class:`PaddleUnavailableError`, which the benchmark records as
    ``failure_kind="unavailable"`` (a skipped run, never a success).
  * Output is normalized to the same contract the Tesseract path uses:
    ``(text, mean_confidence_0_to_1)`` so scoring rules are identical.
    PaddleOCR reports one box per detected text segment; when box
    geometry is available the boxes are re-grouped into visual lines
    (same-row boxes joined left-to-right, rows top-to-bottom) so the
    downstream parser sees label and value on one line exactly as with
    Tesseract's line-based output.
  * No cloud calls are made by this adapter. PaddleOCR downloads model
    files on its FIRST real run unless a local model dir is configured;
    see docs/phase3_ocr_evaluation.md for the offline procedure.

Local verification status: REAL ENGINE RUN verified on 2026-10-15 in a
separate Python 3.11 venv (paddlepaddle 3.3.1 CPU / paddleocr 3.7.0,
Windows). PaddleOCR 3.7 downloads PP-OCRv6_medium det+rec models and also
runs doc-orientation (PP-LCNet_x1_0_doc_ori) and unwarping (UVDoc) steps
by default; measured results are recorded in docs/phase3_ocr_evaluation.md.
On this Windows host the oneDNN ("mkldnn") run mode crashes for these
models ("ConvertPirAttribute2RuntimeAttribute not support ..."), so the
engine is created with ``enable_mkldnn=False`` (plain "paddle" mode) --
see :func:`_engine_for`. PaddleOCR returns one box per detected text
segment (the printed label and its value are frequently separate boxes);
without the visual-line regrouping in :func:`_group_entries_into_lines`
only block-shaped fields (address) match on otherwise-clean documents,
so that normalization is required, not cosmetic. Tesseract remains the
default engine.
"""
from __future__ import annotations

import importlib.util
import platform
import sys
from typing import Any, Callable


class PaddleUnavailableError(Exception):
    """PaddleOCR is not installed/importable. Install it, then retry."""


_ENGINE: Any = None  # cached engine instance (per process)


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return None


def available() -> bool:
    """True only when the paddleocr package is importable in this env."""
    try:
        return importlib.util.find_spec("paddleocr") is not None
    except (ImportError, ValueError):
        return False


def paddle_provenance() -> dict[str, Any]:
    """Availability snapshot for reports. No image data, no model files."""
    ok = available()
    return {
        "available": ok,
        "paddleocr_version": _package_version("paddleocr") if ok else None,
        "paddlepaddle_version": _package_version("paddlepaddle") if ok else None,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "error": None if ok else (
            "paddleocr not importable. Optional backend: install in a "
            "separate venv following docs/phase3_ocr_evaluation.md; "
            "Tesseract remains the default engine."),
    }


def _reset_engine_cache() -> None:
    """Test helper: drop the cached engine instance."""
    global _ENGINE
    _ENGINE = None


def _engine_for(lang: str):
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
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
) -> list[str]:
    """Rebuild visual text lines from per-box OCR results.

    Boxes whose vertical overlap is at least half the smaller box height
    are treated as one visual line and joined left-to-right; lines are
    ordered top-to-bottom. Entries without geometry keep their original
    order (one line each), so mocked/legacy results stay unchanged.
    """
    if any(box is None for box, _, _ in entries):
        return [t for _, t, _ in entries]

    rows: list[dict[str, Any]] = []
    for box, text, _score in entries:
        assert box is not None  # guaranteed by the early return above
        x0, y0, x1, y1 = box
        for row in rows:
            inter = min(y1, row["y1"]) - max(y0, row["y0"])
            min_h = min(y1 - y0, row["y1"] - row["y0"])
            if min_h > 0 and inter >= 0.5 * min_h:
                row["items"].append((x0, text))
                row["y0"] = min(y0, row["y0"])
                row["y1"] = max(y1, row["y1"])
                break
        else:
            rows.append({"y0": y0, "y1": y1, "items": [(x0, text)]})
    rows.sort(key=lambda r: (r["y0"], min(x for x, _ in r["items"])))
    return [" ".join(t for _, t in sorted(row["items"])) for row in rows]


def _extract_lines(result: Any) -> tuple[list[str], list[float]]:
    """Normalize PaddleOCR result shapes into (lines, scores)."""
    entries = _entries_from_result(result)
    lines = _group_entries_into_lines(entries)
    scores = [s for _, _, s in entries if s is not None]
    return lines, scores


def _run_engine(engine: Any, img) -> tuple[str, float]:
    import numpy as np  # lazy; already a dependencies of the opencv variants

    arr = np.array(img.convert("RGB") if img.mode != "RGB" else img)
    if hasattr(engine, "predict"):
        result = engine.predict(arr)  # PaddleOCR 3.x
    else:
        result = engine.ocr(arr, cls=True)  # PaddleOCR 2.x
    texts, scores = _extract_lines(result)
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
