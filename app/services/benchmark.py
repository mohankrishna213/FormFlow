"""Phase 1 baseline OCR benchmark (read-only harness, no pipeline changes).

Privacy assumptions (explicit):
  * Normal reports contain NO raw OCR text and NO applicant values --
    only booleans, counts, lengths, method names, timing and hashes.
  * ``case_id`` values in reports are opaque by default (``case-<sha8>``
    or caller-supplied synthetic IDs). Filenames are NEVER used unless
    the operator explicitly passes ``--case-id``.
  * Report output defaults to gitignored ``data/benchmark/``. Writes to
    any other location inside the repo (outside ``data/``) are refused
    to prevent accidental Git commits. ``%TEMP%``/out-of-repo paths are
    allowed for smoke tests.
  * Reference JSON files with personal data must live outside Git
    (e.g. ``data/benchmark/``, which is gitignored via ``data/``) and
    are never copied into tests, logs or reports.
  * Raw OCR text inspection is available ONLY via an explicit opt-in
    diagnostic bundle (``--write-diagnostics`` / :func:`write_diagnostic_bundle`),
    written as a separate file in the same ignored out dir. Normal
    stdout/reports never contain raw text.

Stage separation:
  * OCR recognition stage (:func:`ocr_stage_diagnostics`): redacted
    per-page counts (chars/words/digits) plus per-field
    ``label_present`` booleans (does any configured label substring
    appear in the OCR text?). If a label is present but no candidate
    was parsed, the parser rejected it; if the label is absent, the
    OCR text itself is missing/altered. No raw text is stored.
  * Field extraction stage (:func:`score_pages` ``fields`` block):
    deterministic candidate scoring against the reference.

Metric definitions (documented denominators, per field ``k``):
  * N = number of cases in the suite.
  * E = cases where the reference expects ``k`` (non-empty).
  * P = cases where the system predicted ``k`` (>= 1 candidate).
  * C = cases where the reference expects ``k`` AND the system
    predicted ``k``. C <= E, so coverage never exceeds 100%.
  * TP = cases where the reference expects ``k`` AND at least one
    candidate normalizes exactly to the expected value.
  * FP = cases where the system predicted ``k`` AND (no candidate
    matches OR at least one extra non-matching candidate exists).
    Spurious predictions on expected-absent references count here, as
    do extra wrong candidates alongside a correct one.
  * clean_TP = cases where TP holds AND no extra non-matching
    candidate exists.
  * recall = TP / E (None when E == 0). Alias
    ``exact_match_rate_on_expected`` is identical by definition.
  * coverage_on_expected = C / E (None when E == 0). Always <= 1.
  * precision = clean_TP / P (None when P == 0). Extra candidates
    reduce precision even when another candidate matches.
  * false_positives = FP (count).

Critical fields (6): full_name, father_name, dob, licence_no,
application_no, address. ``father_name`` and ``application_no``
currently have NO parsers in ``app/services/fields.py`` (operator-
entered or future work); the harness reports the gap
(predicted_present False) without inventing values.

Backend labels are truthful: only ``tesseract`` + ``baseline``
preprocessing exist in this tree (OpenCV/PaddleOCR deferred). Any
other ``backend``/``preprocess_variant`` label raises ValueError
instead of producing a misleading comparison.

Backend failures (OcrUnavailableError/OcrError/unexpected) never
raise: the case is recorded with ``failed: True`` AND its expected
fields retained (expected_present from the reference, predicted
False), so failures count as misses (recall/coverage drop) rather
than being silently excluded.
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

CRITICAL_FIELDS = (
    "full_name",
    "father_name",
    "dob",
    "licence_no",
    "application_no",
    "address",
)

SUPPORTED_BACKENDS = ("tesseract",)
SUPPORTED_PREPROCESS_VARIANTS = ("baseline",)

# Exact dict keys that must never appear in redacted reports.
_FORBIDDEN_OUTPUT_KEYS = {"value", "text", "expected", "predicted", "raw"}


def _validate_backend(backend: str) -> str:
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"unsupported backend '{backend}'. Supported: {list(SUPPORTED_BACKENDS)}. "
            "PaddleOCR/OpenCV variants do not exist in this tree yet; "
            "do not label runs with engines that were not executed."
        )
    return backend


def _validate_variant(variant: str) -> str:
    if variant not in SUPPORTED_PREPROCESS_VARIANTS:
        raise ValueError(
            f"unsupported preprocess_variant '{variant}'. "
            f"Supported: {list(SUPPORTED_PREPROCESS_VARIANTS)}. "
            "OpenCV geometry/quality preprocessing is deferred to Phase 2."
        )
    return variant


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
    "father_name": _norm_name,
    "dob": _norm_dob,
    "licence_no": _norm_code,
    "application_no": _norm_code,
    "address": _norm_address,
}


def normalize_field(field_key: str, value: str | None) -> str:
    fn = _NORMALIZERS.get(field_key, lambda v: (v or "").strip())
    return fn(value or "")


def collect_provenance(
    cfg, backend: str = "tesseract", preprocess_variant: str = "baseline"
) -> dict[str, Any]:
    """Reproducibility snapshot. No document text, no personal data."""
    _validate_backend(backend)
    _validate_variant(preprocess_variant)
    from .fields import load_field_rules

    rules = load_field_rules(cfg)
    tess: dict[str, Any] = {"available": False, "exe": None, "error": None}
    try:
        from .ocr import tesseract_exe

        tess["exe"] = tesseract_exe(cfg)
        tess["available"] = True
    except Exception as e:  # OcrUnavailableError or import issue
        tess["error"] = str(e)[:200]
    fields_snapshot: dict[str, Any] = {}
    if isinstance(rules.get("fields"), dict):
        for k, v in rules["fields"].items():
            fields_snapshot[k] = {"labels": list(v.get("labels", []))} if isinstance(v, dict) else {}
    # Explicit parser gaps (no silent invention downstream).
    for gap in ("father_name", "application_no"):
        fields_snapshot.setdefault(gap, {"labels": [], "parser_missing": True})
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "backend": backend,
        "preprocess_variant": preprocess_variant,
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


def ocr_stage_diagnostics(pages: list[dict], rules: dict[str, Any]) -> dict[str, Any]:
    """Redacted OCR-stage summary: counts + label presence, never raw text.

    ``label_present[k]`` is True when any configured label substring for
    ``k`` appears (case-insensitive) in the combined page text. Fields
    without a parser (father_name, application_no) report
    ``label_present: None`` and ``parser_missing: True`` -- the gap is
    structural, not an OCR observation.
    """
    combined = "\n".join(p.get("text", "") for p in pages)
    low = combined.lower()
    per_page = []
    for p in pages:
        t = p.get("text", "")
        per_page.append({
            "page": p.get("page", 1),
            "method": p.get("method", "?"),
            "chars": len(t),
            "words": len(t.split()),
            "digits": sum(ch.isdigit() for ch in t),
        })
    fields_cfg = rules.get("fields", {}) if isinstance(rules.get("fields"), dict) else {}
    label_present: dict[str, bool | None] = {}
    for k in CRITICAL_FIELDS:
        labels = fields_cfg.get(k, {}).get("labels", []) if isinstance(fields_cfg.get(k), dict) else []
        if not labels:
            label_present[k] = None
        else:
            label_present[k] = any(str(lb).lower() in low for lb in labels)
    return {"per_page": per_page, "label_present": label_present}


def _field_score(
    field_key: str, expected: str | None, candidates: list[dict]
) -> dict[str, Any]:
    """Redacted per-field outcome. Never echoes values."""
    relevant = [c for c in candidates if c.get("field_key") == field_key]
    exp_present = expected is not None and str(expected).strip() != ""
    pred_present = len(relevant) > 0
    exp_norm = normalize_field(field_key, expected) if exp_present else ""
    if exp_present and exp_norm != "":
        matching = sum(
            1 for c in relevant
            if normalize_field(field_key, c.get("value")) == exp_norm
        )
    else:
        matching = 0
    nonmatching = len(relevant) - matching if exp_present else len(relevant)
    exact_match = matching > 0
    extra_incorrect = nonmatching > 0
    # Spurious predictions count even when another candidate matches.
    false_positive = pred_present and (not exact_match or extra_incorrect)
    distinct = {normalize_field(field_key, c.get("value")) for c in relevant}
    low_conf = any((c.get("confidence") or 0) < 0.6 for c in relevant)
    return {
        "field": field_key,
        "expected_present": exp_present,
        "predicted_present": pred_present,
        "exact_match": exact_match,
        "false_positive": false_positive,
        "extra_incorrect": extra_incorrect,
        "num_candidates": len(relevant),
        "num_matching": matching,
        "num_nonmatching": nonmatching,
        "has_conflict": len(distinct) > 1,
        "low_confidence": low_conf,
        "methods": sorted({str(c.get("method", "?")) for c in relevant}),
        "expected_len": len(str(expected)) if exp_present else 0,
        "predicted_lens": sorted([len(str(c.get("value", ""))) for c in relevant]),
    }


def _opaque_case_id(sha256_hex: str | None) -> str:
    if sha256_hex:
        return f"case-{sha256_hex[:8]}"
    return f"case-{hashlib.sha256(str(time.perf_counter()).encode()).hexdigest()[:8]}"


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
    _validate_backend(backend)
    _validate_variant(preprocess_variant)
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
        "privacy": {
            "redacted": True,
            "contains_raw_text": False,
            "contains_applicant_values": False,
            "case_id_opaque_default": True,
        },
        "input_sha256": (input_sha256 or "")[:16],
        "num_pages": len(pages),
        "ocr_stage": ocr_stage_diagnostics(pages, rules),
        "latency_sec": round(total_latency, 4),
        "ocr_latency_sec": round(latency_sec, 4),
        "parse_latency_sec": round(parse_sec, 4),
        "failed": False,
        "failure_kind": None,
        "fields": fields,
    }
    _assert_redacted(result)
    return result


def extract_pages_raw(
    src: Path,
    cfg,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
) -> dict:
    """Return raw OCR pages WITH text for local inspection ONLY.

    Never embed the return value in redacted reports, logs or stdout.
    Use :func:`write_diagnostic_bundle` with explicit operator opt-in
    to persist it under the ignored out dir.
    """
    from .fields import load_field_rules
    from .ocr import extract_document_text

    rules = load_field_rules(cfg)
    return extract_document_text(
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


def write_diagnostic_bundle(
    out_dir: Path,
    opaque_case_id: str,
    pages: list[dict],
    candidates: list[dict],
    provenance: dict[str, Any] | None,
) -> Path:
    """Persist RAW text/values for local debugging (explicit opt-in only).

    The bundle file name is prefixed ``diagnostic-raw-`` so it is never
    confused with a redacted report. It must live under the ignored out
    dir enforced by :func:`resolve_out_dir`.
    """
    out_dir = resolve_out_dir(out_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", opaque_case_id)[:40] or "case"
    dest = out_dir / f"diagnostic-raw-{safe_id}-{stamp}.json"
    dest.write_text(json.dumps(
        {"case_id": opaque_case_id, "provenance": provenance,
         "pages": pages, "candidates": candidates},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WARNING: raw diagnostic bundle written to {dest} "
          f"(contains OCR text/values; keep local, never commit).",
          file=sys.stderr)
    return dest


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
    ``failed: True`` while retaining the reference's expected fields,
    so they count as misses. ``src`` is only read, never modified.
    Default ``case_id`` is opaque (``case-<sha8>``); filenames are
    never used unless the caller explicitly supplies ``case_id``.
    """
    _validate_backend(backend)
    _validate_variant(preprocess_variant)
    from .fields import load_field_rules
    from .ocr import OcrError, OcrUnavailableError, extract_document_text

    rules = load_field_rules(cfg)
    provenance = collect_provenance(cfg, backend, preprocess_variant)
    sha = _sha256_file(src)
    opaque = case_id if case_id else _opaque_case_id(sha)
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
            case_id=opaque,
            input_sha256=sha,
        )
    except OcrUnavailableError as e:
        return _failure(
            opaque, backend, preprocess_variant, provenance,
            sha, time.perf_counter() - t0, "unavailable", str(e), reference,
        )
    except OcrError as e:
        return _failure(
            opaque, backend, preprocess_variant, provenance,
            sha, time.perf_counter() - t0, "ocr_error", str(e), reference,
        )
    except Exception as e:  # never crash a suite on one file
        return _failure(
            opaque, backend, preprocess_variant, provenance,
            sha, time.perf_counter() - t0, type(e).__name__, str(e), reference,
        )


