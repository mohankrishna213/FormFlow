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
  * No cloud calls are made by this adapter. PaddleOCR downloads model
    files on its FIRST real run unless a local model dir is configured;
    see docs/phase3_ocr_evaluation.md for the offline procedure.

Local verification status: UNVERIFIED on this host. At evaluation time
(Python 3.14.7 / Windows) no compatible PaddlePaddle CPU wheel existed,
so the adapter was only exercised against mocked engines in tests. Do not
label any measured run "paddleocr" unless the package is actually
installed and the run executed.
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
    try:
        _ENGINE = PaddleOCR(lang=lang, use_textline_orientation=False)
    except TypeError:
        _ENGINE = PaddleOCR(lang=lang, use_angle_cls=True)
    return _ENGINE


def _extract_lines(result: Any) -> tuple[list[str], list[float]]:
    """Normalize PaddleOCR result shapes into (texts, scores).

    Handles 3.x ``predict()`` dicts (``rec_texts``/``rec_scores``) and
    2.x ``ocr()`` lists of ``[box, (text, score)]`` entries.
    """
    texts: list[str] = []
    scores: list[float] = []

    def _walk(obj: Any) -> None:
        if obj is None:
            return
        if isinstance(obj, dict):
            rec_texts = obj.get("rec_texts")
            rec_scores = obj.get("rec_scores")
            if isinstance(rec_texts, (list, tuple)):
                for i, t in enumerate(rec_texts):
                    if t is not None and str(t).strip():
                        texts.append(str(t))
                        try:
                            scores.append(float(rec_scores[i]))
                        except (TypeError, IndexError, ValueError):
                            pass
                return
            for v in obj.values():
                _walk(v)
            return
        if isinstance(obj, (list, tuple)):
            if (len(obj) == 2 and isinstance(obj[1], (list, tuple))
                    and len(obj[1]) == 2 and isinstance(obj[1][0], str)):
                texts.append(obj[1][0])
                try:
                    scores.append(float(obj[1][1]))
                except (TypeError, ValueError):
                    pass
                return
            for v in obj:
                _walk(v)

    _walk(result)
    return texts, scores


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
