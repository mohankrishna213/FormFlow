"""Tests for Chrome extension origin policy, CORS headers, and preflight handling."""
from __future__ import annotations

import os

from app import create_app
from app.db import connect
from app.services.api_auth import TOKEN_FILENAME, ensure_token

TEST_EXT_ID = "abcdefghijklmnopqrstuvwxyz123456"
VALID_EXT_ORIGIN = f"chrome-extension://{TEST_EXT_ID}"


def _app_and_token(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    token_path = cfg.data_dir / TOKEN_FILENAME
    token = token_path.read_text(encoding="utf-8").strip()
    return app, token


def test_chrome_extension_origin_accepted_with_token(tmp_cfg):
    """A request from a Chrome extension with a valid Bearer token is accepted with CORS headers."""
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()

    res = client.get(
        "/api/cases",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": VALID_EXT_ORIGIN,
        },
    )
    assert res.status_code == 200
    assert res.headers.get("Access-Control-Allow-Origin") == VALID_EXT_ORIGIN
    assert "Authorization" in res.headers.get("Access-Control-Allow-Headers", "")
    assert res.get_json() == {"cases": []}


def test_cors_preflight_options_accepted_for_extension(tmp_cfg):
    """CORS preflight OPTIONS from a valid extension origin returns 204 with CORS headers."""
    app, _ = _app_and_token(tmp_cfg)
    client = app.test_client()

    res = client.open(
        "/api/cases",
        method="OPTIONS",
        headers={
            "Origin": VALID_EXT_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert res.status_code == 204
    assert res.headers.get("Access-Control-Allow-Origin") == VALID_EXT_ORIGIN
    assert "Authorization" in res.headers.get("Access-Control-Allow-Headers", "")
    assert "GET" in res.headers.get("Access-Control-Allow-Methods", "")


def test_cors_preflight_rejected_for_evil_origin(tmp_cfg):
    """CORS preflight OPTIONS from an unapproved web origin is rejected with 403."""
    app, _ = _app_and_token(tmp_cfg)
    client = app.test_client()

    res = client.open(
        "/api/cases",
        method="OPTIONS",
        headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert res.status_code == 403
    assert res.headers.get("Access-Control-Allow-Origin") is None


def test_invalid_extension_id_rejected(tmp_cfg):
    """Malformed extension IDs (not 32 alphanumeric chars) are rejected with 403."""
    app, token = _app_and_token(tmp_cfg)
    client = app.test_client()

    for bad_origin in (
        "chrome-extension://short",
        "chrome-extension://invalid_chars_here_123456789012",
        "chrome-extension://UPPERCASE_CHARACTERS_NOT_ALLOWED32",
        "chrome-extension://toolongtoolongtoolongtoolongtoolong123",
        "chrome-extension://abcdefghijklmnopqrstuvwxyz123456:8080",
        "chrome-extension://abcdefghijklmnopqrstuvwxyz123456/path",
    ):
        res = client.get(
            "/api/cases",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": bad_origin,
            },
        )
        assert res.status_code == 403, f"Expected 403 for {bad_origin}, got {res.status_code}"
        assert res.headers.get("Access-Control-Allow-Origin") is None


def test_configured_extension_id_restriction(tmp_cfg):
    """When FORMFLOW_EXTENSION_ID is configured, only that exact extension ID is accepted."""
    allowed_id = "targetextensionid123456789012345"
    disallowed_id = "otherextensionid1234567890123456"

    old_env = os.environ.get("FORMFLOW_EXTENSION_ID")
    try:
        os.environ["FORMFLOW_EXTENSION_ID"] = allowed_id
        app, token = _app_and_token(tmp_cfg)
        client = app.test_client()

        # Allowed ID succeeds
        res_ok = client.get(
            "/api/cases",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": f"chrome-extension://{allowed_id}",
            },
        )
        assert res_ok.status_code == 200
        assert res_ok.headers.get("Access-Control-Allow-Origin") == f"chrome-extension://{allowed_id}"

        # Other ID is rejected
        res_blocked = client.get(
            "/api/cases",
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": f"chrome-extension://{disallowed_id}",
            },
        )
        assert res_blocked.status_code == 403
    finally:
        if old_env is not None:
            os.environ["FORMFLOW_EXTENSION_ID"] = old_env
        else:
            os.environ.pop("FORMFLOW_EXTENSION_ID", None)
