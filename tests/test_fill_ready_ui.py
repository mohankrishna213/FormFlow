"""Phase 6 tests: Ready to fill UI view.

Tests the Flask /cases/<case_id>/fill-ready route and its interaction with
reviewed cases, unreviewed cases, missing fields, and navigation links.
Synthetic fixtures only — no real PII or external dependencies.
"""
from __future__ import annotations

from app import create_app
from app.db import connect, create_case, create_extracted_field
from app.services.review import save_review


CASE_ID = "CASE-20261016-UI1"


def _setup_app_and_client(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    client = app.test_client()
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    return app, client, conn


def _seed_reviewed_case(conn, case_id=CASE_ID):
    create_case(conn, case_id, appl_number="LD-2026/001", status="needs_attention")
    case = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    create_extracted_field(conn, case["id"], "full_name", "Priya Sharma",
                          method="manual", confidence=None)
    create_extracted_field(conn, case["id"], "dob", "1992-05-12",
                          method="manual", confidence=None)
    values = {
        "full_name": "Priya Sharma",
        "father_name": "Raj Sharma",
        "dob": "1992-05-12",
        "address": "45 Park Avenue, Bengaluru",
        "licence_no": "DL-1234567890",
        "appl_number": "LD-2026/001",
    }
    case, changed, errors = save_review(conn, case_id, values)
    assert not errors
    assert case["status"] == "reviewed"
    return case


def test_fill_ready_reviewed_case_renders_fields_and_copy_buttons(tmp_cfg):
    app, client, conn = _setup_app_and_client(tmp_cfg)
    try:
        _seed_reviewed_case(conn)
        res = client.get(f"/cases/{CASE_ID}/fill-ready")
        assert res.status_code == 200
        html = res.get_data(as_text=True)

        # Title and header
        assert "Ready to Fill" in html
        assert CASE_ID in html
        assert "Driving Licence Application" in html

        # Check field labels and values
        assert "Full name" in html
        assert "Priya Sharma" in html
        assert "Date of birth" in html
        assert "1992-05-12" in html
        assert "Address" in html
        assert "45 Park Avenue, Bengaluru" in html
        assert "Application number" in html
        assert "LD-2026/001" in html

        # Check Copy buttons and script
        assert 'class="copy-btn"' in html
        assert 'data-field="full_name"' in html
        assert 'copyField' in html
        assert 'navigator.clipboard' in html
        assert 'fallbackCopy' in html
    finally:
        conn.close()


def test_fill_ready_unreviewed_case_shows_error(tmp_cfg):
    app, client, conn = _setup_app_and_client(tmp_cfg)
    try:
        create_case(conn, "CASE-UNREVIEWED", status="needs_attention")
        res = client.get("/cases/CASE-UNREVIEWED/fill-ready")
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "case_not_reviewed" in html
        assert 'class="copy-btn"' not in html
    finally:
        conn.close()


def test_fill_ready_missing_optional_field_shows_unavailable(tmp_cfg):
    app, client, conn = _setup_app_and_client(tmp_cfg)
    try:
        create_case(conn, "CASE-PARTIAL", appl_number="", status="needs_attention")
        case = conn.execute("SELECT * FROM cases WHERE case_id = 'CASE-PARTIAL'").fetchone()
        create_extracted_field(conn, case["id"], "full_name", "Amit Patel",
                              method="manual", confidence=None)
        create_extracted_field(conn, case["id"], "dob", "1988-11-20",
                              method="manual", confidence=None)
        values = {
            "full_name": "Amit Patel",
            "father_name": "",
            "dob": "1988-11-20",
            "address": "",
            "licence_no": "",
            "appl_number": "",
        }
        case, changed, errors = save_review(conn, "CASE-PARTIAL", values)
        assert not errors

        res = client.get("/cases/CASE-PARTIAL/fill-ready")
        assert res.status_code == 200
        html = res.get_data(as_text=True)

        assert "Amit Patel" in html
        # licence_no has no value, should show unavailable
        assert "Value not available" in html
    finally:
        conn.close()


def test_case_detail_shows_ready_to_fill_button_only_for_reviewed(tmp_cfg):
    app, client, conn = _setup_app_and_client(tmp_cfg)
    try:
        # 1. Unreviewed case: no "Ready to Fill" button
        create_case(conn, "CASE-UNREV", status="needs_attention")
        res = client.get("/cases/CASE-UNREV")
        assert res.status_code == 200
        assert "Ready to Fill" not in res.get_data(as_text=True)

        # 2. Reviewed case: has "Ready to Fill" button
        _seed_reviewed_case(conn, "CASE-REV")
        res = client.get("/cases/CASE-REV")
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "Ready to Fill" in html
        assert "/cases/CASE-REV/fill-ready" in html
    finally:
        conn.close()


def test_fill_ready_404_on_nonexistent_case(tmp_cfg):
    app, client, conn = _setup_app_and_client(tmp_cfg)
    try:
        res = client.get("/cases/CASE-NONEXISTENT/fill-ready")
        assert res.status_code == 404
    finally:
        conn.close()
