"""Phase 6 tests: API authentication, security, and endpoint behavior.

Covers token auth, origin restrictions, input validation, error shapes,
and endpoint contracts.  Synthetic data only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app import create_app
from app.db import (
    connect,
    create_case,
    create_extracted_field,
)
from app.services.api_auth import TOKEN_FILENAME
from app.services.fill_packet import DEFAULT_WORKFLOW
from app.services.review import save_review

CASE = "CASE-20261016-API"


def _app_and_token(tmp_cfg):
    """Create app, ensure token exists, return (app, token_str)."""
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    token_path = cfg.data_dir / TOKEN_FILENAME
    token = token_path.read_text(encoding="utf-8").strip()
    return app, token


def _seed_reviewed_case(tmp_cfg):
    """Seed + review a case, return (app, token, conn)."""
    app, token = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    create_case(conn, CASE, status="needs_attention")
    create_extracted_field(conn, 1, "full_name", "Ravi Kumar",
                          method="text:labeled", confidence=0.9)
    create_extracted_field(conn, 1, "dob", "1995-08-17",
                          method="text:labeled", confidence=0.9)
    values = {
        "full_name": "Ravi Kumar", "father_name": "",
        "dob": "1995-08-17", "address": "",
        "licence_no": "", "appl_number": "",
    }
    case, _, errors = save_review(conn, CASE, values)
    assert not errors
    conn.close()
    return app, token


def auth_headers(token, origin=None):
    h = {"Authorization": f"Bearer {token}"}
    if origin:
        h["Origin"] = origin
    return h


# ------------------------------------------------------------------
# Token authentication
# ------------------------------------------------------------------

def test_missing_token_rejected(tmp_cfg):
    app, _ = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases")
    assert r.status_code == 401
    assert r.get_json()["error"] == "authentication_required"


def test_invalid_token_rejected(tmp_cfg):
    app, _ = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_valid_token_accepted(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases", headers=auth_headers(token))
    assert r.status_code == 200


def test_token_not_in_error_response(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases", headers={"Authorization": "Bearer wrong"})
    body = r.get_data(as_text=True)
    assert token not in body
    assert "wrong" not in body


# ------------------------------------------------------------------
# Origin restrictions
# ------------------------------------------------------------------

def test_localhost_origin_accepted(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases",
                   headers=auth_headers(token, "http://127.0.0.1:5000"))
    assert r.status_code == 200


def test_external_origin_rejected(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases",
                   headers=auth_headers(token, "https://evil.example.com"))
    assert r.status_code == 403
    assert r.get_json()["error"] == "origin_not_allowed"


def test_no_origin_header_accepted(tmp_cfg):
    """Same-origin requests (no Origin header) are accepted."""
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases", headers=auth_headers(token))
    assert r.status_code == 200


# ------------------------------------------------------------------
# GET /api/cases
# ------------------------------------------------------------------

def test_cases_list_minimal(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases", headers=auth_headers(token))
    assert r.status_code == 200
    data = r.get_json()
    assert "cases" in data
    assert len(data["cases"]) >= 1
    case = data["cases"][0]
    # Only minimal fields — no applicant data
    assert "case_id" in case
    assert "status" in case
    assert "reviewed" in case
    for banned in ("full_name", "dob", "address", "appl_number"):
        assert banned not in case


# ------------------------------------------------------------------
# GET /api/cases/<id>/fill-packet
# ------------------------------------------------------------------

def test_fill_packet_valid(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.get(f"/api/cases/{CASE}/fill-packet",
                   headers=auth_headers(token))
    assert r.status_code == 200
    data = r.get_json()
    assert data["case_id"] == CASE
    assert "fields" in data
    assert "review_version" in data


def test_fill_packet_invalid_case(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases/NONEXISTENT/fill-packet",
                   headers=auth_headers(token))
    assert r.status_code == 404
    data = r.get_json()
    assert data["error"] == "case_not_found"
    # No applicant values in error
    assert "Ravi" not in json.dumps(data)


def test_fill_packet_unreviewed(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    create_case(conn, "CASE-UNREV", status="needs_attention")
    conn.close()
    client = app.test_client()
    r = client.get("/api/cases/CASE-UNREV/fill-packet",
                   headers=auth_headers(token))
    assert r.status_code == 409
    assert r.get_json()["error"] == "case_not_reviewed"


def test_fill_packet_invalid_workflow(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.get(f"/api/cases/{CASE}/fill-packet?workflow=bad",
                   headers=auth_headers(token))
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_workflow"


def test_fill_packet_no_secrets_in_response(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.get(f"/api/cases/{CASE}/fill-packet",
                   headers=auth_headers(token))
    body = r.get_data(as_text=True)
    assert token not in body


# ------------------------------------------------------------------
# POST /api/cases/<id>/events
# ------------------------------------------------------------------

def test_valid_event(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    case = conn.execute("SELECT * FROM cases WHERE case_id = ?", (CASE,)).fetchone()
    rv = case["review_version"]
    conn.close()

    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "copy_attempt", "review_version": rv, "field_key": "full_name"},
    )
    assert r.status_code == 201
    assert r.get_json()["ok"] is True


def test_invalid_event_type_rejected(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "hack_attempt", "review_version": 1},
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_event_type"


def test_stale_review_version_rejected(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "copy_attempt", "review_version": 9999},
    )
    assert r.status_code == 409
    assert r.get_json()["error"] == "stale_review_version"


def test_missing_review_version_rejected(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "copy_attempt"},
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "review_version_required"


def test_malformed_payload_rejected(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        data="not json",
    )
    assert r.status_code == 400


def test_wrong_content_type_rejected(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "text/plain"},
        data="{}",
    )
    assert r.status_code == 415


def test_event_nonexistent_case(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.post(
        "/api/cases/NONEXISTENT/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "copy_attempt", "review_version": 1},
    )
    assert r.status_code == 404


def test_api_responses_contain_only_documented_fields(tmp_cfg):
    """API error responses must not leak applicant values."""
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    # Fill-packet endpoint
    r = client.get(f"/api/cases/{CASE}/fill-packet", headers=auth_headers(token))
    data = r.get_json()
    top_keys = set(data.keys())
    expected_top = {"case_id", "workflow", "workflow_label", "review_version",
                    "reviewed_at", "fields"}
    assert top_keys == expected_top
    # Each field entry
    for f in data["fields"]:
        fkeys = set(f.keys())
        allowed = {"key", "label", "value", "eligible_to_fill", "reason"}
        assert fkeys <= allowed


# ------------------------------------------------------------------
# Token file management
# ------------------------------------------------------------------

def test_token_file_created_in_data_dir(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    token_path = cfg.data_dir / TOKEN_FILENAME
    assert token_path.exists()
    assert len(token) >= 32  # at least 32 hex chars


def test_token_file_not_in_git(tmp_cfg):
    """Verify .gitignore covers the token file."""
    root = Path(__file__).resolve().parents[1]
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    # data/ is gitignored, which covers data/.api_token
    assert "data/" in gi
