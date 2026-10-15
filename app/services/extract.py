"""Phase 4 orchestrator: OCR -> parse -> match, one document at a time.

Never raises: every outcome lands on the document row (ocr_status /
ocr_error) and extracted-field rows, so the worker always finishes with a
traceable state. Raw page text goes to a gitignored JSON sidecar for
review/debugging only -- never to logs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from PIL import Image

from ..config import FormFlowConfig
from ..db import connect, get_document, init_db, update_document
from .fields import extract_candidates, load_field_rules
from .match import attach_or_provisional
from .ocr import OcrError, OcrUnavailableError, extract_document_text

SKIP_DOCTYPES = {"photo", "signature"}


def sidecar_path(cfg: FormFlowConfig, document_id: int) -> Path:
    d = cfg.data_dir / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"doc_{document_id}.json"


def run_extraction(
    document_id: int,
    cfg: FormFlowConfig,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
) -> dict:
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        doc = get_document(conn, document_id)
        if doc is None:
            raise ValueError(f"unknown document: {document_id}")
        keys = set(doc.keys())
        ocr_status = doc["ocr_status"] if "ocr_status" in keys else "pending"
        if ocr_status in ("text", "ocr", "mixed", "done", "skipped"):
            return dict(doc)  # idempotent: never re-extract finished work
        from ..db import list_document_fields

        if list_document_fields(conn, document_id):
            # Fields already stored (e.g. provisional case made earlier):
            # reruns must not duplicate rows or spawn extra cases.
            return dict(doc)

        if (doc["doctype"] or "unknown") in SKIP_DOCTYPES:
            return dict(update_document(
                conn, document_id, ocr_status="skipped",
                ocr_error=("identity photo/signature carries no readable personal "
                           "information; manual assignment required"),
            ))

        src = Path(doc["orig_path"] or "")
        if not src.exists():
            return dict(update_document(
                conn, document_id, ocr_status="failed",
                ocr_error="staged original not found; cannot extract"))

        rules = load_field_rules(cfg)
        try:
            result = extract_document_text(
                src, cfg, dpi=int(rules.get("dpi", 300)),
                lang=str(rules.get("lang", "eng")),
                min_page_chars=int(rules.get("min_page_chars", 30)),
                binarize=bool(rules.get("binarize", False)),
                ocr_fn=ocr_fn,
            )
        except OcrUnavailableError as e:
            return dict(update_document(
                conn, document_id, ocr_status="unavailable", ocr_error=str(e)))
        except OcrError as e:
            return dict(update_document(
                conn, document_id, ocr_status="failed", ocr_error=str(e)))

        pages = result["pages"]
        sidecar_path(cfg, document_id).write_text(
            json.dumps({"document_id": document_id,
                        "job_id": doc["intake_job_id"],
                        "source": result["source"],
                        "pages": [{"page": p["page"], "method": p["method"],
                                   "chars": len(p["text"]), "text": p["text"]}
                                  for p in pages]},
                       ensure_ascii=False),
            encoding="utf-8",
        )

        candidates = extract_candidates(pages, rules)
        if not candidates:
            return dict(update_document(
                conn, document_id, ocr_status="needs_attention",
                ocr_error="no candidate fields found; operator review required"))

        case, outcome = attach_or_provisional(conn, document_id, candidates)
        if outcome == "provisional":
            status, msg = "needs_attention", (
                f"provisional case {case['case_id']} created; "
                "needs operator assignment/merge")
        elif outcome == "attached_with_conflict":
            status, msg = "needs_attention", (
                f"attached to {case['case_id']} but new values disagree; operator review required")
        else:
            status, msg = result["source"], f"attached to {case['case_id']}"
        return dict(update_document(
            conn, document_id, case_id=case["id"],
            ocr_status=status, ocr_error=None if outcome == "attached" else msg))
    finally:
        try:
            conn.close()
        except Exception:
            pass
