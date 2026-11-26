"""Authenticated local API (Phase 6.1 hardened).

Endpoints:
    GET  /api/cases                         — minimal list of selectable cases
    GET  /api/cases/<case_id>/fill-packet   — validated fill packet
    POST /api/cases/<case_id>/events        — record a fill/copy event

All endpoints require a valid ``Authorization: Bearer <token>`` header.
Responses never contain applicant values in error messages, secrets, or
internal tracebacks.  CORS is deny-by-default; Phase 7 will add the
extension's origin explicitly.
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_file

from ..db import get_document, utcnow_iso
from . import open_db
from ..services.api_auth import check_origin, ensure_token, require_api_auth
from ..services.catalog import get_catalog
from ..services.fill_packet import (
    DEFAULT_WORKFLOW,
    WORKFLOWS,
    PacketError,
    build_fill_packet,
)
from ..services.filters import resolve_within, sanitize_filename
from ..services.prepare import mime_for
from ..services.profiles import load_profiles
from ..services.review import REVIEW_FIELDS

api_bp = Blueprint("api", __name__, url_prefix="/api")

# Maximum POST body size for event payloads (4 KB).
MAX_EVENT_BODY = 4096

# Allowed event types for the events endpoint.
ALLOWED_EVENT_TYPES = {
    "copy_attempt", "fill_attempt", "restore_attempt",
    "autofill_attempt", "file_attach_attempt",
}


# Supported internal field keys for event reporting (extensible via catalogue).
class SupportedFieldKeys(set):
    def __contains__(self, item: object) -> bool:
        if super().__contains__(item):
            return True
        if isinstance(item, str) and get_catalog().get(item) is not None:
            return True
        return False


SUPPORTED_FIELD_KEYS = SupportedFieldKeys(REVIEW_FIELDS)

# Permitted payload properties for the events endpoint.
ALLOWED_PAYLOAD_KEYS = {"event_type", "review_version", "field_key", "detail"}


@api_bp.after_request
def _add_cors_headers(response):
    """Add CORS headers for validated origins."""
    origin = request.headers.get("Origin")
    if origin and check_origin():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "86400"
        response.headers["Vary"] = "Origin"
    return response


@api_bp.before_request
def _handle_cors_preflight():
    """Intercept CORS preflight requests before automatic route dispatch."""
    if request.method == "OPTIONS":
        if not check_origin():
            return jsonify({"error": "origin_not_allowed"}), 403
        return "", 204


@api_bp.record_once
def _setup_token(state):
    """Ensure the API token file exists when the blueprint is registered."""
    with state.app.app_context():
        ensure_token()


def _db():
    conn, _ = open_db()
    return conn


@api_bp.get("/cases")
@require_api_auth
def list_cases():
    """Minimal list of cases suitable for a case-picker dropdown."""
    conn = _db()
    if conn is None:
        return jsonify({"cases": []})
    try:
        cfg = current_app.config["FORMFLOW"]
        auto_ready = bool(getattr(cfg, "auto_ready_fill", True))
        rows = conn.execute(
            "SELECT case_id, status, reviewed_at, review_version "
            "FROM cases ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        cases = [
            {
                "case_id": r["case_id"],
                "status": r["status"],
                "reviewed": r["status"] == "reviewed" and bool(r["reviewed_at"]),
                "auto_ready": auto_ready,
                "review_version": r["review_version"],
                "reviewed_at": r["reviewed_at"],
            }
            for r in rows
        ]
        return jsonify({"cases": cases})
    finally:
        conn.close()


@api_bp.get("/cases/<case_id>/fill-packet")
@require_api_auth
def fill_packet(case_id: str):
    """Return the fill packet (auto-ready high-confidence OCR or operator-reviewed)."""
    workflow = request.args.get("workflow", DEFAULT_WORKFLOW)
    conn = _db()
    if conn is None:
        return jsonify({"error": "service_unavailable"}), 503
    try:
        cfg = current_app.config["FORMFLOW"]
        result = build_fill_packet(
            conn, case_id, workflow,
            auto_ready=bool(getattr(cfg, "auto_ready_fill", True)),
            min_confidence=float(getattr(cfg, "auto_fill_min_confidence", 0.70)),
        )
        if isinstance(result, PacketError):
            return jsonify(result.to_dict()), result.http_status
        return jsonify(result)
    finally:
        conn.close()


def _serialize_ready_doc(row, attached: bool, profiles: dict | None = None) -> dict:
    keys = set(row.keys())
    prep = Path(row["prepared_path"]) if row["prepared_path"] else None
    exp_raw = row["inbox_export_path"] if "inbox_export_path" in keys else None
    inbox_name = Path(exp_raw).name if exp_raw else None
    filename = inbox_name or (prep.name if prep else sanitize_filename(row["orig_filename"] or "document.jpg"))
    ext = prep.suffix.lower() if prep else Path(filename).suffix.lower()
    prof_key = (row["profile_name"] or row["doctype"] or "").lower()
    label = None
    if isinstance(profiles, dict) and prof_key in profiles and isinstance(profiles[prof_key], dict):
        label = profiles[prof_key].get("label")
    return {
        "id": row["id"],
        "doctype": row["doctype"],
        "profile_name": row["profile_name"] or row["doctype"],
        "label": label,
        "filename": filename,
        "inbox_filename": inbox_name,
        "byte_size": row["byte_size_prepared"],
        "mime": mime_for(ext, prepared=True),
        "attached_to_case": attached,
    }


@api_bp.get("/cases/<case_id>/documents")
@require_api_auth
def list_case_ready_documents(case_id: str):
    """Return upload-ready prepared documents for a reviewed case plus unassigned photo/signature assets."""
    conn = _db()
    if conn is None:
        return jsonify({"error": "service_unavailable"}), 503
    try:
        cfg = current_app.config["FORMFLOW"]
        case = conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if case is None:
            return jsonify({"error": "case_not_found"}), 404
        if not getattr(cfg, "auto_ready_fill", True) and (case["status"] != "reviewed" or not case["reviewed_at"]):
            return jsonify({"error": "case_not_reviewed"}), 409

        case_rows = conn.execute(
            """SELECT * FROM documents
               WHERE case_id = ? AND status = 'ready' AND prepared_path IS NOT NULL
               ORDER BY id DESC""",
            (case["id"],),
        ).fetchall()
        # Standalone passphotos & signatures carry no OCR text and may not yet be
        # manually linked to a case; expose recent ready ones for 1-click upload.
        asset_rows = conn.execute(
            """SELECT * FROM documents
               WHERE case_id IS NULL AND status = 'ready'
                 AND prepared_path IS NOT NULL
                 AND doctype IN ('photo', 'signature')
               ORDER BY id DESC LIMIT 10""",
        ).fetchall()

        active_profiles, _ = load_profiles(cfg)
        docs = []
        for r in case_rows:
            try:
                p = resolve_within(Path(r["prepared_path"]), cfg.data_dir)
                if p.is_file():
                    docs.append(_serialize_ready_doc(r, attached=True, profiles=active_profiles))
            except ValueError:
                continue
        for r in asset_rows:
            try:
                p = resolve_within(Path(r["prepared_path"]), cfg.data_dir)
                if p.is_file():
                    docs.append(_serialize_ready_doc(r, attached=False, profiles=active_profiles))
            except ValueError:
                continue
        return jsonify({"case_id": case["case_id"], "documents": docs})
    finally:
        conn.close()


@api_bp.get("/cases/<case_id>/documents/<int:doc_id>/prepared")
@require_api_auth
def fetch_case_prepared_document(case_id: str, doc_id: int):
    """Serve prepared bytes for a reviewed case's document or unassigned photo/signature asset."""
    conn = _db()
    if conn is None:
        return jsonify({"error": "service_unavailable"}), 503
    try:
        cfg = current_app.config["FORMFLOW"]
        case = conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if case is None:
            return jsonify({"error": "case_not_found"}), 404
        if not getattr(cfg, "auto_ready_fill", True) and (case["status"] != "reviewed" or not case["reviewed_at"]):
            return jsonify({"error": "case_not_reviewed"}), 409

        doc = get_document(conn, doc_id)
        if doc is None or doc["status"] != "ready" or not doc["prepared_path"]:
            return jsonify({"error": "document_not_found"}), 404

        allowed = (doc["case_id"] == case["id"]) or (
            doc["case_id"] is None and doc["doctype"] in ("photo", "signature")
        )
        if not allowed:
            return jsonify({"error": "document_not_in_case"}), 403

        try:
            path = resolve_within(Path(doc["prepared_path"]), cfg.data_dir)
        except ValueError:
            return jsonify({"error": "document_not_found"}), 404
        if not path.is_file():
            return jsonify({"error": "document_not_found"}), 404

        keys = set(doc.keys())
        exp_raw = doc["inbox_export_path"] if "inbox_export_path" in keys else None
        dl_name = Path(exp_raw).name if exp_raw else path.name
        resp = send_file(
            path,
            mimetype=mime_for(path.suffix, prepared=True),
            as_attachment=False,
            download_name=sanitize_filename(dl_name),
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp
    finally:
        conn.close()


@api_bp.post("/cases/<case_id>/events")
@require_api_auth
def record_event(case_id: str):
    """Record a fill/copy/restore event from the future extension."""
    # Content-Type enforcement
    if not request.is_json:
        return jsonify({"error": "content_type_required"}), 415

    # Size guard
    if request.content_length and request.content_length > MAX_EVENT_BODY:
        return jsonify({"error": "payload_too_large"}), 413

    try:
        body = request.get_json(silent=True)
    except Exception:
        body = None
    if not isinstance(body, dict):
        return jsonify({"error": "invalid_payload"}), 400

    # Reject unexpected payload fields
    unexpected_keys = set(body.keys()) - ALLOWED_PAYLOAD_KEYS
    if unexpected_keys:
        return jsonify({"error": "unexpected_payload_fields"}), 400

    event_type = body.get("event_type")
    if not isinstance(event_type, str) or event_type not in ALLOWED_EVENT_TYPES:
        return jsonify({"error": "invalid_event_type"}), 400

    review_version = body.get("review_version")
    if review_version is None:
        return jsonify({"error": "review_version_required"}), 400
    if (
        isinstance(review_version, bool)
        or not isinstance(review_version, int)
        or review_version <= 0
    ):
        return jsonify({"error": "invalid_review_version"}), 400

    field_key = body.get("field_key")
    if event_type == "copy_attempt":
        # copy_attempt requires a supported field_key
        if not field_key or not isinstance(field_key, str) or field_key not in SUPPORTED_FIELD_KEYS:
            return jsonify({"error": "unsupported_field_key"}), 400
    else:
        # fill_attempt and restore_attempt may omit field_key, but if present must be supported
        if field_key is not None and field_key != "":
            if not isinstance(field_key, str) or field_key not in SUPPORTED_FIELD_KEYS:
                return jsonify({"error": "unsupported_field_key"}), 400

    detail = body.get("detail")
    if detail is not None:
        if not isinstance(detail, str) or len(detail) > 100:
            return jsonify({"error": "invalid_detail"}), 400

    conn = _db()
    if conn is None:
        return jsonify({"error": "service_unavailable"}), 503
    try:
        case = conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if case is None:
            return jsonify({"error": "case_not_found"}), 404

        # Reject stale review version
        if case["review_version"] != review_version:
            return jsonify({"error": "stale_review_version"}), 409

        # Reject if not currently reviewed (only when strict review mode is enforced)
        cfg = current_app.config["FORMFLOW"]
        if not getattr(cfg, "auto_ready_fill", True) and case["status"] != "reviewed":
            return jsonify({"error": "case_not_reviewed"}), 409

        # Record the event — only sanitized, minimal structured metadata is stored.
        detail_obj = {
            "event_type": event_type,
            "review_version": review_version,
        }
        if field_key:
            detail_obj["field_key"] = field_key

        conn.execute(
            "INSERT INTO case_events (case_id, kind, detail, created_at) "
            "VALUES (?, ?, ?, ?)",
            (case["id"], f"api_{event_type}", json.dumps(detail_obj), utcnow_iso()),
        )
        conn.commit()
        return jsonify({"ok": True}), 201
    finally:
        conn.close()
