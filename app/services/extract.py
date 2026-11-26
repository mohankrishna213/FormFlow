"""Document extraction orchestrator: OCR -> classify -> parse fields -> case match.

Invariants:
- Never raises on expected document/OCR failures: outcomes are stored on the
  document row and in ``data/ocr/doc_<id>.json``.
- Safe reprocessing (``reprocess_document``) refreshes OCR candidates without
  duplicating cases or overwriting operator-confirmed / customer-profile values.
- Identity documents (Aadhaar, PAN, SSC, DL, LL, TR) are never skipped as photos.
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
from .match import attach_asset_to_burst_case, attach_or_provisional
from .ocr import OcrError, OcrUnavailableError, extract_document_text
from .prepare import sync_inbox_export
from .profiles import custom_classifier_rules, load_profiles

SKIP_DOCTYPES = {"photo", "signature"}
_ID_HINTS = (
    "aadhaar", "aadhar", "pan", "ssc", "memo", "cert", "marks",
    "license", "licence", "ll", "dl", "rc", "form3", "form7", "form23",
)
_IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def sidecar_path(cfg: FormFlowConfig, document_id: int) -> Path:
    d = cfg.data_dir / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"doc_{document_id}.json"


def _resolve_burst_context(conn, doc, cfg: FormFlowConfig) -> tuple[float, str | None]:
    """Return (batch_window_sec, arrival_timestamp) anchored to intake arrival time."""
    keys = set(doc.keys())
    arrival_ts: str | None = doc["created_at"] if "created_at" in keys else None
    if doc["intake_job_id"] is None:
        return 0.0, arrival_ts
    batch_window = float(getattr(cfg, "inbox_batch_window_sec", 0.0) or 0.0)
    jrow = conn.execute(
        "SELECT created_at FROM intake_jobs WHERE id = ?",
        (doc["intake_job_id"],),
    ).fetchone()
    if jrow and jrow["created_at"]:
        arrival_ts = jrow["created_at"]
    return batch_window, arrival_ts


def _try_visual_asset_prepare(
    conn,
    doc,
    src: Path,
    cfg: FormFlowConfig,
    batch_window: float,
    arrival_ts: str | None,
    *,
    allow_signature: bool = True,
    low_text_verified: bool = False,
) -> tuple[dict | None, str | None]:
    """Visually classify unlabeled phone uploads as photo (<50KB), signature (<50KB), or card.

    Returns ``(final_doc_row_if_skipped, export_label_override)``.
    """
    from .document_cv import classify_visual_image
    from .prepare import reprepare_document

    document_id = doc["id"]
    with Image.open(src) as _im:
        _im.load()
        vis_type, _, vis_reason = classify_visual_image(
            _im.copy(),
            allow_signature=allow_signature,
            low_text_verified=low_text_verified,
        )

    if vis_type in ("photo", "signature"):
        reprepare_document(document_id, cfg, doctype_hint=vis_type, auto_crop=True)
        if batch_window > 0.0 and not doc["case_id"]:
            attach_asset_to_burst_case(
                conn, document_id, vis_type, batch_window, reference_ts=arrival_ts
            )
            sync_inbox_export(conn, document_id, cfg, label_hint=vis_type)
        updated = update_document(
            conn, document_id,
            ocr_status="skipped",
            ocr_error=f"auto-detected {vis_type} ({vis_reason}); prepared <50 KB",
        )
        return dict(updated), vis_type

    return None, vis_type


def _reprepare_after_ocr(
    conn,
    doc,
    src: Path,
    cfg: FormFlowConfig,
    class_res,
    alnum_chars: int,
    detected_rot: int,
    batch_window: float,
    arrival_ts: str | None,
) -> tuple[dict | None, str | None]:
    """Re-crop/re-classify image documents using OCR clues and visual fallback.

    Returns ``(early_return_doc_if_visual_asset, export_label)``.
    """
    document_id = doc["id"]
    profiles, _ = load_profiles(cfg)
    export_label = (
        class_res.category
        if class_res.category not in ("unknown", "supporting_ll")
        else (doc["profile_name"] or doc["doctype"])
    )
    is_image = src.suffix.lower() in _IMAGE_EXTS
    is_prep_supported = is_image or src.suffix.lower() == ".pdf"

    if class_res.is_identity_doc and (
        doc["doctype"] in ("unknown", "photo", "signature")
        or (class_res.category in profiles and doc["profile_name"] != class_res.category)
        or detected_rot != 0
    ):
        update_document(conn, document_id, doctype=class_res.doctype)
        if is_prep_supported:
            try:
                from .prepare import reprepare_document

                target_profile = class_res.category if class_res.category in profiles else class_res.doctype
                reprepare_document(
                    document_id, cfg,
                    doctype_hint=target_profile,
                    rotate_deg=detected_rot if is_image else 0,
                    label_hint=class_res.category,
                )
                if target_profile != class_res.doctype:
                    update_document(conn, document_id, doctype=class_res.doctype)
            except Exception:
                pass
        return None, export_label

    if doc["doctype"] == "unknown" and is_image:
        try:
            from .prepare import reprepare_document

            if alnum_chars >= 35:
                reprepare_document(
                    document_id, cfg, doctype_hint="supporting_ll",
                    rotate_deg=detected_rot, auto_crop=True,
                )
                return None, "supporting_ll"

            skipped_doc, vis_type = _try_visual_asset_prepare(
                conn, doc, src, cfg, batch_window, arrival_ts,
                allow_signature=(alnum_chars < 15),
                low_text_verified=(alnum_chars < 15),
            )
            if skipped_doc is not None:
                return skipped_doc, vis_type
            if vis_type == "supporting_ll":
                reprepare_document(
                    document_id, cfg, doctype_hint="supporting_ll",
                    rotate_deg=detected_rot, auto_crop=True,
                )
                return None, "supporting_ll"
        except Exception:
            pass

    return None, export_label


def _write_ocr_sidecar(
    cfg: FormFlowConfig,
    document_id: int,
    doc,
    result: dict[str, Any],
    rules: dict[str, Any],
    class_res,
) -> None:
    """Persist structured OCR provenance and text under gitignored ``data/ocr/``."""
    pages = result["pages"]
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


def _store_candidates_for_case(
    conn,
    case_pk: int,
    document_id: int,
    candidates: list[dict[str, Any]],
    *,
    replace_existing: bool = False,
) -> None:
    """Write OCR candidate rows for an already-linked case without touching confirmed entries."""
    if replace_existing:
        conn.execute(
            """DELETE FROM extracted_fields
               WHERE document_id = ?
                 AND method NOT IN ('manual_entry', 'operator_confirmed', 'customer_profile')""",
            (document_id,),
        )
    for c in candidates:
        create_extracted_field(
            conn,
            case_id=case_pk,
            document_id=document_id,
            field_key=c["field_key"],
            value=c["value"],
            source_page=c.get("source_page", 1),
            method=c.get("method", "ocr"),
            confidence=c.get("confidence", 0.5),
        )
    conn.commit()


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
                return dict(doc)
            if list_document_fields(conn, document_id):
                return dict(doc)

        batch_window, arrival_ts = _resolve_burst_context(conn, doc, cfg)

        # Skip OCR for standalone photo/signature unless filename hints at an ID card
        curr_doctype = (doc["doctype"] or "unknown").lower()
        if curr_doctype in SKIP_DOCTYPES:
            fname_lower = (doc["orig_filename"] or "").lower()
            if not any(k in fname_lower for k in _ID_HINTS):
                if batch_window > 0.0 and not doc["case_id"]:
                    attach_asset_to_burst_case(
                        conn, document_id, curr_doctype, batch_window, reference_ts=arrival_ts
                    )
                    sync_inbox_export(conn, document_id, cfg, label_hint=curr_doctype)
                return dict(update_document(
                    conn, document_id, ocr_status="skipped",
                    ocr_error=(
                        "identity photo/signature carries no readable personal "
                        "information; manual assignment required"
                        if not batch_window else
                        f"{curr_doctype} grouped into active customer batch"
                    ),
                ))

        src = Path(doc["orig_path"] or "")
        if not src.exists():
            return dict(update_document(
                conn, document_id, ocr_status="failed",
                ocr_error="staged original not found; cannot extract"))

        rules = load_field_rules(cfg)
        try:
            result = extract_document_text(
                src, cfg,
                dpi=int(rules.get("dpi", 300)),
                lang=str(rules.get("lang", "eng")),
                min_page_chars=int(rules.get("min_page_chars", 30)),
                binarize=bool(rules.get("binarize", False)),
                psm=int(rules.get("psm", 6)),
                upscale=bool(rules.get("upscale", False)),
                upscale_min_height=int(rules.get("upscale_min_height", 1500)),
                ocr_fn=ocr_fn,
            )
        except OcrUnavailableError as e:
            if doc["doctype"] == "unknown" and src.suffix.lower() in _IMAGE_EXTS:
                try:
                    skipped_doc, _ = _try_visual_asset_prepare(
                        conn, doc, src, cfg, batch_window, arrival_ts,
                        allow_signature=False,
                        low_text_verified=False,
                    )
                    if skipped_doc is not None:
                        return skipped_doc
                except Exception:
                    pass
            return dict(update_document(
                conn, document_id, ocr_status="unavailable", ocr_error=str(e)))
        except OcrError as e:
            return dict(update_document(
                conn, document_id, ocr_status="failed", ocr_error=str(e)))

        pages = result["pages"]
        full_text = " ".join(p["text"] for p in pages)
        active_profiles, _ = load_profiles(cfg)

        # Offline QR code decode (Aadhaar Secure QR / XML QR / PAN QR) for exact high-confidence fields
        qr_candidates: list[dict[str, Any]] = []
        qr_category: str | None = None
        if src.suffix.lower() in _IMAGE_EXTS:
            try:
                from .qr_decode import extract_qr_candidates

                with Image.open(src) as _qim:
                    _qim.load()
                    qr_candidates, qr_category = extract_qr_candidates(_qim)
            except Exception:
                qr_candidates, qr_category = [], None

        ocr_hints_map: dict[str, Any] = {"text": full_text}
        if qr_category == "aadhaar":
            ocr_hints_map["qr"] = "unique identification authority aadhaar"
        elif qr_category == "pan":
            ocr_hints_map["qr"] = "income tax department permanent account number"

        class_res = classify_document(
            filename=doc["orig_filename"],
            extension=src.suffix,
            ocr_hints=ocr_hints_map,
            custom_rules=custom_classifier_rules(active_profiles),
        )
        alnum_chars = sum(1 for ch in full_text if ch.isalnum()) + (40 if qr_candidates else 0)
        detected_rot = int(pages[0].get("detected_rotation", 0)) if pages else 0

        early_doc, export_label = _reprepare_after_ocr(
            conn, doc, src, cfg, class_res, alnum_chars, detected_rot,
            batch_window, arrival_ts,
        )
        if early_doc is not None:
            return early_doc

        _write_ocr_sidecar(cfg, document_id, doc, result, rules, class_res)

        candidates = extract_candidates(pages, rules)
        if qr_candidates:
            seen_qr = {(c["field_key"], str(c["value"]).strip().lower()) for c in qr_candidates}
            merged = list(qr_candidates)
            for c in candidates:
                if (c["field_key"], str(c["value"]).strip().lower()) not in seen_qr:
                    merged.append(c)
            candidates = merged
        if not candidates:
            if batch_window > 0.0 and not doc["case_id"]:
                attach_asset_to_burst_case(
                    conn, document_id, export_label or doc["doctype"], batch_window,
                    reference_ts=arrival_ts,
                )
                sync_inbox_export(conn, document_id, cfg, label_hint=export_label)
            return dict(update_document(
                conn, document_id, ocr_status="needs_attention",
                ocr_error="no candidate fields found; operator review required"))

        if doc["case_id"]:
            _store_candidates_for_case(
                conn, doc["case_id"], document_id, candidates, replace_existing=reprocess
            )
            return dict(update_document(
                conn, document_id, ocr_status=result["source"], ocr_error=None
            ))

        case, outcome = attach_or_provisional(
            conn, document_id, candidates,
            batch_window_sec=batch_window,
            reference_ts=arrival_ts,
        )
        if outcome == "provisional":
            status, msg = "needs_attention", (
                f"provisional case {case['case_id']} created; "
                "needs operator assignment/merge")
        elif outcome == "attached_with_conflict":
            status, msg = "needs_attention", (
                f"attached to {case['case_id']} but new values disagree; operator review required")
        else:
            status, msg = result["source"], f"attached to {case['case_id']}"

        updated_doc = update_document(
            conn, document_id, case_id=case["id"],
            ocr_status=status, ocr_error=None if outcome == "attached" else msg)
        sync_inbox_export(conn, document_id, cfg, label_hint=export_label)
        return dict(get_document(conn, document_id) or updated_doc)
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
    """Safely reprocess a document without duplicating cases or overwriting confirmed data."""
    return run_extraction(document_id, cfg, ocr_fn=ocr_fn, reprocess=True)
