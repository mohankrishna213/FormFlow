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


def _setup_app_and_client(tmp_cfg, **overrides):
    cfg_dict = dict(tmp_cfg)
    cfg_dict.update(overrides)
    app = create_app(cfg_dict)
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
    app, client, conn = _setup_app_and_client(tmp_cfg, auto_ready_fill=False)
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
    app, client, conn = _setup_app_and_client(tmp_cfg, auto_ready_fill=False)
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
    app, client, conn = _setup_app_and_client(tmp_cfg, auto_ready_fill=False)
    try:
        # 1. Unreviewed case in strict mode: no "Ready to Fill" button
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


def test_auto_ready_ui_shows_address_iso_dob_and_highest_conf_without_review(tmp_cfg):
    """Default Auto-Ready UI makes high-confidence OCR fields directly ready without review."""
    app, client, conn = _setup_app_and_client(tmp_cfg)
    try:
        case = create_case(conn, "CASE-AUTO-UI", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "Priya Sharma",
                               method="ocr:aadhaar", confidence=0.85)
        create_extracted_field(conn, case["id"], "father_name", "Raj Sharma",
                               method="ocr:aadhaar", confidence=0.85)
        create_extracted_field(conn, case["id"], "father_name", "Rajesh Sharma",
                               method="ocr:ssc", confidence=0.82)
        create_extracted_field(conn, case["id"], "dob", "05/07/2005",
                               method="ocr:labeled-dmy", confidence=0.85)
        create_extracted_field(conn, case["id"], "address", "45 Park Avenue, Bengaluru - 560001",
                               method="ocr:labeled-block", confidence=0.70)
        create_extracted_field(conn, case["id"], "mobile_no", "9876543210",
                               method="ocr:aadhaar-mobile", confidence=0.85)
        create_extracted_field(conn, case["id"], "licence_no", "KA012026001234",
                               method="ocr:labeled-ll", confidence=0.55)

        # Case Detail shows Ready to Fill immediately + optional review label
        detail_html = client.get("/cases/CASE-AUTO-UI").get_data(as_text=True)
        assert "Ready to Fill" in detail_html
        assert "/cases/CASE-AUTO-UI/fill-ready" in detail_html

        # Ready to Fill renders Auto-Ready badge, Address, Mobile, ISO DOB, and 0.85 father_name
        html = client.get("/cases/CASE-AUTO-UI/fill-ready").get_data(as_text=True)
        assert "Auto-Ready (OCR)" in html
        assert "Auto-Detect / Flexible" in html
        assert "Priya Sharma" in html
        assert "Raj Sharma" in html
        assert "Rajesh Sharma" not in html
        assert "2005-07-05" in html
        assert "45 Park Avenue, Bengaluru - 560001" in html
        assert "9876543210" in html
        assert "Low OCR confidence" in html  # licence_no at 0.55 is gated
        assert "highest-confidence OCR value was selected automatically" in html
    finally:
        conn.close()


def test_option1_reviewed_unchanged_ocr_address_appears_in_ready_to_fill(tmp_cfg):
    """Option 1: clicking Validate & Save with unchanged OCR address confirms it in Ready to Fill."""
    app, client, conn = _setup_app_and_client(tmp_cfg)
    try:
        case = create_case(conn, "CASE-OPT1-ADDR", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "Priya Sharma",
                               method="ocr:aadhaar", confidence=0.85)
        create_extracted_field(conn, case["id"], "dob", "2005-07-05",
                               method="ocr:labeled-dmy", confidence=0.85)
        create_extracted_field(conn, case["id"], "address", "12 MG Road, Hyderabad - 500001",
                               method="ocr:labeled-block", confidence=0.65)
        _, _, errors = save_review(conn, "CASE-OPT1-ADDR", {
            "full_name": "Priya Sharma",
            "father_name": "",
            "dob": "2005-07-05",
            "address": "12 MG Road, Hyderabad - 500001",
            "licence_no": "",
            "appl_number": "",
        })
        assert not errors

        html = client.get("/cases/CASE-OPT1-ADDR/fill-ready").get_data(as_text=True)
        assert "12 MG Road, Hyderabad - 500001" in html
        assert 'data-field="address"' in html
    finally:
        conn.close()


