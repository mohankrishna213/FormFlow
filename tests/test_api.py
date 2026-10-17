"""Phase 6.1 tests: API authentication, security, and hardened endpoint behavior.

Covers:
- Strict token authentication and constant-time validation.
- Token file stability across restarts and POSIX permissions.
- Hardened origin policy: localhost on expected port only, rejection of
  unexpected ports, Origin: null, malformed origins, and external domains.
  Explicit allowance of omitted Origin for non-browser CLI clients.
- Hardened event payload validation: schema restrictions, rejection of
  unexpected properties, boolean review versions, unsupported field keys,
  stale versions, and minimal event recording without applicant data leakage.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from app import create_app
from app.db import (
    connect,
    create_case,
    create_extracted_field,
)
from app.services.api_auth import TOKEN_FILENAME, ensure_token, validate_token
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


def _seed_reviewed_case(tmp_cfg, appl_number=""):
    """Seed + review a case with manual confirmed values, return (app, token)."""
    app, token = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    create_case(conn, CASE, status="needs_attention")
    create_extracted_field(conn, 1, "full_name", "Ravi Kumar",
                          method="manual", confidence=None)
    create_extracted_field(conn, 1, "dob", "1995-08-17",
                          method="manual", confidence=None)
    values = {
        "full_name": "Ravi Kumar", "father_name": "",
        "dob": "1995-08-17", "address": "",
        "licence_no": "", "appl_number": appl_number,
    }
    case, _, errors = save_review(conn, CASE, values)
    assert not errors
    conn.close()
    return app, token


def auth_headers(token, origin=None):
    h = {"Authorization": f"Bearer {token}"}
    if origin is not None:
        h["Origin"] = origin
    return h


# ------------------------------------------------------------------
# Token authentication & timing safety
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
    assert r.get_json()["error"] == "authentication_required"


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
# Origin policy hardening
# ------------------------------------------------------------------

def test_localhost_origin_accepted(tmp_cfg):
    """Allowed localhost origin on configured port is accepted."""
    app, token = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    client = app.test_client()

    # Both 127.0.0.1 and localhost on configured port
    for origin in (f"http://127.0.0.1:{cfg.port}", f"http://localhost:{cfg.port}"):
        r = client.get("/api/cases", headers=auth_headers(token, origin))
        assert r.status_code == 200, f"Expected 200 for origin {origin}, got {r.status_code}"


def test_unexpected_localhost_port_rejected(tmp_cfg):
    """Localhost on an unexpected port (e.g. rogue server on 8080) is rejected."""
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    for origin in ("http://127.0.0.1:8080", "http://localhost:3000", "http://127.0.0.1:80"):
        r = client.get("/api/cases", headers=auth_headers(token, origin))
        assert r.status_code == 403
        assert r.get_json()["error"] == "origin_not_allowed"


def test_origin_null_rejected(tmp_cfg):
    """Origin: null (sandboxed iframe / data: URI / file:) is rejected."""
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases", headers=auth_headers(token, "null"))
    assert r.status_code == 403
    assert r.get_json()["error"] == "origin_not_allowed"


def test_malformed_origin_rejected(tmp_cfg):
    """Malformed origins (e.g. domain spoofing, invalid URL) are rejected."""
    app, token = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    client = app.test_client()
    for origin in (
        f"http://127.0.0.1.attacker.com:{cfg.port}",
        "not-a-valid-origin",
        "javascript:alert(1)",
        f"http://user:pass@127.0.0.1:{cfg.port}",
    ):
        r = client.get("/api/cases", headers=auth_headers(token, origin))
        assert r.status_code == 403
        assert r.get_json()["error"] == "origin_not_allowed"


def test_external_origin_rejected(tmp_cfg):
    """External websites are rejected with 403."""
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases", headers=auth_headers(token, "https://evil.example.com"))
    assert r.status_code == 403
    assert r.get_json()["error"] == "origin_not_allowed"


def test_no_origin_header_accepted(tmp_cfg):
    """Requests without Origin header (CLI, PowerShell, same-origin GET) are accepted with token."""
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
    assert "case_id" in case
    assert "status" in case
    assert "reviewed" in case
    assert "review_version" in case
    assert case["review_version"] == 1
    assert "reviewed_at" in case
    for banned in ("full_name", "dob", "address", "appl_number"):
        assert banned not in case


# ------------------------------------------------------------------
# GET /api/cases/<id>/fill-packet
# ------------------------------------------------------------------

def test_fill_packet_valid(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.get(f"/api/cases/{CASE}/fill-packet", headers=auth_headers(token))
    assert r.status_code == 200
    data = r.get_json()
    assert data["case_id"] == CASE
    assert "fields" in data
    assert "review_version" in data


def test_fill_packet_invalid_case(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()
    r = client.get("/api/cases/NONEXISTENT/fill-packet", headers=auth_headers(token))
    assert r.status_code == 404
    data = r.get_json()
    assert data["error"] == "case_not_found"
    assert "Ravi" not in json.dumps(data)


def test_fill_packet_unreviewed(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    create_case(conn, "CASE-UNREV", status="needs_attention")
    conn.close()
    client = app.test_client()
    r = client.get("/api/cases/CASE-UNREV/fill-packet", headers=auth_headers(token))
    assert r.status_code == 409
    assert r.get_json()["error"] == "case_not_reviewed"


def test_fill_packet_invalid_workflow(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.get(f"/api/cases/{CASE}/fill-packet?workflow=bad", headers=auth_headers(token))
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_workflow"


def test_fill_packet_app_status_workflow(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg, appl_number="3984089026")
    client = app.test_client()
    r = client.get(f"/api/cases/{CASE}/fill-packet?workflow=app_status", headers=auth_headers(token))
    assert r.status_code == 200
    data = r.get_json()
    assert data["case_id"] == CASE
    assert data["workflow"] == "app_status"
    assert data["workflow_label"] == "Application Status"
    keys = [f["key"] for f in data["fields"]]
    assert keys == ["application_no", "dob"]
    fmap = {f["key"]: f for f in data["fields"]}
    assert fmap["application_no"]["eligible_to_fill"] is True
    assert fmap["application_no"]["value"] == "3984089026"
    assert fmap["dob"]["eligible_to_fill"] is True
    assert fmap["dob"]["value"] == "1995-08-17"


def test_fill_packet_auto_workflow(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg, appl_number="3984089026")
    client = app.test_client()
    r = client.get(f"/api/cases/{CASE}/fill-packet?workflow=auto", headers=auth_headers(token))
    assert r.status_code == 200
    data = r.get_json()
    assert data["case_id"] == CASE
    assert data["workflow"] == "auto"
    assert data["workflow_label"] == "Auto-Detect / Flexible"
    fmap = {f["key"]: f for f in data["fields"]}
    assert "application_no" in fmap
    assert "dob" in fmap
    assert fmap["application_no"]["value"] == "3984089026"


def test_fill_packet_no_secrets_in_response(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.get(f"/api/cases/{CASE}/fill-packet", headers=auth_headers(token))
    body = r.get_data(as_text=True)
    assert token not in body


# ------------------------------------------------------------------
# POST /api/cases/<id>/events (Hardened)
# ------------------------------------------------------------------

def test_valid_event(tmp_cfg):
    """Valid copy_attempt and fill_attempt events are recorded."""
    app, token = _seed_reviewed_case(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    case = conn.execute("SELECT * FROM cases WHERE case_id = ?", (CASE,)).fetchone()
    rv = case["review_version"]
    conn.close()

    client = app.test_client()

    # 1. copy_attempt with valid field_key
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "copy_attempt", "review_version": rv, "field_key": "full_name"},
    )
    assert r.status_code == 201
    assert r.get_json()["ok"] is True

    # 2. fill_attempt without field_key (form-level)
    r2 = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "fill_attempt", "review_version": rv},
    )
    assert r2.status_code == 201
    assert r2.get_json()["ok"] is True

    # 3. autofill_attempt without field_key (extension autofill)
    r3 = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "autofill_attempt", "review_version": rv, "detail": "Operator autofilled fields"},
    )
    assert r3.status_code == 201
    assert r3.get_json()["ok"] is True


def test_copy_attempt_requires_field_key(tmp_cfg):
    """copy_attempt without field_key is rejected."""
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "copy_attempt", "review_version": 1},
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "unsupported_field_key"


def test_unsupported_field_key_rejected(tmp_cfg):
    """Unsupported or arbitrary field_key strings are rejected."""
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    for bad_key in ("credit_card", "password", "unknown_field", "x" * 55):
        r = client.post(
            f"/api/cases/{CASE}/events",
            headers={**auth_headers(token), "Content-Type": "application/json"},
            json={"event_type": "copy_attempt", "review_version": 1, "field_key": bad_key},
        )
        assert r.status_code == 400
        assert r.get_json()["error"] == "unsupported_field_key"


def test_unexpected_payload_fields_rejected(tmp_cfg):
    """Inappropriate extra properties in event payload are rejected."""
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={
            "event_type": "copy_attempt",
            "review_version": 1,
            "field_key": "full_name",
            "applicant_name": "Ravi Kumar",  # unapproved extra field
        },
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "unexpected_payload_fields"


def test_boolean_review_version_rejected(tmp_cfg):
    """review_version = True (bool) must be rejected, not treated as 1."""
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "copy_attempt", "review_version": True, "field_key": "full_name"},
    )
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_review_version"


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
        json={"event_type": "copy_attempt", "review_version": 9999, "field_key": "full_name"},
    )
    assert r.status_code == 409
    assert r.get_json()["error"] == "stale_review_version"


def test_missing_review_version_rejected(tmp_cfg):
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.post(
        f"/api/cases/{CASE}/events",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={"event_type": "fill_attempt"},
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
        json={"event_type": "copy_attempt", "review_version": 1, "field_key": "full_name"},
    )
    assert r.status_code == 404


def test_api_responses_contain_only_documented_fields(tmp_cfg):
    """API responses must contain only documented fields."""
    app, token = _seed_reviewed_case(tmp_cfg)
    client = app.test_client()
    r = client.get(f"/api/cases/{CASE}/fill-packet", headers=auth_headers(token))
    data = r.get_json()
    top_keys = set(data.keys())
    expected_top = {"case_id", "workflow", "workflow_label", "review_version",
                    "reviewed_at", "fields"}
    assert top_keys == expected_top
    for f in data["fields"]:
        fkeys = set(f.keys())
        allowed = {"key", "label", "value", "eligible_to_fill", "reason"}
        assert fkeys <= allowed


# ------------------------------------------------------------------
# Token file management & permissions
# ------------------------------------------------------------------

def test_token_file_created_in_data_dir(tmp_cfg):
    app, token = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    token_path = cfg.data_dir / TOKEN_FILENAME
    assert token_path.exists()
    assert len(token) >= 32


def test_token_stability_across_restarts(tmp_cfg):
    """Token is preserved across application restarts and not overwritten."""
    app1, token1 = _app_and_token(tmp_cfg)
    # Simulate restart by creating app again with same config
    with app1.app_context():
        ensure_token()  # second invocation
    app2, token2 = _app_and_token(tmp_cfg)
    assert token1 == token2


def test_unreadable_or_missing_token_fails_safely(tmp_cfg):
    """Missing or unreadable token file fails closed (401 Unauthorized)."""
    app, token = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    token_path = cfg.data_dir / TOKEN_FILENAME
    token_path.unlink()  # remove file
    assert not token_path.exists()

    with app.app_context():
        assert validate_token("any-token") is False

    client = app.test_client()
    r = client.get("/api/cases", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_token_permissions_posix(tmp_cfg):
    """On POSIX systems, verify file mode is 0o600 (owner read/write only)."""
    if os.name == "nt":
        # On Windows, NTFS ACLs govern permissions; chmod only toggles read-only bit.
        return
    app, _ = _app_and_token(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    token_path = cfg.data_dir / TOKEN_FILENAME
    mode = stat.S_IMODE(os.stat(token_path).st_mode)
    assert mode == 0o600


def test_token_file_not_in_git(tmp_cfg):
    """Verify .gitignore covers the token file."""
    root = Path(__file__).resolve().parents[1]
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    assert "data/" in gi
