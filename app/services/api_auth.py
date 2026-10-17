"""Local API token management and authentication hardening (Phase 6.1).

Generates a strong, unpredictable token using ``secrets`` and stores it in
the local data directory (which is gitignored).  The token is:

- Created once on first run (or when the file is missing).
- Preserved across application restarts (never replaced if already valid).
- Stored with restrictive permissions (0o600 on POSIX; under user profile on Windows).
- Compared using ``hmac.compare_digest`` to avoid timing side-channels.
- Never logged, returned in responses, placed in URLs, or committed.

Origin policy:
- Rejects non-localhost origins (403 Forbidden).
- Rejects unexpected localhost ports (only the configured server port is allowed).
- Rejects Origin: null and malformed origins.
- Requests without an Origin header are permitted for non-browser clients (CLI,
  PowerShell, Python test runners) and same-origin browser navigations, protected
  by mandatory Bearer token validation on all API endpoints.
"""
import hmac
import os
import re
import secrets
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import current_app, jsonify, request

TOKEN_FILENAME = ".api_token"
TOKEN_BYTES = 32  # 64 hex chars
_EXTENSION_ID_RE = re.compile(r"^[a-z0-9]{32}$")


def _token_path() -> Path:
    """Return the on-disk path for the API token file."""
    cfg = current_app.config["FORMFLOW"]
    return cfg.data_dir / TOKEN_FILENAME


def ensure_token() -> None:
    """Create the token file if it does not exist (first-run setup).

    - Does not overwrite an existing valid token.
    - Applies owner-only permissions (0o600) on POSIX platforms.
    - On Windows, relies on local user profile NTFS ACLs for the data folder.
    """
    path = _token_path()
    if path.exists() and path.is_file():
        try:
            content = path.read_text(encoding="utf-8").strip()
            if content and len(content) >= 32:
                return
        except OSError:
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(TOKEN_BYTES)

    if os.name != "nt":
        # POSIX: create with restrictive 0o600 permissions
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(path), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
    else:
        # Windows: write to user-owned data directory
        path.write_text(token, encoding="utf-8")


def _read_token() -> str | None:
    """Read the stored token from disk.  Returns None if missing or unreadable."""
    path = _token_path()
    try:
        if not path.is_file():
            return None
        content = path.read_text(encoding="utf-8").strip()
        return content if content else None
    except (OSError, ValueError):
        return None


def validate_token(provided: str) -> bool:
    """Constant-time comparison of the provided token against the stored one."""
    stored = _read_token()
    if not stored or not provided:
        return False
    return hmac.compare_digest(stored.encode("utf-8"), provided.encode("utf-8"))


def _check_origin() -> bool:
    """Validate Origin header against permitted local origins.

    Allowed origins:
    - http://127.0.0.1:{port}
    - http://localhost:{port}
      where {port} is the configured server port (default 5000).
    - chrome-extension://{extension_id}
      where {extension_id} is a valid 32-character extension ID (matching
      configured FORMFLOW_EXTENSION_ID if set).

    Reject:
    - 'null' (sandboxed iframes, file://, data:)
    - non-localhost origins (https://evil.com)
    - localhost on unexpected ports (http://localhost:8080)
    - malformed origins (e.g., http://127.0.0.1.attacker.com)
    - invalid or mismatched chrome-extension IDs

    Missing Origin header:
    Permitted for non-browser clients (PowerShell, curl, CLI scripts, test suites)
    and same-origin browser GET requests that omit the Origin header.
    Every protected request still strictly requires a valid Bearer token.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    if origin == "null" or not origin.strip():
        return False

    parsed = urlsplit(origin)

    # Reject unexpected credentials, path, query, or fragment
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False

    # Chrome extension origins
    if parsed.scheme == "chrome-extension":
        ext_id = (parsed.netloc or parsed.hostname or "").lower()
        if not _EXTENSION_ID_RE.match(ext_id):
            return False
        cfg = current_app.config.get("FORMFLOW")
        configured_id = getattr(cfg, "extension_id", None) if cfg else None
        if configured_id and ext_id != configured_id.lower():
            return False
        return True

    # Localhost HTTP/HTTPS origins
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return False

    # Check port against configured application port
    cfg = current_app.config.get("FORMFLOW")
    expected_port = cfg.port if cfg else 5000
    actual_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if actual_port != expected_port:
        return False

    return True


check_origin = _check_origin


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
