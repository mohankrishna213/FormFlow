"""Local API token management (Phase 6).

Generates a strong, unpredictable token using ``secrets`` and stores it in
the data directory (which is already gitignored).  The token is:

- Created once on first run (or when the file is missing).
- Read from disk on every ``require_api_auth`` call (cached per-app).
- Compared using ``hmac.compare_digest`` to avoid timing side-channels.
- Never logged, returned in responses, placed in URLs, or committed.

The future Chrome extension (Phase 7) owner retrieves the token by reading
``data/.api_token`` directly on the local machine.  No unauthenticated
endpoint exposes it.
"""
from __future__ import annotations

import hmac
import secrets
from functools import wraps
from pathlib import Path

from flask import abort, current_app, jsonify, request


TOKEN_FILENAME = ".api_token"
TOKEN_BYTES = 32  # 64 hex chars


def _token_path() -> Path:
    """Return the on-disk path for the API token file."""
    cfg = current_app.config["FORMFLOW"]
    return cfg.data_dir / TOKEN_FILENAME


def ensure_token() -> None:
    """Create the token file if it does not exist (first-run setup)."""
    path = _token_path()
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(TOKEN_BYTES)
    path.write_text(token, encoding="utf-8")


def _read_token() -> str | None:
    """Read the stored token from disk.  Returns None if missing."""
    path = _token_path()
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def validate_token(provided: str) -> bool:
    """Constant-time comparison of the provided token against the stored one."""
    stored = _read_token()
    if not stored or not provided:
        return False
    return hmac.compare_digest(stored.encode(), provided.encode())


def _check_origin() -> bool:
    """Reject non-localhost origins.  Missing Origin is allowed (same-origin
    requests from the Flask UI don't always send it)."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    # Allow localhost on any port
    import re
    return bool(re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin))


def require_api_auth(f):
    """Decorator for API routes that require a valid Bearer token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Origin check
        if not _check_origin():
            return jsonify({"error": "origin_not_allowed"}), 403

        # Extract token from Authorization header
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "authentication_required"}), 401
        token = auth[7:]  # strip "Bearer "
        if not validate_token(token):
            return jsonify({"error": "authentication_required"}), 401
        return f(*args, **kwargs)
    return decorated
