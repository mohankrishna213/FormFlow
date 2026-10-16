"""Authenticated local API (Phase 6).

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

from flask import Blueprint, jsonify, request

from ..db import connect, utcnow_iso
from ..services.api_auth import ensure_token, require_api_auth
from ..services.fill_packet import (
    DEFAULT_WORKFLOW,
    WORKFLOWS,
    PacketError,
    build_fill_packet,
)

api_bp = Blueprint("api", __name__, url_prefix="/api")

# Maximum POST body size for event payloads (4 KB).
MAX_EVENT_BODY = 4096

# Allowed event types for the events endpoint.
ALLOWED_EVENT_TYPES = {"copy_attempt", "fill_attempt", "restore_attempt"}


@api_bp.record_once
def _setup_token(state):
    """Ensure the API token file exists when the blueprint is registered."""
    with state.app.app_context():
        ensure_token()


def _db():
    from flask import current_app
    cfg = current_app.config["FORMFLOW"]
    if str(cfg.db_path) == ":memory:":
        return None
    return connect(cfg.db_path)


@api_bp.get("/cases")
@require_api_auth
def list_cases():
    """Minimal list of cases suitable for a case-picker dropdown."""
    conn = _db()
    if conn is None:
        return jsonify({"cases": []})
    try:
        rows = conn.execute(
            "SELECT case_id, status, reviewed_at, review_version "
            "FROM cases ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        cases = [
            {
                "case_id": r["case_id"],
                "status": r["status"],
                "reviewed": r["status"] == "reviewed" and bool(r["reviewed_at"]),
            }
            for r in rows
        ]
        return jsonify({"cases": cases})
    finally:
        conn.close()


@api_bp.get("/cases/<case_id>/fill-packet")
@require_api_auth
def fill_packet(case_id: str):
    """Return the fill packet for a reviewed case."""
    workflow = request.args.get("workflow", DEFAULT_WORKFLOW)
    conn = _db()
    if conn is None:
        return jsonify({"error": "service_unavailable"}), 503
    try:
        result = build_fill_packet(conn, case_id, workflow)
        if isinstance(result, PacketError):
            return jsonify(result.to_dict()), result.http_status
        return jsonify(result)
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

    event_type = body.get("event_type", "")
    if event_type not in ALLOWED_EVENT_TYPES:
        return jsonify({"error": "invalid_event_type"}), 400

    review_version = body.get("review_version")
    if review_version is None or not isinstance(review_version, int):
        return jsonify({"error": "review_version_required"}), 400

    field_key = body.get("field_key", "")
    if not isinstance(field_key, str) or len(field_key) > 50:
        return jsonify({"error": "invalid_field_key"}), 400

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

        # Reject if not currently reviewed
        if case["status"] != "reviewed":
            return jsonify({"error": "case_not_reviewed"}), 409

        # Record the event — no applicant values are logged.
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
