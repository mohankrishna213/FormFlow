"""Tests for FormFlow Chrome Extension Manifest and Packaging (Phase 7A).

Validates:
- Manifest V3 structure
- Minimal permissions (storage only, no broad web permissions, no content scripts)
- Referencing existing files and icons
- Absence of portal automation, DOM selectors, or Sarathi scripts in Phase 7A
"""

import json
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = REPO_ROOT / "extension"
MANIFEST_FILE = EXTENSION_DIR / "manifest.json"


def test_manifest_file_exists_and_is_valid_json():
    assert MANIFEST_FILE.is_file(), f"Manifest file missing: {MANIFEST_FILE}"
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert isinstance(manifest, dict)
    assert manifest.get("manifest_version") == 3
    assert manifest.get("name") == "FormFlow - Driving Licence Assistant"
    assert "version" in manifest


def test_manifest_permissions_are_minimal():
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    permissions = manifest.get("permissions", [])
    # Only storage is required for saving API URL and Bearer token
    assert set(permissions) == {"storage"}, f"Unexpected permissions: {permissions}"

    host_permissions = manifest.get("host_permissions", [])
    # Host permissions must be restricted to local loopback FormFlow API
    assert len(host_permissions) > 0
    for pattern in host_permissions:
        assert (
            "127.0.0.1" in pattern or "localhost" in pattern
        ), f"Overly broad host permission: {pattern}"
        assert "<all_urls>" not in pattern
        assert "*://*/*" not in pattern
        assert "sarathi" not in pattern.lower()


def test_no_content_scripts_or_portal_automation_in_phase_7a():
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # Content scripts are explicitly forbidden in Phase 7A
    assert "content_scripts" not in manifest, "Phase 7A must not contain content_scripts"
    assert "web_accessible_resources" not in manifest or len(manifest["web_accessible_resources"]) == 0


def test_all_referenced_files_exist():
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # Popup
    action = manifest.get("action", {})
    default_popup = action.get("default_popup")
    assert default_popup, "Manifest must specify action.default_popup"
    popup_path = EXTENSION_DIR / default_popup
    assert popup_path.is_file(), f"Popup file missing: {popup_path}"

    # Action Icons
    action_icons = action.get("default_icon", {})
    for size, rel_path in action_icons.items():
        icon_path = EXTENSION_DIR / rel_path
        assert icon_path.is_file(), f"Action icon missing: {icon_path} ({size})"

    # Top-level Icons
    icons = manifest.get("icons", {})
    for size, rel_path in icons.items():
        icon_path = EXTENSION_DIR / rel_path
        assert icon_path.is_file(), f"Extension icon missing: {icon_path} ({size})"

    # Popup referenced scripts/styles
    popup_content = popup_path.read_text(encoding="utf-8")
    assert 'src="api_client.js"' in popup_content
    assert (EXTENSION_DIR / "api_client.js").is_file()
    assert 'src="popup.js"' in popup_content
    assert (EXTENSION_DIR / "popup.js").is_file()
    assert 'href="popup.css"' in popup_content
    assert (EXTENSION_DIR / "popup.css").is_file()


def test_no_hardcoded_tokens_or_pii_in_extension_bundle():
    """Ensure no test tokens or placeholder secrets are committed into extension directory."""
    import re

    hex_token_pattern = re.compile(r"\b[0-9a-fA-F]{64}\b")
    for file_path in EXTENSION_DIR.rglob("*"):
        if file_path.is_file() and file_path.suffix in (".js", ".html", ".json", ".css"):
            text = file_path.read_text(encoding="utf-8")
            match = hex_token_pattern.search(text)
            assert not match, f"Found hardcoded 64-character token in {file_path}: {match.group(0) if match else ''}"
