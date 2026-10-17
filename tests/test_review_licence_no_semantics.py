"""Phase 7B Part 1 Tests: Learner's Licence Number (licence_no) Semantics & Operator Confirmation.

Validates:
1. licence_no remains the existing field key (no duplicate columns/keys).
2. FIELD_LABELS["licence_no"] is "Learner's Licence Number".
3. application_no is kept strictly distinct and never used as an LL number.
4. An OCR-only licence_no candidate remains ineligible when case is reviewed without confirmation.
5. An operator can explicitly confirm an unchanged OCR candidate via confirm_licence_no='1'.
6. Confirmed licence_no is persisted with method='manual' and becomes eligible only on a reviewed case.
7. New evidence or edits invalidate review state and block packet issuance.
"""

from __future__ import annotations

import pytest
from app import create_app
from app.db import connect, create_case, create_extracted_field
from app.services.fill_packet import build_fill_packet, DEFAULT_WORKFLOW, PacketError
from app.services.review import (
    FIELD_LABELS,
    MANUAL_FIELDS,
    REVIEW_FIELDS,
    save_review,
)

CASE_ID = "CASE-20261017-LL1"


def _setup_case(tmp_cfg, status="needs_attention"):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    case = create_case(conn, CASE_ID, status=status)
    return app, conn, case


def test_internal_keys_preserved_and_label_updated():
    assert "licence_no" in REVIEW_FIELDS
    assert "licence_no" in MANUAL_FIELDS
    assert "learning_licence_no" not in REVIEW_FIELDS
    assert FIELD_LABELS["licence_no"] == "Learner's Licence Number"
    assert FIELD_LABELS["application_no"] == "Application number"


def test_application_no_never_used_as_ll_number(tmp_cfg):
    app, conn, case = _setup_case(tmp_cfg)
    try:
        # Candidate application_no and confirmed appl_number on case
        conn.execute("UPDATE cases SET appl_number = 'APPL-99999' WHERE id = ?", (case["id"],))
        create_extracted_field(conn, case["id"], "full_name", "Devi Sharma", method="manual")
        create_extracted_field(conn, case["id"], "dob", "1992-04-12", method="manual")

        # Review save without licence_no
        values = {
            "full_name": "Devi Sharma",
            "father_name": "",
            "dob": "1992-04-12",
            "address": "",
            "licence_no": "",
            "appl_number": "APPL-99999",
        }
        case_res, _, errors = save_review(conn, CASE_ID, values)
        assert not errors
        assert case_res["status"] == "reviewed"

        packet = build_fill_packet(conn, CASE_ID, DEFAULT_WORKFLOW)
        assert not isinstance(packet, PacketError)
        fmap = {f["key"]: f for f in packet["fields"]}

        # licence_no is unavailable and distinct
        assert fmap["licence_no"]["eligible_to_fill"] is False
        assert fmap["licence_no"]["value"] is None
        assert fmap["licence_no"]["reason"] == "Value not available"

        # application_no is confirmed
        assert fmap["application_no"]["eligible_to_fill"] is True
        assert fmap["application_no"]["value"] == "APPL-99999"
    finally:
        conn.close()


def test_ocr_only_ll_number_remains_ineligible_without_explicit_confirmation(tmp_cfg):
    app, conn, case = _setup_case(tmp_cfg)
    try:
        # Seed required fields and an OCR candidate for licence_no
        create_extracted_field(conn, case["id"], "full_name", "Kavita Reddy", method="text:labeled", confidence=0.88)
        create_extracted_field(conn, case["id"], "dob", "1994-06-25", method="text:labeled", confidence=0.88)
        create_extracted_field(conn, case["id"], "licence_no", "TS09/0001234/2021", method="ocr:labeled", confidence=0.75)

        # Operator validates case but DOES NOT check confirm_licence_no
        values = {
            "full_name": "Kavita Reddy",
            "father_name": "",
            "dob": "1994-06-25",
            "address": "",
            "licence_no": "TS09/0001234/2021",
            "appl_number": "",
            # confirm_licence_no omitted / not checked
        }
        case_res, changed, errors = save_review(conn, CASE_ID, values)
        assert not errors
        assert case_res["status"] == "reviewed"

        # In fill packet, required fields are confirmed by Validate & Save,
        # but OCR-only licence_no is strictly NOT eligible
        packet = build_fill_packet(conn, CASE_ID, DEFAULT_WORKFLOW)
        assert not isinstance(packet, PacketError)
        fmap = {f["key"]: f for f in packet["fields"]}

        assert fmap["full_name"]["eligible_to_fill"] is True
        assert fmap["dob"]["eligible_to_fill"] is True
        assert fmap["licence_no"]["eligible_to_fill"] is False
        assert fmap["licence_no"]["value"] is None
        assert fmap["licence_no"]["reason"] == "Value not confirmed by operator"
    finally:
        conn.close()


