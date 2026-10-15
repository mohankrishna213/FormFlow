"""Phase 5: dashboard, review Validate & Save, merge/assign, XSS, files."""
import re
from pathlib import Path

from app import create_app
from app.db import (
    connect,
    create_case,
    create_document,
    create_extracted_field,
    create_job,
    generate_case_id,
)


def seed_case(conn, case_id="CASE-20261015-001", fields=(), status="needs_attention",
              appl=None, customer=None):
    row = create_case(conn, case_id, customer_id=customer, appl_number=appl, status=status)
    for key, value, method in fields:
        create_extracted_field(conn, row["id"], key, value, method=method, confidence=0.85)
    return conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()


def token_for(client, url):
    html = client.get(url).data.decode()
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no CSRF token on {url}"
    return m.group(1)


def test_case_list_search_and_pagination(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        for i in range(25):
            seed_case(conn, f"CASE-20261015-{i + 1:03d}",
                      [("full_name", "Test Person", "text:labeled")])
    finally:
        conn.close()
    client = app.test_client()
    p1 = client.get("/cases").data.decode()
    assert p1.count("/cases/CASE-20261015-") == 20
    assert "Page 1 of 2" in p1
    p2 = client.get("/cases?page=2").data.decode()
    assert p2.count("/cases/CASE-20261015-") == 5
    assert client.get("/cases?q=CASE-20261015-007").data.decode().count("/cases/CASE-20261015-") == 1
    assert "No cases match" in client.get("/cases?q=nobody-here").data.decode()
    assert "No cases match" in client.get("/cases?status=reviewed").data.decode()


def test_dashboard_next_actions(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn, "CASE-20261015-001", [("full_name", "Amy Lee", "text:labeled")],
                  status="reviewed")
        seed_case(conn, "CASE-20261015-002", [("full_name", "Bo Ray", "text:labeled")],
                  status="needs_attention")
        seed_case(conn, "CASE-20261015-003", [("full_name", "Cy Fox", "text:labeled")],
                  status="ready")
    finally:
        conn.close()
    html = app.test_client().get("/").data.decode()
    assert "Done" in html and "Resolve" in html and "Review" in html


def test_review_prefill_save_and_persist(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn, fields=[("full_name", "ravi kumar", "text:labeled"),
                                ("dob", "1995-08-17", "text:labeled-dmy")])
    finally:
        conn.close()
    client = app.test_client()
    html = client.get("/cases/CASE-20261015-001/review").data.decode()
    assert "ravi kumar" in html and "1995-08-17" in html
    assert "checkbox" not in html.lower()  # no per-field checkboxes
    token = token_for(client, "/cases/CASE-20261015-001/review")
    r = client.post("/cases/CASE-20261015-001/review", data={
        "csrf_token": token, "full_name": "Ravi Kumar",
        "dob": "1995-08-17", "address": "12 MG Road", "appl_number": ""})
    assert r.status_code == 302
    conn = connect(cfg.db_path)  # restart-equivalent reconnect
    try:
        case = conn.execute("SELECT * FROM cases WHERE case_id = 'CASE-20261015-001'").fetchone()
        assert case["status"] == "reviewed" and case["reviewed_at"]
        manual = conn.execute(
            "SELECT field_key, value FROM extracted_fields WHERE method = 'manual'").fetchall()
        assert {m["field_key"]: m["value"] for m in manual}["full_name"] == "Ravi Kumar"
    finally:
        conn.close()


def test_licence_no_saved_and_validated(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn, fields=[("full_name", "Ravi Kumar", "text:labeled"),
                                ("dob", "1995-08-17", "text:labeled")])
    finally:
        conn.close()
    client = app.test_client()
    assert 'name="licence_no"' in client.get("/cases/CASE-20261015-001/review").data.decode()
    token = token_for(client, "/cases/CASE-20261015-001/review")
    bad = client.post("/cases/CASE-20261015-001/review", data={
        "csrf_token": token, "full_name": "Ravi Kumar", "dob": "1995-08-17",
        "address": "", "licence_no": "!!!", "appl_number": ""})
    assert bad.status_code == 200 and "Licence number" in bad.data.decode()
    good = client.post("/cases/CASE-20261015-001/review", data={
        "csrf_token": token, "full_name": "Ravi Kumar", "dob": "1995-08-17",
        "address": "", "licence_no": "TG 1234567890", "appl_number": ""})
    assert good.status_code == 302
    conn = connect(cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT value FROM extracted_fields WHERE field_key='licence_no' AND method='manual'").fetchall()
        assert [r["value"] for r in rows] == ["TG 1234567890"]
    finally:
        conn.close()


def test_review_invalid_values_rejected(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, "/cases/CASE-20261015-001/review")
    for bad in ({"full_name": "", "dob": "1995-08-17", "address": "", "appl_number": ""},
                {"full_name": "Ravi123", "dob": "1995-08-17", "address": "", "appl_number": ""},
                {"full_name": "Ravi Kumar", "dob": "05/06/2020", "address": "", "appl_number": ""},
                {"full_name": "Ravi Kumar", "dob": "not-a-date", "address": "", "appl_number": ""},
                {"full_name": "Ravi Kumar", "dob": "1995-08-17", "address": "", "appl_number": "!!!"}):
        r = client.post("/cases/CASE-20261015-001/review",
                        data={"csrf_token": token, **bad})
        assert r.status_code == 200
        assert "Fix these" in r.data.decode()
    conn = connect(cfg.db_path)
    try:
        case = conn.execute("SELECT * FROM cases WHERE case_id = 'CASE-20261015-001'").fetchone()
        assert case["status"] == "needs_attention" and not case["reviewed_at"]
        assert conn.execute("SELECT COUNT(*) n FROM extracted_fields WHERE method='manual'").fetchone()["n"] == 0
    finally:
        conn.close()


def test_conflict_blocks_until_edited(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn, fields=[("full_name", "Ravi Kumar", "text:labeled"),
                                ("dob", "1995-08-17", "text:labeled"),
                                ("dob", "1999-06-13", "ocr:labeled")])
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, "/cases/CASE-20261015-001/review")
    # Prefilled value is the latest candidate (1999-06-13); saving untouched blocks.
    blocked = client.post("/cases/CASE-20261015-001/review", data={
        "csrf_token": token, "full_name": "Ravi Kumar", "dob": "1999-06-13",
        "address": "", "appl_number": ""})
    assert blocked.status_code == 200 and "Conflicting values" in blocked.data.decode()
    # Picking the other candidate value resolves it.
    resolved = client.post("/cases/CASE-20261015-001/review", data={
        "csrf_token": token, "full_name": "Ravi Kumar", "dob": "1995-08-17",
        "address": "", "appl_number": ""})
    assert resolved.status_code == 302


def test_edit_after_review_revalidates(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn, fields=[("full_name", "Ravi Kumar", "manual"),
                                ("dob", "1995-08-17", "manual")], status="reviewed")
        conn.execute("UPDATE cases SET reviewed_at = 'old', review_version = 1"
                     " WHERE case_id = 'CASE-20261015-001'")
        conn.commit()
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, "/cases/CASE-20261015-001/review")
    r = client.post("/cases/CASE-20261015-001/review", data={
        "csrf_token": token, "full_name": "Ravi Kumar", "dob": "1995-08-17",
        "address": "New Address", "appl_number": ""})
    assert r.status_code == 302
    conn = connect(cfg.db_path)
    try:
        case = conn.execute("SELECT * FROM cases WHERE case_id = 'CASE-20261015-001'").fetchone()
        assert case["status"] == "reviewed" and case["reviewed_at"] != "old"
        assert case["review_version"] == 2
    finally:
        conn.close()


