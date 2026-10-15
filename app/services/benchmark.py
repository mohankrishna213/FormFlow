"""Phase 1 baseline OCR benchmark (read-only harness, no pipeline changes).

Compares deterministic field extraction against a reference WITHOUT
persisting raw OCR text or personal values. Outputs contain only
booleans, lengths, methods/confidences and timing -- never ``value``
or ``text`` -- so results stay safe to keep locally and must still
live under gitignored ``data/benchmark/`` (``data/`` is gitignored).

Critical fields (per task): full_name, dob, licence_no,
application_no, address. ``application_no`` currently has NO extractor
(it is operator-entered via review UI), so baseline coverage for it is
0 by design; the harness records the gap instead of inventing values.
``father_name`` has no parser either (noted as a known gap, not scored).

Two uses:
  1. Synthetic pytest fixtures (no PII): pass ``pages`` + ``reference``
     directly to :func:`score_pages`.
  2. Explicit local-only manual run on a real file (never committed):
     ``python -m app.services.benchmark --input <path>
     --reference <json> --out data/benchmark``.
     The reference JSON lives outside Git (e.g. data/benchmark/) and
     is never copied into tests or logs.

Metrics per case and suite: per-field exact-match (after
normalization), coverage (predicted present / expected present and /
total critical), false positives (predicted present but missing or
wrong), mean/p95 latency, backend failure rate. Provenance records
python/platform/tesseract availability + ocr_rules snapshot so runs
are reproducible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image

CRITICAL_FIELDS = ("full_name", "dob", "licence_no", "application_no", "address")

# Keys that must never appear in benchmark outputs (raw evidence).
_FORBIDDEN_OUTPUT_KEYS = {"value", "text", "expected", "predicted", "raw"}


def _norm_name(v: str) -> str:
    from .fields import normalize_name

    return normalize_name(v or "")


def _norm_dob(v: str) -> str:
    return (v or "").strip()


def _norm_code(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").strip().upper()).strip()


def _norm_address(v: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\"']", "", (v or "").lower())).strip()


_NORMALIZERS = {
    "full_name": _norm_name,
    "dob": _norm_dob,
    "licence_no": _norm_code,
    "application_no": _norm_code,
    "address": _norm_address,
}


def normalize_field(field_key: str, value: str | None) -> str:
    fn = _NORMALIZERS.get(field_key, lambda v: (v or "").strip())
    return fn(value or "")


def collect_provenance(cfg) -> dict[str, Any]:
    """Reproducibility snapshot. No document text, no personal data."""
    from .fields import load_field_rules

    rules = load_field_rules(cfg)
    tess: dict[str, Any] = {"available": False, "exe": None, "error": None}
    try:
        from .ocr import tesseract_exe

        tess["exe"] = tesseract_exe(cfg)
        tess["available"] = True
    except Exception as e:  # OcrUnavailableError or import issue
        tess["error"] = str(e)[:200]
    fields_snapshot = {}
    if isinstance(rules.get("fields"), dict):
        for k, v in rules["fields"].items():
            fields_snapshot[k] = {"labels": list(v.get("labels", []))} if isinstance(v, dict) else {}
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "backend": "tesseract",
        "preprocess_variant": "baseline",
        "tesseract": tess,
        "ocr_rules": {
            "version": rules.get("version"),
            "dpi": rules.get("dpi"),
            "lang": rules.get("lang"),
            "psm": rules.get("psm"),
            "upscale": rules.get("upscale"),
            "upscale_min_height": rules.get("upscale_min_height"),
            "min_page_chars": rules.get("min_page_chars"),
            "binarize": rules.get("binarize"),
            "fields": fields_snapshot,
        },
    }


def _field_score(
    field_key: str, expected: str | None, candidates: list[dict]
) -> dict[str, Any]:
    """Redacted per-field outcome. Never echoes values."""
    relevant = [c for c in candidates if c.get("field_key") == field_key]
    exp_present = expected is not None and str(expected).strip() != ""
    pred_present = len(relevant) > 0
    exp_norm = normalize_field(field_key, expected) if exp_present else ""
    match = False
    if exp_present and pred_present:
        for c in relevant:
            if normalize_field(field_key, c.get("value")) == exp_norm and exp_norm != "":
                match = True
                break
    distinct = {normalize_field(field_key, c.get("value")) for c in relevant}
    false_positive = pred_present and not match
    # Uncertain/date-ambiguity or low-confidence candidates are flagged,
    # never silently counted as correct.
    low_conf = any((c.get("confidence") or 0) < 0.6 for c in relevant)
    return {
        "field": field_key,
        "expected_present": exp_present,
        "predicted_present": pred_present,
        "exact_match": match,
        "false_positive": false_positive,
        "num_candidates": len(relevant),
        "has_conflict": len(distinct) > 1,
        "low_confidence": low_conf,
        "methods": sorted({str(c.get("method", "?")) for c in relevant}),
        "expected_len": len(str(expected)) if exp_present else 0,
        "predicted_lens": sorted([len(str(c.get("value", ""))) for c in relevant]),
    }


def score_pages(
    pages: list[dict],
    rules: dict[str, Any],
    reference: dict[str, str | None],
    latency_sec: float = 0.0,
    backend: str = "tesseract",
    preprocess_variant: str = "baseline",
    provenance: dict[str, Any] | None = None,
    case_id: str = "synthetic",
    input_sha256: str | None = None,
) -> dict[str, Any]:
    """Score already-obtained ``pages`` against ``reference`` (redacted)."""
    from .fields import extract_candidates

    t0 = time.perf_counter()
    candidates = extract_candidates(pages, rules)
    parse_sec = time.perf_counter() - t0
    total_latency = latency_sec + parse_sec
    fields = {
        k: _field_score(k, reference.get(k), candidates) for k in CRITICAL_FIELDS
    }
    result = {
        "case_id": case_id,
        "backend": backend,
        "preprocess_variant": preprocess_variant,
        "provenance": provenance,
        "input_sha256": (input_sha256 or "")[:16],
        "num_pages": len(pages),
        "page_methods": [p.get("method", "?") for p in pages],
        "ocr_chars": [len(p.get("text", "")) for p in pages],
        "latency_sec": round(total_latency, 4),
        "ocr_latency_sec": round(latency_sec, 4),
        "parse_latency_sec": round(parse_sec, 4),
        "failed": False,
        "failure_kind": None,
        "fields": fields,
    }
    _assert_redacted(result)
    return result


def benchmark_file(
    src: Path,
    reference: dict[str, str | None],
    cfg,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
    backend: str = "tesseract",
    preprocess_variant: str = "baseline",
    case_id: str | None = None,
) -> dict[str, Any]:
    """Run baseline ``extract_document_text`` + scoring on one local file.

    Never raises for OCR problems: failures are recorded as
    ``failed: True`` with ``failure_kind`` so the suite can report a
    backend failure rate. ``src`` is only read, never modified.
    """
    from .fields import load_field_rules
    from .ocr import OcrError, OcrUnavailableError, extract_document_text

    rules = load_field_rules(cfg)
    provenance = collect_provenance(cfg)
    provenance["backend"] = backend
    provenance["preprocess_variant"] = preprocess_variant
    sha = _sha256_file(src)
    t0 = time.perf_counter()
    try:
        out = extract_document_text(
            src,
            cfg,
            dpi=int(rules.get("dpi", 300)),
            lang=str(rules.get("lang", "eng")),
            min_page_chars=int(rules.get("min_page_chars", 30)),
            binarize=bool(rules.get("binarize", False)),
            psm=int(rules.get("psm", 6)),
            upscale=bool(rules.get("upscale", False)),
            upscale_min_height=int(rules.get("upscale_min_height", 1500)),
            ocr_fn=ocr_fn,
        )
        latency = time.perf_counter() - t0
        return score_pages(
            out["pages"],
            rules,
            reference,
            latency_sec=latency,
            backend=backend,
            preprocess_variant=preprocess_variant,
            provenance=provenance,
            case_id=case_id or src.name,
            input_sha256=sha,
        )
    except OcrUnavailableError as e:
        return _failure(
            case_id or src.name, backend, preprocess_variant, provenance,
            sha, time.perf_counter() - t0, "unavailable", str(e),
        )
    except OcrError as e:
        return _failure(
            case_id or src.name, backend, preprocess_variant, provenance,
            sha, time.perf_counter() - t0, "ocr_error", str(e),
        )
    except Exception as e:  # never crash a suite on one file
        return _failure(
            case_id or src.name, backend, preprocess_variant, provenance,
            sha, time.perf_counter() - t0, type(e).__name__, str(e),
        )


def _failure(case_id, backend, variant, provenance, sha, latency, kind, msg) -> dict:
    res = {
        "case_id": case_id,
        "backend": backend,
        "preprocess_variant": variant,
        "provenance": provenance,
        "input_sha256": (sha or "")[:16],
        "num_pages": 0,
        "page_methods": [],
        "ocr_chars": [],
        "latency_sec": round(latency, 4),
        "ocr_latency_sec": round(latency, 4),
        "parse_latency_sec": 0.0,
        "failed": True,
        "failure_kind": kind,
        "failure_hint": msg[:200],
        "fields": {
            k: {
                "field": k, "expected_present": False, "predicted_present": False,
                "exact_match": False, "false_positive": False,
                "num_candidates": 0, "has_conflict": False,
                "low_confidence": False, "methods": [],
                "expected_len": 0, "predicted_lens": [],
            }
            for k in CRITICAL_FIELDS
        },
    }
    _assert_redacted(res)
    return res


def summarize(results: list[dict]) -> dict[str, Any]:
    """Suite-level metrics over redacted per-case results."""
    n = len(results)
    failed = sum(1 for r in results if r.get("failed"))
    lats = sorted([float(r.get("latency_sec", 0.0)) for r in results])
    per_field: dict[str, dict] = {}
    for k in CRITICAL_FIELDS:
        exp = sum(1 for r in results if r["fields"][k]["expected_present"])
        pred = sum(1 for r in results if r["fields"][k]["predicted_present"])
        hit = sum(1 for r in results if r["fields"][k]["exact_match"])
        fp = sum(1 for r in results if r["fields"][k]["false_positive"])
        per_field[k] = {
            "expected_present": exp,
            "predicted_present": pred,
            "exact_match": hit,
            "exact_match_rate_on_expected": round(hit / exp, 4) if exp else None,
            "coverage_on_expected": round(pred / exp, 4) if exp else None,
            "false_positives": fp,
        }
    total_exp = sum(v["expected_present"] for v in per_field.values())
    total_hit = sum(v["exact_match"] for v in per_field.values())
    total_pred = sum(v["predicted_present"] for v in per_field.values())
    return {
        "num_cases": n,
        "failures": failed,
        "backend_failure_rate": round(failed / n, 4) if n else 0.0,
        "latency_sec": {
            "mean": round(sum(lats) / len(lats), 4) if lats else 0.0,
            "p95": round(lats[min(len(lats) - 1, int(-(-95 * len(lats) // 100)) - 1)], 4) if lats else 0.0,
            "max": round(max(lats), 4) if lats else 0.0,
        },
        "per_field": per_field,
        "overall_exact_match_rate": round(total_hit / total_exp, 4) if total_exp else None,
        "overall_coverage": round(total_pred / total_exp, 4) if total_exp else None,
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _assert_redacted(obj: Any, _path: str = "$") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in _FORBIDDEN_OUTPUT_KEYS, f"raw key leaked at {_path}.{k}"
            _assert_redacted(v, f"{_path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_redacted(v, f"{_path}[{i}]")


def load_reference(path: Path) -> dict[str, str | None]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("reference JSON must be an object")
    ref: dict[str, str | None] = {}
    for k in CRITICAL_FIELDS:
        v = raw.get(k)
        ref[k] = None if v in (None, "") else str(v)
    return ref


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Local-only FormFlow OCR baseline benchmark. "
        "Reference JSON with personal data must live outside Git "
        "(e.g. data/benchmark/, which is gitignored). Raw text/values "
        "are never written to outputs or logs.")
    ap.add_argument("--input", required=True, help="local document path (read-only)")
    ap.add_argument("--reference", required=True, help="local reference JSON path (outside Git)")
    ap.add_argument("--out", default="data/benchmark", help="output dir (default gitignored data/benchmark)")
    ap.add_argument("--backend", default="tesseract")
    ap.add_argument("--preprocess-variant", default="baseline")
    ap.add_argument("--case-id", default=None)
    args = ap.parse_args(argv)

    from ..config import load_config

    cfg = load_config()
    src = Path(args.input)
    if not src.is_file():
        print(f"input not found: {src}", file=sys.stderr)
        return 2
    ref = load_reference(Path(args.reference))
    res = benchmark_file(src, ref, cfg, backend=args.backend,
                         preprocess_variant=args.preprocess_variant,
                         case_id=args.case_id or src.name)
    summary = summarize([res])
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = outdir / f"benchmark_{stamp}.json"
    out.write_text(json.dumps({"results": [res], "summary": summary},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    # Redacted stdout: metrics only, no values/text.
    print(json.dumps({"out": str(out), "summary": summary,
                      "failed": res["failed"],
                      "failure_kind": res.get("failure_kind"),
                      "per_field_match": {k: v["exact_match"] for k, v in res["fields"].items()}},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