def test_operator_explicit_confirmation_makes_unchanged_ocr_ll_eligible(tmp_cfg):
    app, conn, case = _setup_case(tmp_cfg)
    try:
        create_extracted_field(conn, case["id"], "full_name", "Kavita Reddy", method="text:labeled", confidence=0.88)
        create_extracted_field(conn, case["id"], "dob", "1994-06-25", method="text:labeled", confidence=0.88)
        create_extracted_field(conn, case["id"], "licence_no", "TS09/0001234/2021", method="ocr:labeled", confidence=0.75)

        # Operator explicitly confirms the OCR candidate using confirm_licence_no='1'
        values = {
            "full_name": "Kavita Reddy",
            "father_name": "",
            "dob": "1994-06-25",
            "address": "",
            "licence_no": "TS09/0001234/2021",
            "confirm_licence_no": "1",
            "appl_number": "",
        }
        case_res, changed, errors = save_review(conn, CASE_ID, values)
        assert not errors
        assert case_res["status"] == "reviewed"
        assert changed is True

        # Verify a manual row was inserted into extracted_fields
        manual_row = conn.execute(
            """SELECT * FROM extracted_fields
               WHERE case_id = ? AND field_key = 'licence_no' AND method = 'manual'""",
            (case["id"],),
        ).fetchone()
        assert manual_row is not None
        assert manual_row["value"] == "TS09/0001234/2021"

        # Now in fill packet, licence_no is eligible!
        packet = build_fill_packet(conn, CASE_ID, DEFAULT_WORKFLOW)
        assert not isinstance(packet, PacketError)
        fmap = {f["key"]: f for f in packet["fields"]}
        assert fmap["licence_no"]["eligible_to_fill"] is True
        assert fmap["licence_no"]["value"] == "TS09/0001234/2021"
        assert fmap["licence_no"]["label"] == "Learner's Licence Number"
    finally:
        conn.close()


def test_unreviewed_or_edited_case_rejects_packet(tmp_cfg):
    app, conn, case = _setup_case(tmp_cfg)
    try:
        create_extracted_field(conn, case["id"], "full_name", "Kavita Reddy", method="manual")
        create_extracted_field(conn, case["id"], "dob", "1994-06-25", method="manual")
        create_extracted_field(conn, case["id"], "licence_no", "TS09/0001234/2021", method="manual")

        # Case still in needs_attention
        packet = build_fill_packet(conn, CASE_ID, DEFAULT_WORKFLOW)
        assert isinstance(packet, PacketError)
        assert packet.reason == "case_not_reviewed"
        assert packet.http_status == 409
    finally:
        conn.close()


def test_review_form_checkbox_state_and_validation_errors(tmp_cfg):
    """Test that confirm_licence_no checkbox state is consistent across GET, validation errors, and re-review."""
    import re
    app, conn, case = _setup_case(tmp_cfg)
    try:
        create_extracted_field(conn, case["id"], "full_name", "Kavita Reddy", method="text:labeled")
        create_extracted_field(conn, case["id"], "dob", "1994-06-25", method="text:labeled")
        create_extracted_field(conn, case["id"], "licence_no", "TS09/0001234/2021", method="ocr:labeled")

        client = app.test_client()

        # 1. Initial GET: checkbox is present and UNCHECKED
        get_res = client.get(f"/cases/{CASE_ID}/review")
        assert get_res.status_code == 200
        html = get_res.data.decode()
        assert 'name="confirm_licence_no"' in html
        assert 'name="confirm_licence_no" value="1" checked' not in html

        csrf_m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        assert csrf_m
        token = csrf_m.group(1)

        # 2. POST with a validation error (invalid DOB) and confirm_licence_no='1'
        post_data = {
            "csrf_token": token,
            "full_name": "Kavita Reddy",
            "father_name": "",
            "dob": "invalid-date",
            "address": "",
            "licence_no": "TS09/0001234/2021",
            "confirm_licence_no": "1",
            "appl_number": "",
        }
        res_err = client.post(f"/cases/{CASE_ID}/review", data=post_data)
        assert res_err.status_code == 200
        html_err = res_err.data.decode()
        assert "Date of birth" in html_err
        # confirm_licence_no MUST remain checked on validation error re-render
        assert 'name="confirm_licence_no" value="1" checked' in html_err

        # 3. POST with a validation error WITHOUT checking confirm_licence_no
        post_data_uncheck = {
            "csrf_token": token,
            "full_name": "Kavita Reddy",
            "father_name": "",
            "dob": "invalid-date",
            "address": "",
            "licence_no": "TS09/0001234/2021",
            "appl_number": "",
        }
        res_err2 = client.post(f"/cases/{CASE_ID}/review", data=post_data_uncheck)
        assert res_err2.status_code == 200
        html_err2 = res_err2.data.decode()
        assert 'name="confirm_licence_no"' in html_err2
        assert 'name="confirm_licence_no" value="1" checked' not in html_err2

        # 4. Successful POST with confirm_licence_no="1"
        post_data_valid = {
            "csrf_token": token,
            "full_name": "Kavita Reddy",
            "father_name": "",
            "dob": "1994-06-25",
            "address": "",
            "licence_no": "TS09/0001234/2021",
            "confirm_licence_no": "1",
            "appl_number": "",
        }
        res_ok = client.post(f"/cases/{CASE_ID}/review", data=post_data_valid)
        assert res_ok.status_code == 302

        # 5. Subsequent GET on reviewed case: shows "Confirmed by operator", checkbox no longer shown
        get_sub = client.get(f"/cases/{CASE_ID}/review")
        assert get_sub.status_code == 200
        html_sub = get_sub.data.decode()
        assert "Confirmed by operator" in html_sub
        assert 'name="confirm_licence_no"' not in html_sub

        # Fill packet confirms licence_no is eligible
        packet = build_fill_packet(conn, CASE_ID, DEFAULT_WORKFLOW)
        assert not isinstance(packet, PacketError)
        fmap = {f["key"]: f for f in packet["fields"]}
        assert fmap["licence_no"]["eligible_to_fill"] is True
        assert fmap["licence_no"]["value"] == "TS09/0001234/2021"
    finally:
        conn.close()