def test_merge_flow_with_confirmation(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn, "CASE-20261015-001", [("full_name", "Ravi Kumar", "text:labeled")])
        seed_case(conn, "CASE-20261015-002", [("full_name", "Ravi Kumar", "ocr:labeled")])
        create_document(conn, orig_filename="sig.png", orig_path="p",
                        intake_job_id=None, case_id=2)
    finally:
        conn.close()
    client = app.test_client()
    confirm = client.get("/cases/CASE-20261015-001/merge?source=CASE-20261015-002")
    assert confirm.status_code == 200 and "sig.png" in confirm.data.decode()
    assert client.post("/cases/CASE-20261015-001/merge",
                       data={"source": "CASE-20261015-001"}).status_code == 400  # no token
    token = token_for(client, "/cases/CASE-20261015-001/merge?source=CASE-20261015-002")
    assert client.post("/cases/CASE-20261015-001/merge",
                       data={"csrf_token": token, "source": "CASE-20261015-001"}).status_code == 400
    r = client.post("/cases/CASE-20261015-001/merge",
                    data={"csrf_token": token, "source": "CASE-20261015-002"})
    assert r.status_code == 302
    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT * FROM cases WHERE case_id='CASE-20261015-002'").fetchone() is None
        target = conn.execute("SELECT * FROM cases WHERE case_id='CASE-20261015-001'").fetchone()
        assert conn.execute("SELECT COUNT(*) n FROM documents WHERE case_id=?", (target["id"],)).fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM extracted_fields WHERE case_id=?", (target["id"],)).fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(*) n FROM case_events WHERE kind='merged'").fetchone()["n"] == 1
    finally:
        conn.close()


