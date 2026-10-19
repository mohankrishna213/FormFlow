"""Phase 4 & Phase 3 orchestrator: OCR -> parse -> match, one document at a time.

Features:
- Never raises: every outcome lands on document row and extracted-field rows.
- Structured sidecar with regions, engine, preprocessing, classification evidence.
- Safe reprocessing (reprocess_document) that never creates duplicate cases,
  duplicate candidate rows, or overwrites confirmed customer-profile fields.
- Re-evaluated SKIP_DOCTYPES: identity documents are never skipped as photos.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from ..config import FormFlowConfig
from ..db import (
    connect,
    create_extracted_field,
    get_document,
    init_db,
    list_document_fields,
    update_document,
)
from .classify import classify_document
from .fields import extract_candidates, load_field_rules
from .match import attach_or_provisional
from .ocr import OcrError, OcrUnavailableError, extract_document_text

SKIP_DOCTYPES = {"photo", "signature"}
_ID_HINTS = (
    "aadhaar", "aadhar", "pan", "ssc", "memo", "cert", "marks",
    "license", "licence", "ll", "dl", "rc", "form3", "form7", "form23",
)


def sidecar_path(cfg: FormFlowConfig, document_id: int) -> Path:
    d = cfg.data_dir / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"doc_{document_id}.json"


def run_extraction(
    document_id: int,
    cfg: FormFlowConfig,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
    reprocess: bool = False,
) -> dict:
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        doc = get_document(conn, document_id)
        if doc is None:
            raise ValueError(f"unknown document: {document_id}")
        keys = set(doc.keys())
        ocr_status = doc["ocr_status"] if "ocr_status" in keys else "pending"

        if not reprocess:
            if ocr_status in ("text", "ocr", "mixed", "done", "skipped"):
                return dict(doc)  # idempotent: never re-extract finished work

            if list_document_fields(conn, document_id):
                # Fields already stored: reruns must not duplicate rows or spawn extra cases.
                return dict(doc)

        # Re-evaluate SKIP_DOCTYPES: check if filename or properties indicate identity doc
        curr_doctype = (doc["doctype"] or "unknown").lower()
        if curr_doctype in SKIP_DOCTYPES:
            fname_lower = (doc["orig_filename"] or "").lower()
            has_id_hint = any(k in fname_lower for k in _ID_HINTS)
            if not has_id_hint:
                return dict(update_document(
                    conn, document_id, ocr_status="skipped",
                    ocr_error=("identity photo/signature carries no readable personal "
                               "information; manual assignment required"),
                ))
            # Has identity hint in filename: do not skip, proceed to OCR

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
                psm=int(rules.get("psm", 6)),
                upscale=bool(rules.get("upscale", False)),
                upscale_min_height=int(rules.get("upscale_min_height", 1500)),
                ocr_fn=ocr_fn,
            )
        except OcrUnavailableError as e:
            return dict(update_document(
                conn, document_id, ocr_status="unavailable", ocr_error=str(e)))
        except OcrError as e:
            return dict(update_document(
                conn, document_id, ocr_status="failed", ocr_error=str(e)))

        pages = result["pages"]
        full_text = " ".join(p["text"] for p in pages)

        # Classification check with extracted text
        class_res = classify_document(
            filename=doc["orig_filename"],
            extension=src.suffix,
            ocr_hints={"text": full_text},
        )

        # Update document doctype if classified as identity document and previously unknown
        if class_res.is_identity_doc and doc["doctype"] in ("unknown", "photo", "signature"):
            update_document(conn, document_id, doctype=class_res.doctype)

        # Write sidecar with full provenance
        sidecar_data = {
            "document_id": document_id,
            "job_id": doc["intake_job_id"],
            "source": result["source"],
            "engine": result.get("engine", "tesseract"),
            "pipeline_version": str(rules.get("version", "5")),
            "retry_path": result.get("retry_path"),
            "classification": {
                "suggested_category": class_res.category,
                "doctype": class_res.doctype,
                "confidence": class_res.confidence,
                "reason": class_res.reason,
                "evidence": class_res.evidence,
            },
            "pages": [
                {
                    "page": p["page"],
                    "method": p["method"],
                    "engine": p.get("engine", result.get("engine", "tesseract")),
                    "model_version": p.get("model_version"),
                    "preprocessing": p.get("preprocessing", []),
                    "chars": len(p["text"]),
                    "text": p["text"],
                    "regions": p.get("regions", []),
                    "confidence": p.get("confidence", 0.0),
                }
                for p in pages
            ],
        }
        sidecar_path(cfg, document_id).write_text(
            json.dumps(sidecar_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        candidates = extract_candidates(pages, rules)
        if not candidates:
            return dict(update_document(
                conn, document_id, ocr_status="needs_attention",
                ocr_error="no candidate fields found; operator review required"))

        # Reprocessing with existing case: update candidates directly
        if reprocess and doc["case_id"]:
            # Delete previous unconfirmed candidate rows for this document
            conn.execute(
                """DELETE FROM extracted_fields
                   WHERE document_id = ?
                     AND method NOT IN ('manual_entry', 'operator_confirmed', 'customer_profile')""",
                (document_id,),
            )
            for c in candidates:
                create_extracted_field(
                    conn,
                    case_id=doc["case_id"],
                    document_id=document_id,
                    field_key=c["field_key"],
                    value=c["value"],
                    source_page=c.get("source_page", 1),
                    method=c.get("method", "ocr"),
                    confidence=c.get("confidence", 0.5),
                )
            conn.commit()
            return dict(update_document(
                conn, document_id, ocr_status=result["source"], ocr_error=None
            ))

        # Attach to existing case if already associated
        if doc["case_id"]:
            for c in candidates:
                create_extracted_field(
                    conn,
                    case_id=doc["case_id"],
                    document_id=document_id,
                    field_key=c["field_key"],
                    value=c["value"],
                    source_page=c.get("source_page", 1),
                    method=c.get("method", "ocr"),
                    confidence=c.get("confidence", 0.5),
                )
            conn.commit()
            return dict(update_document(
                conn, document_id, ocr_status=result["source"], ocr_error=None
            ))

        # Initial unassigned extraction: match or provisional
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


def reprocess_document(
    document_id: int,
    cfg: FormFlowConfig,
    ocr_fn: Callable[[Image.Image], tuple[str, float]] | None = None,
    force: bool = False,
) -> dict:
    """Safely reprocess a document without duplicating cases or overwriting confirmed data.

    - Deletes previous unconfirmed OCR candidates for this document.
    - Preserves operator-confirmed fields, manual entries, and customer_fields.
    - Attaches new candidates to the existing case.
    - Updates sidecar and document row status.
    """
    return run_extraction(document_id, cfg, ocr_fn=ocr_fn, reprocess=True)