def _failure(case_id, backend, variant, provenance, sha, latency, kind, msg,
             reference: dict[str, str | None]) -> dict:
    # Retain expected presence so failures count as misses (FN), not exclusions.
    fields = {}
    for k in CRITICAL_FIELDS:
        exp = reference.get(k)
        exp_present = exp is not None and str(exp).strip() != ""
        fields[k] = {
            "field": k, "expected_present": exp_present,
            "predicted_present": False, "exact_match": False,
            "false_positive": False, "extra_incorrect": False,
            "num_candidates": 0, "num_matching": 0, "num_nonmatching": 0,
            "has_conflict": False, "low_confidence": False, "methods": [],
            "expected_len": len(str(exp)) if exp_present else 0,
            "predicted_lens": [],
        }
    res = {
        "case_id": case_id,
        "backend": backend,
        "preprocess_variant": variant,
        "provenance": provenance,
        "privacy": {
            "redacted": True,
            "contains_raw_text": False,
            "contains_applicant_values": False,
            "case_id_opaque_default": True,
        },
        "input_sha256": (sha or "")[:16],
        "num_pages": 0,
        "ocr_stage": {"per_page": [], "label_present": {k: None for k in CRITICAL_FIELDS}},
        "latency_sec": round(latency, 4),
        "ocr_latency_sec": round(latency, 4),
        "parse_latency_sec": 0.0,
        "failed": True,
        "failure_kind": kind,
        "failure_hint": msg[:200],
        "fields": fields,
    }
    _assert_redacted(res)
    return res


