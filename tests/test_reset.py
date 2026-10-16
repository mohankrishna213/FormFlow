"""Reset button: wipe DB + generated files for testing (Inbox kept)."""
import re

from app import create_app
from app.db import connect, create_case, create_document, create_extracted_field, create_job
from app.services.reset import reset_all


def seed_everything(conn, data_dir):
    (data_dir / "staging").mkdir(parents=True, exist_ok=True)
    (data_dir / "prepared").mkdir(parents=True, exist_ok=True)
    (data_dir / "ocr").mkdir(parents=True, exist_ok=True)
    (data_dir / "staging" / "a.pdf").write_bytes(b"staged")
    (data_dir / "prepared" / "a.jpg").write_bytes(b"prepared")
    (data_dir / "ocr" / "doc_1.json").write_bytes(b"{}")
    inbox_file = data_dir.parent / "Inbox" / "keep.pdf"
    inbox_file.write_bytes(b"inbox-kept")
    case = create_case(conn, "CASE-20261015-001", status="needs_attention")
    create_job(conn, "C:/Inbox/a.pdf", state="ready")
    create_document(conn, orig_filename="a.pdf", orig_path="p", case_id=case["id"])
    create_extracted_field(conn, case["id"], "full_name", "Amy Lee")
    conn.execute(
        """INSERT INTO conflict_acks (case_id, field_key, upto_id, value_norm, created_at)
           VALUES (?, 'full_name', 1, 'amy lee', 'now')""",
        (case["id"],))
    conn.commit()


def test_reset_clears_db_and_generated_files(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_everything(conn, cfg.data_dir)
    finally:
        conn.close()
    counts = reset_all(cfg)
    assert counts["cases"] == 1 and counts["documents"] == 1
    assert counts["intake_jobs"] == 1 and counts["extracted_fields"] == 1
    conn = connect(cfg.db_path)
    try:
        for t in ("cases", "documents", "intake_jobs", "extracted_fields",
                  "case_events", "customers", "conflict_acks"):
            assert conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"] == 0
    finally:
        conn.close()
    assert list((cfg.data_dir / "staging").iterdir()) == []
    assert list((cfg.data_dir / "prepared").iterdir()) == []
    assert list((cfg.data_dir / "ocr").iterdir()) == []
    assert (cfg.data_dir.parent / "Inbox" / "keep.pdf").exists()  # Inbox kept


def test_reset_routes_with_confirmation_and_csrf(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_everything(conn, cfg.data_dir)
    finally:
        conn.close()
    client = app.test_client()
    assert client.post("/queue/reset").status_code == 400  # no token
    html = client.get("/queue/reset").data.decode()
    assert "delete everything" in html.lower()
    assert "Cases: 1" in html
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    assert client.post("/queue/reset", data={"csrf_token": token}).status_code == 302
    assert "No intake jobs yet" in client.get("/queue").data.decode()
    assert "No cases yet" in client.get("/").data.decode()


def test_reset_hidden_when_disabled(tmp_cfg):
    app = create_app({**dict(tmp_cfg), "allow_reset": False})
    client = app.test_client()
    assert client.get("/queue/reset").status_code == 404
    assert client.post("/queue/reset", data={"csrf_token": "x"}).status_code in (400, 404)
    assert "Delete all testing data" not in client.get("/queue").data.decode()
