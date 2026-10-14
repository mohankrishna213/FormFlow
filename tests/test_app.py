"""Phase 1 app tests: factory, local-only bind, routes, gitignore hygiene."""
from pathlib import Path

from app import create_app
from app.db import create_case, generate_case_id, init_db, connect


def test_app_binds_localhost_only(tmp_cfg):
    app = create_app(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 5001


def test_health_and_dashboard(tmp_cfg):
    app = create_app(tmp_cfg)
    client = app.test_client()
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"
    r = client.get("/")
    assert r.status_code == 200
    assert b"FormFlow" in r.data
    r = client.get("/cases")
    assert r.status_code == 200
    r = client.get("/documents")
    assert r.status_code == 200


def test_case_visible_after_create(tmp_cfg):
    app = create_app(tmp_cfg)
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        cid = generate_case_id(conn, "20261014")
        create_case(conn, cid)
    finally:
        conn.close()
    client = app.test_client()
    assert client.get(f"/cases/{cid}").status_code == 200
    assert client.get("/cases/NOPE-000").status_code == 404


def test_no_customer_data_tracked_by_git():
    root = Path(__file__).resolve().parents[1]
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    for token in ("data/", "Inbox/", "*.db", "config.local.json", "*.log"):
        assert token in gi, f".gitignore missing {token}"