def summarize(results: list[dict]) -> dict[str, Any]:
    """Suite-level metrics over redacted per-case results.

    See module docstring for denominator definitions. Coverage uses
    C/E (proposed-among-expected) so it can never exceed 1. Precision
    uses clean_TP/P so extra candidates reduce it.
    """
    n = len(results)
    failed = sum(1 for r in results if r.get("failed"))
    lats = sorted([float(r.get("latency_sec", 0.0)) for r in results])
    per_field: dict[str, dict] = {}
    for k in CRITICAL_FIELDS:
        exp = sum(1 for r in results if r["fields"][k]["expected_present"])
        pred = sum(1 for r in results if r["fields"][k]["predicted_present"])
        hit = sum(1 for r in results if r["fields"][k]["exact_match"])
        covered = sum(1 for r in results
                      if r["fields"][k]["expected_present"] and r["fields"][k]["predicted_present"])
        clean = sum(1 for r in results
                    if r["fields"][k]["exact_match"] and not r["fields"][k]["extra_incorrect"])
        fp = sum(1 for r in results if r["fields"][k]["false_positive"])
        per_field[k] = {
            "expected_present": exp,
            "predicted_present": pred,
            "exact_match": hit,
            "clean_hits": clean,
            "cases_with_extra_incorrect": sum(
                1 for r in results if r["fields"][k]["extra_incorrect"]),
            "exact_match_rate_on_expected": round(hit / exp, 4) if exp else None,
            "recall": round(hit / exp, 4) if exp else None,
            "coverage_on_expected": round(covered / exp, 4) if exp else None,
            "precision": round(clean / pred, 4) if pred else None,
            "false_positives": fp,
        }
    total_exp = sum(v["expected_present"] for v in per_field.values())
    total_hit = sum(v["exact_match"] for v in per_field.values())
    total_clean = sum(v["clean_hits"] for v in per_field.values())
    total_pred = sum(v["predicted_present"] for v in per_field.values())
    total_covered = sum(
        round(v["coverage_on_expected"] * v["expected_present"], 4)
        if v["coverage_on_expected"] is not None else 0
        for v in per_field.values()
    )
    return {
        "num_cases": n,
        "failures": failed,
        "backend_failure_rate": round(failed / n, 4) if n else 0.0,
        "metric_definitions": {
            "recall": "TP/E; E=expected-present cases",
            "exact_match_rate_on_expected": "alias of recall",
            "coverage_on_expected": "C/E; C=expected+predicted; never > 1",
            "precision": "clean_TP/P; clean=match with no extra candidates",
            "false_positives": "cases with any spurious candidate",
        },
        "latency_sec": {
            "mean": round(sum(lats) / len(lats), 4) if lats else 0.0,
            "p95": round(lats[min(len(lats) - 1, int(-(-95 * len(lats) // 100)) - 1)], 4) if lats else 0.0,
            "max": round(max(lats), 4) if lats else 0.0,
        },
        "per_field": per_field,
        "overall_exact_match_rate": round(total_hit / total_exp, 4) if total_exp else None,
        "overall_recall": round(total_hit / total_exp, 4) if total_exp else None,
        "overall_coverage": round(total_covered / total_exp, 4) if total_exp else None,
        "overall_precision": round(total_clean / total_pred, 4) if total_pred else None,
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


def resolve_out_dir(out: Path | str) -> Path:
    """Enforce ignored-dir output: repo writes allowed only under data/."""
    from ..config import REPO_ROOT

    p = Path(out)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    else:
        p = p.resolve()
    try:
        rel = p.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return p  # outside repo (e.g. %TEMP%): allowed
    data_root = (REPO_ROOT / "data").resolve()
    try:
        p.relative_to(data_root)
        return p
    except ValueError:
        raise ValueError(
            f"refusing to write reports inside repo but outside gitignored data/: {p}. "
            "Use data/benchmark/ (default, gitignored) or a path outside the repo."
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Local-only FormFlow OCR baseline benchmark (redacted). "
        "Reference JSON with personal data must live outside Git "
        "(e.g. data/benchmark/, gitignored). Raw text/values never appear "
        "in reports/stdout; use --write-diagnostics for a separate local "
        "raw bundle when inspection is needed.")
    ap.add_argument("--input", required=True, help="local document path (read-only)")
    ap.add_argument("--reference", required=True, help="local reference JSON path (outside Git)")
    ap.add_argument("--out", default="data/benchmark", help="output dir (default gitignored data/benchmark)")
    ap.add_argument("--backend", default="tesseract", choices=list(SUPPORTED_BACKENDS))
    ap.add_argument("--preprocess-variant", default="baseline",
                    choices=list(SUPPORTED_PREPROCESS_VARIANTS))
    ap.add_argument("--case-id", default=None,
                    help="optional explicit ID (may be identifying; default is opaque case-<sha8>)")
    ap.add_argument("--write-diagnostics", action="store_true",
                    help="also write a separate RAW diagnostic bundle (contains OCR text/values; keep local, never commit)")
    args = ap.parse_args(argv)

    from ..config import load_config

    cfg = load_config()
    src = Path(args.input)
    if not src.is_file():
        print(f"input not found: {src}", file=sys.stderr)
        return 2
    try:
        outdir = resolve_out_dir(Path(args.out))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    ref = load_reference(Path(args.reference))
    res = benchmark_file(src, ref, cfg, backend=args.backend,
                         preprocess_variant=args.preprocess_variant,
                         case_id=args.case_id)
    summary = summarize([res])
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    opaque = re.sub(r"[^A-Za-z0-9_-]", "_", str(res["case_id"]))[:40]
    out = outdir / f"benchmark_{stamp}_{opaque}.json"
    out.write_text(json.dumps({"results": [res], "summary": summary},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    if args.write_diagnostics:
        try:
            raw = extract_pages_raw(src, cfg)
            from .fields import extract_candidates, load_field_rules

            cands = extract_candidates(raw["pages"], load_field_rules(cfg))
            write_diagnostic_bundle(outdir, str(res["case_id"]), raw["pages"], cands,
                                    res.get("provenance"))
        except Exception as e:
            print(f"diagnostic bundle failed: {e}", file=sys.stderr)
    # Redacted stdout: metrics only, no values/text/filenames.
    print(json.dumps({"out": str(out), "summary": summary,
                      "failed": res["failed"],
                      "failure_kind": res.get("failure_kind"),
                      "case_id": res["case_id"],
                      "per_field_match": {k: v["exact_match"] for k, v in res["fields"].items()},
                      "per_field_ocr_label_present": {
                          k: v for k, v in res["ocr_stage"]["label_present"].items()}},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
