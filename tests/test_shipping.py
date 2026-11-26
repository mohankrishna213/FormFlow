"""Tests for production desktop app launcher, Waitress WSGI server, and release packaging."""
from __future__ import annotations

from pathlib import Path
import zipfile

import desktop_app
from app import create_app
from run import create_http_server
from scripts.build_release import build_release, verify_release_dir


def test_waitress_http_server_binds_strictly_localhost(tmp_cfg):
    app = create_app({**dict(tmp_cfg), "port": 0})
    srv = create_http_server(app)
    assert srv is not None
    try:
        assert srv.effective_host == "127.0.0.1"
        assert int(srv.effective_port) > 0
    finally:
        srv.close()


def test_desktop_app_window_args_and_autostart_command(tmp_path):
    data_dir = tmp_path / "data"
    args = desktop_app.build_app_window_args(
        "edge", Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        "http://127.0.0.1:5000", data_dir,
    )
    assert "--app=http://127.0.0.1:5000" in args
    assert any(a.startswith("--user-data-dir=") for a in args)
    assert "--no-first-run" in args
    assert "--disable-features=msEdgeSidebarV2" in args
    assert (data_dir / "app_window_profile").is_dir()

    # Fake bundled runtime layout -> autostart_command prefers runtime/pythonw.exe
    fake_root = tmp_path / "FormFlowPortable"
    (fake_root / "runtime").mkdir(parents=True)
    (fake_root / "runtime" / "pythonw.exe").write_bytes(b"")
    cmd = desktop_app.autostart_command(fake_root)
    assert "runtime" in cmd and "pythonw.exe" in cmd
    assert "--background" in cmd


def test_read_or_create_api_token_persists_and_stays_consistent(tmp_path):
    data_dir = tmp_path / "data"
    t1 = desktop_app.read_or_create_api_token(data_dir)
    assert len(t1) >= 64
    t2 = desktop_app.read_or_create_api_token(data_dir)
    assert t1 == t2


def test_dry_run_release_build_is_pii_free_and_passes_audit(tmp_path):
    release_dir, zip_path = build_release(
        out_dir=tmp_path,
        version="test",
        include_runtime=False,
        make_zip=True,
    )
    assert release_dir.is_dir()
    assert zip_path is not None and zip_path.is_file()
    assert verify_release_dir(release_dir) == []

    # Strictly NO customer DB, tokens, data/, or Inbox files in the release archive
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
    assert "FormFlow-vtest-win64/Inbox/" in names
    assert "FormFlow-vtest-win64/Setup_FormFlow.bat" in names
    assert "FormFlow-vtest-win64/FormFlow.vbs" in names
    assert "FormFlow-vtest-win64/Uninstall_FormFlow.bat" in names
    assert "FormFlow-vtest-win64/desktop_app.py" in names
    assert "FormFlow-vtest-win64/extension/manifest.json" in names
    assert not any("/data/" in n or n.endswith(".db") or ".api_token" in n for n in names)

    # Audit catches leaked PII/secrets if injected
    (release_dir / "Inbox" / "leaked_customer.jpg").write_bytes(b"secret")
    (release_dir / "formflow.db").write_bytes(b"db")
    issues = verify_release_dir(release_dir)
    assert any("Inbox/" in i for i in issues)
    assert any("formflow.db" in i for i in issues)
