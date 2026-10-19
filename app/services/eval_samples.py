"""Local privacy-safe evaluation runner for local sample images (Phase 3).

Evaluates a directory of privately supplied sample images without uploading
any data or leaking PII into ordinary console logs or reports.
Reports:
- Processing latency (per image and summary statistics)
- Document classification results and confidence
- Extracted field keys, candidate counts, and extraction confidence
- Redacted value lengths (never actual personal values)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from PIL import Image

from ..config import load_config
from .classify import classify_document
from .fields import extract_candidates, load_field_rules
from .ocr import extract_document_text


def _mask_value(key: str, val: str) -> str:
    """Return a privacy-safe redacted representation showing type and length only."""
    length = len(val)
    return f"[REDACTED {key} ({length} chars)]"


def evaluate_directory(
    directory: Path | str,
    cfg=None,
    verbose: bool = True,
    backend: str = "tesseract",
) -> dict:
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise FileNotFoundError(f"Directory not found: {directory}")

    if cfg is None:
        cfg = load_config()

    rules = load_field_rules(cfg)
    image_exts = {".png", ".jpg", ".jpeg", ".pdf"}
    files = sorted([f for f in dir_path.iterdir() if f.suffix.lower() in image_exts])

    results = []
    latencies = []
    category_counts: dict[str, int] = {}
    field_counts: dict[str, int] = {}

    if verbose:
        print(f"\n{'='*70}")
        print(f"FormFlow Phase 3 Local Evaluation: {dir_path.name} ({len(files)} files, backend={backend})")
        print(f"{'='*70}")

    for idx, f in enumerate(files, 1):
        t0 = time.perf_counter()
        doc_error = None
        extracted_fields = []
        classification = None
        engine_used = "unknown"
        transforms_used = []

        try:
            res = extract_document_text(f, cfg, selective_retry=True, backend=backend)
            pages = res.get("pages", [])
            engine_used = res.get("engine", backend)
            if pages:
                transforms_used = pages[0].get("preprocessing", [])
            all_text = " ".join(p.get("text", "") for p in pages)

            # Classify
            classification = classify_document(
                filename=f.name,
                extension=f.suffix,
                ocr_hints={"text": all_text},
            )

            # Field extraction
            candidates = extract_candidates(pages, rules)
            extracted_fields = candidates

        except Exception as e:
            doc_error = str(e)

        elapsed = time.perf_counter() - t0
        latencies.append(elapsed)

        cat = classification.category if classification else "error"
        category_counts[cat] = category_counts.get(cat, 0) + 1

        fields_summary = {}
        for c in extracted_fields:
            k = c["field_key"]
            field_counts[k] = field_counts.get(k, 0) + 1
            fields_summary.setdefault(k, []).append({
                "masked": _mask_value(k, c["value"]),
                "confidence": c.get("confidence", 0.0),
                "method": c.get("method", "ocr"),
            })

        doc_res = {
            "filename": f.name,
            "latency_sec": round(elapsed, 3),
            "category": cat,
            "engine": engine_used,
            "preprocessing": transforms_used,
            "field_keys": sorted(fields_summary.keys()),
            "fields_detail": fields_summary,
            "error": doc_error,
        }
        results.append(doc_res)

        if verbose:
            status = f"[{cat.upper()}]" if not doc_error else f"[ERROR: {doc_error[:30]}]"
            f_keys_str = ", ".join(sorted(fields_summary.keys())) or "no fields found"
            print(f"{idx:2d}. {f.name[:32]:<32} | {status:<18} | {elapsed:5.2f}s | {engine_used} | fields: {f_keys_str}")

    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    summary = {
        "total_files": len(files),
        "mean_latency_sec": round(avg_lat, 3),
        "min_latency_sec": round(min(latencies), 3) if latencies else 0.0,
        "max_latency_sec": round(max(latencies), 3) if latencies else 0.0,
        "categories": category_counts,
        "field_yields": field_counts,
        "results": results,
    }

    if verbose:
        print(f"\n{'-'*70}")
        print("SUMMARY EVALUATION METRICS:")
        print(f"Total files evaluated : {len(files)}")
        print(f"Latency range         : min {summary['min_latency_sec']}s, avg {summary['mean_latency_sec']}s, max {summary['max_latency_sec']}s")
        print("Categories identified :", ", ".join(f"{k}: {v}" for k, v in sorted(category_counts.items())))
        print("Fields extracted      :", ", ".join(f"{k}: {v}" for k, v in sorted(field_counts.items())))
        print(f"{'='*70}\n")

    return summary


def main():
    parser = argparse.ArgumentParser(description="Evaluate FormFlow OCR on local sample images without leaking PII.")
    parser.add_argument("directory", nargs="?", default=r"data\sample images", help="Path to sample images directory")
    parser.add_argument("--backend", choices=["tesseract", "paddleocr"], default="tesseract", help="OCR engine backend")
    args = parser.parse_args()
    evaluate_directory(args.directory, verbose=True, backend=args.backend)


if __name__ == "__main__":
    main()