def test_assign_document_invalidates_reviewed_target(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn, "CASE-20261015-001", [("full_name", "Amy Lee", "manual")], status="reviewed")
        doc = create_document(conn, orig_filename="photo.jpg", orig_path="p")
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, "/cases/CASE-20261015-001")
    r = client.post("/cases/CASE-20261015-001/assign-document",
                    data={"csrf_token": token, "document_id": str(doc["id"])})
    assert r.status_code == 302
    conn = connect(cfg.db_path)
    try:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc["id"],)).fetchone()
        assert row["case_id"] == 1
        case = conn.execute("SELECT * FROM cases WHERE case_id='CASE-20261015-001'").fetchone()
        assert case["status"] == "needs_attention" and case["reviewed_at"] is None
    finally:
        conn.close()


def test_csrf_and_404s(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    client = app.test_client()
    assert client.post("/cases/NOPE/review", data={}).status_code == 400  # token first
    assert client.get("/cases/NOPE").status_code == 404
    assert client.get("/cases/NOPE/review").status_code == 404
    assert client.get("/documents/9999/original").status_code == 404


def test_xss_escaped(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn, fields=[("full_name", "<img src=x onerror=alert(1)>", "ocr:labeled")])
        create_document(conn, orig_filename="<script>alert(2)</script>.pdf", orig_path="p",
                        case_id=1)
    finally:
        conn.close()
    client = app.test_client()
    for url in ("/cases", "/", "/cases/CASE-20261015-001",
                "/cases/CASE-20261015-001/review", "/needs-attention"):
        html = client.get(url).data.decode()
        assert "<script>alert(2)" not in html and "<img src=x onerror" not in html
    # Values render escaped where they appear.
    assert "&lt;script&gt;" in client.get("/cases/CASE-20261015-001").data.decode()
    cases_html = client.get("/cases").data.decode()
    assert "&lt;img" in cases_html


def test_file_serving_and_traversal_guard(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    (cfg.data_dir / "staging").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "prepared").mkdir(parents=True, exist_ok=True)
    orig = cfg.data_dir / "staging" / "a.pdf"
    prep = cfg.data_dir / "prepared" / "a.pdf"
    orig.write_bytes(b"%PDF-1.4 fake")
    prep.write_bytes(b"%PDF-1.4 fake-prepared")
    conn = connect(cfg.db_path)
    try:
        good = create_document(conn, orig_filename="a.pdf", orig_path=str(orig),
                               prepared_path=str(prep), mime="application/pdf", case_id=None)
        evil = create_document(conn, orig_filename="evil.pdf",
                               orig_path="C:/Windows/outside.pdf", case_id=None)
    finally:
        conn.close()
    client = app.test_client()
    assert client.get(f"/documents/{good['id']}/original").status_code == 200
    assert client.get(f"/documents/{good['id']}/prepared").status_code == 200
    assert client.get(f"/documents/{good['id']}/bogus").status_code == 404
    assert client.get(f"/documents/{evil['id']}/original").status_code == 404


def test_needs_attention_aggregates(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_case(conn, status="needs_attention")
        create_job(conn, "C:/Inbox/f.pdf", state="failed", last_error="boom")
        create_document(conn, orig_filename="u.jpg", orig_path="p", case_id=None)
    finally:
        conn.close()
    html = app.test_client().get("/needs-attention").data.decode()
    assert "CASE-20261015-001" in html and "u.jpg" in html and "boom" in html


def test_new_intakes_appear_without_restart(tmp_cfg):
    app = create_app(dict(tmp_cfg))  # app started first
    client = app.test_client()
    assert "No cases" in client.get("/").data.decode()
    conn = connect(app.config["FORMFLOW"].db_path)  # intake lands later
    try:
        seed_case(conn, fields=[("full_name", "Late Arrival", "text:labeled")])
    finally:
        conn.close()
    assert "Late Arrival" in client.get("/").data.decode()  # visible, no restart