def test_ui_profiles_production_desk_default_and_dev_switch(tmp_cfg):
    """Default UI profile is clean Production Desk mode; POST /ui-mode toggles Dev diagnostics safely."""
    import re

    app, client, conn = _setup_app_and_client(tmp_cfg)
    try:
        case = create_case(conn, "CASE-UI-MODE", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "Kiran Rao",
                               method="ocr:aadhaar", confidence=0.85)
        create_extracted_field(conn, case["id"], "dob", "1998-03-14",
                               method="ocr:labeled-dmy", confidence=0.85)
        create_extracted_field(conn, case["id"], "address", "22 Jubilee Hills, Hyderabad - 500033",
                               method="ocr:labeled-block", confidence=0.75)

        # 1. Production (Desk) mode by default
        dash = client.get("/").get_data(as_text=True)
        assert "Customer Desk" in dash
        assert "data-live-dashboard" in dash
        assert "Watching Inbox" in dash
        assert 'href="/queue"' not in dash
        assert 'href="/healthz"' not in dash
        assert "Dev Mode" in dash

        # Case Detail in Desk mode: inline 1-click Copy + no raw OCR provenance/Events log
        detail = client.get("/cases/CASE-UI-MODE").get_data(as_text=True)
        assert "Customer Details" in detail
        assert 'data-copy-target="desk-val-full_name"' in detail
        assert 'data-copy-target="desk-val-address"' in detail
        assert "22 Jubilee Hills, Hyderabad - 500033" in detail
        assert "ocr:aadhaar" not in detail
        assert "<h3>Events</h3>" not in detail

        # 2. Live status endpoint returns non-PII snapshot with no-store header
        st_res = client.get("/dashboard/status")
        assert st_res.status_code == 200
        assert "no-store" in st_res.headers.get("Cache-Control", "")
        st_json = st_res.get_json()
        assert st_json["case_count"] == 1
        assert "latest_sig" in st_json
        assert "Kiran Rao" not in st_res.get_data(as_text=True)
        assert "Jubilee Hills" not in st_res.get_data(as_text=True)

        # 3. POST /ui-mode enforces CSRF and prevents open redirects
        assert client.post("/ui-mode", data={"mode": "dev"}).status_code == 400
        m = re.search(r'name="csrf_token" value="([^"]+)"', dash)
        assert m
        csrf = m.group(1)

        sw = client.post(
            "/ui-mode",
            data={"csrf_token": csrf, "mode": "dev", "next": "https://evil.example.com"},
        )
        assert sw.status_code == 302
        assert sw.headers["Location"].endswith("/")

        # 4. Dev mode shows Queue/Health nav links, Daily work queue, raw OCR provenance & Events
        dash_dev = client.get("/").get_data(as_text=True)
        assert "Daily work queue" in dash_dev
        assert 'href="/queue"' in dash_dev
        assert 'href="/healthz"' in dash_dev
        assert "Desk Mode" in dash_dev

        detail_dev = client.get("/cases/CASE-UI-MODE").get_data(as_text=True)
        assert "Candidate fields (OCR provenance)" in detail_dev
        assert "ocr:aadhaar" in detail_dev
        assert "<h3>Events</h3>" in detail_dev
    finally:
        conn.close()


def test_ui_mode_config_override_starts_in_dev(tmp_cfg):
    app, client, conn = _setup_app_and_client(tmp_cfg, ui_mode="dev")
    try:
        dash = client.get("/").get_data(as_text=True)
        assert "Daily work queue" in dash
        assert 'href="/queue"' in dash
    finally:
        conn.close()



def test_fill_ready_404_on_nonexistent_case(tmp_cfg):
    app, client, conn = _setup_app_and_client(tmp_cfg)
    try:
        res = client.get("/cases/CASE-NONEXISTENT/fill-ready")
        assert res.status_code == 404
    finally:
        conn.close()
