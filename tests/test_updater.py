"""Tests for GitHub Releases delta auto-updater, SHA-256 verification, rollback, and patch builder."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
import zipfile

import pytest

from app import create_app
from app.services import updater
from app.version import APP_VERSION
from scripts.build_release import build_patch_and_manifest, build_release


def _make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in files.items():
            zf.writestr(arcname, data)
    return path


def test_version_parsing_and_comparison():
    assert updater.parse_version("v1.2.1") == (1, 2, 1)
    assert updater.is_newer_version("1.2.2", "1.2.1") is True
    assert updater.is_newer_version("v1.10.0", "1.2.1") is True
    assert updater.is_newer_version("1.2.1", "1.2.1") is False
    assert updater.is_newer_version("1.2.0", "1.2.1") is False


def test_healthz_exposes_app_version(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    resp = app.test_client().get("/healthz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["version"] == APP_VERSION


def test_check_download_and_apply_patch_preserves_user_data_and_rejects_bad_hash(tmp_path):
    # Create a fake FormFlow installation with existing user state
    install_dir = tmp_path / "FormFlowInstall"
    (install_dir / "app").mkdir(parents=True)
    (install_dir / "data").mkdir(parents=True)
    (install_dir / "Inbox").mkdir(parents=True)
    (install_dir / "app" / "hello.txt").write_text("old-version", encoding="utf-8")
    (install_dir / "profiles.json").write_text('{"custom": true}', encoding="utf-8")
    (install_dir / "config.local.json").write_text('{"allow_reset": true}', encoding="utf-8")
    (install_dir / "data" / "formflow.db").write_bytes(b"real-customer-db")
    (install_dir / "Inbox" / "keep.jpg").write_bytes(b"inbox-file")

    # Build a valid delta patch ZIP served over local HTTP
    patch_file = _make_zip(
        tmp_path / "FormFlow-v9.9.9-patch.zip",
        {
            "app/hello.txt": b"new-version-9.9.9",
            "app/added_module.txt": b"brand-new",
        },
    )
    patch_sha = updater.sha256_file(patch_file)

    manifest_holder: dict[str, bytes] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/update-manifest.json":
                payload = manifest_holder["manifest"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/FormFlow-v9.9.9-patch.zip":
                data = patch_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    manifest_holder["manifest"] = json.dumps({
        "version": "9.9.9",
        "patch_url": f"http://127.0.0.1:{port}/FormFlow-v9.9.9-patch.zip",
        "patch_sha256": patch_sha,
        "patch_bytes": patch_file.stat().st_size,
        "notes": "Test release",
    }).encode("utf-8")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        info = updater.check_for_update(
            current_version="1.2.1",
            manifest_url=f"http://127.0.0.1:{port}/update-manifest.json",
        )
        assert info.available is True
        assert info.latest_version == "9.9.9"

        # Tampered SHA must fail download verification
        bad_info = updater.UpdateInfo(
            available=True,
            current_version="1.2.1",
            latest_version="9.9.9",
            patch_url=info.patch_url,
            patch_sha256="0" * 64,
        )
        with pytest.raises(updater.UpdateError, match="SHA-256 mismatch"):
            updater.download_patch(bad_info, tmp_path / "dl_bad")

        # Valid download & apply
        dl = updater.download_patch(info, tmp_path / "dl_ok")
        res = updater.apply_patch_update(
            install_dir, dl, info.patch_sha256, data_dir=install_dir / "data", verify_boot=False
        )
        assert res["updated_files"] == 2
        assert (install_dir / "app" / "hello.txt").read_text(encoding="utf-8") == "new-version-9.9.9"
        assert (install_dir / "app" / "added_module.txt").read_text(encoding="utf-8") == "brand-new"
        # User state & custom configs strictly untouched
        assert (install_dir / "profiles.json").read_text(encoding="utf-8") == '{"custom": true}'
        assert (install_dir / "config.local.json").read_text(encoding="utf-8") == '{"allow_reset": true}'
        assert (install_dir / "data" / "formflow.db").read_bytes() == b"real-customer-db"
        assert (install_dir / "Inbox" / "keep.jpg").read_bytes() == b"inbox-file"
    finally:
        srv.shutdown()
        srv.server_close()


def test_patch_rejects_traversal_and_protected_files_and_rolls_back_on_boot_failure(tmp_path):
    install_dir = tmp_path / "AppRoot"
    (install_dir / "app").mkdir(parents=True)
    (install_dir / "app" / "__init__.py").write_text("def create_app(): return 'ok'\n", encoding="utf-8")

    # 1. Path traversal rejected
    evil_zip = _make_zip(tmp_path / "evil.zip", {"../outside.txt": b"nope"})
    with pytest.raises(updater.UpdateError, match="unsafe path traversal"):
        updater.apply_patch_update(
            install_dir, evil_zip, updater.sha256_file(evil_zip), verify_boot=False
        )

    # 2. Protected user profiles.json rejected
    prof_zip = _make_zip(tmp_path / "prof.zip", {"profiles.json": b"{}"})
    with pytest.raises(updater.UpdateError, match="protected user config"):
        updater.apply_patch_update(
            install_dir, prof_zip, updater.sha256_file(prof_zip), verify_boot=False
        )

    # 3. Protected data/ directory rejected
    data_zip = _make_zip(tmp_path / "data.zip", {"data/formflow.db": b"bad"})
    with pytest.raises(updater.UpdateError, match="protected directory"):
        updater.apply_patch_update(
            install_dir, data_zip, updater.sha256_file(data_zip), verify_boot=False
        )

    # 4. Broken code triggers automatic rollback (restores old file + deletes new file)
    broken_zip = _make_zip(
        tmp_path / "broken.zip",
        {
            "app/__init__.py": b"this is invalid python syntax !!!\n",
            "app/brand_new_broken.py": b"x = 1\n",
        },
    )
    with pytest.raises(updater.UpdateError, match="rolled back"):
        updater.apply_patch_update(
            install_dir, broken_zip, updater.sha256_file(broken_zip), verify_boot=True
        )
    assert (install_dir / "app" / "__init__.py").read_text(encoding="utf-8") == "def create_app(): return 'ok'\n"
    assert not (install_dir / "app" / "brand_new_broken.py").exists()


def test_build_patch_and_manifest_creates_clean_delta_archive(tmp_path):
    release_dir, _ = build_release(
        out_dir=tmp_path, version="1.2.1", include_runtime=False, make_zip=False
    )
    patch_zip, manifest_path = build_patch_and_manifest(
        release_dir, tmp_path, version="1.2.1", repo="mohankrishna213/FormFlow"
    )
    assert patch_zip.is_file()
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.1"
    assert manifest["patch_sha256"] == updater.sha256_file(patch_zip)
    assert "mohankrishna213/FormFlow/releases/download/v1.2.1/FormFlow-v1.2.1-patch.zip" in manifest["patch_url"]

    with zipfile.ZipFile(patch_zip, "r") as zf:
        # Must pass updater's strict member validator cleanly (no data/, Inbox/, runtime/, profiles.json!)
        members = updater._validate_patch_members(zf)
        rels = {p.as_posix() for _, p in members}
    assert "app/__init__.py" in rels
    assert "app/version.py" in rels
    assert "desktop_app.py" in rels
    assert "extension/manifest.json" in rels
    assert "profiles.json" not in rels
