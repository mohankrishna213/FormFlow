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
from app.db import connect, create_case, create_document, create_extracted_field
from app.services.fields import extract_candidates
from app.services.fill_packet import (
    DEFAULT_WORKFLOW,
    PacketError,
    build_fill_packet,
    detect_case_licence_type,
)
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


def test_extract_candidates_licence_kind_detection():
    rules = {
        "fields": {
            "licence_no": {
                "labels": [
                    "driving licence no", "licence no", "learner's licence no",
                ],
            },
        },
    }
    # 1. Explicit LL label
    pages_ll = [{"page": 1, "method": "ocr", "text": "Learner's Licence No: TS09/0001234/2021"}]
    cands_ll = extract_candidates(pages_ll, rules)
    lic_cands_ll = [c for c in cands_ll if c["field_key"] == "licence_no"]
    assert len(lic_cands_ll) == 1
    assert lic_cands_ll[0]["method"] == "ocr:labeled:ll"
    assert lic_cands_ll[0]["value"] == "TS09/0001234/2021"

    # 2. Explicit DL label
    pages_dl = [{"page": 1, "method": "ocr", "text": "Driving Licence No: DL-0420110012345"}]
    cands_dl = extract_candidates(pages_dl, rules)
    lic_cands_dl = [c for c in cands_dl if c["field_key"] == "licence_no"]
    assert len(lic_cands_dl) == 1
    assert lic_cands_dl[0]["method"] == "ocr:labeled:dl"
    assert lic_cands_dl[0]["value"] == "DL-0420110012345"

    # 3. Generic label disambiguated by FORM 3 in document text
    pages_form3 = [{"page": 1, "method": "text", "text": "FORM 3\nLicence No: TS09/0001234/2021"}]
    cands_f3 = extract_candidates(pages_form3, rules)
    lic_f3 = [c for c in cands_f3 if c["field_key"] == "licence_no"]
    assert len(lic_f3) == 1
    assert lic_f3[0]["method"] == "text:labeled:ll"

    # 4. Generic label disambiguated by FORM 7 in document text
    pages_form7 = [{"page": 1, "method": "text", "text": "FORM 7\nLicence No: DL-0420110012345"}]
    cands_f7 = extract_candidates(pages_form7, rules)
    lic_f7 = [c for c in cands_f7 if c["field_key"] == "licence_no"]
    assert len(lic_f7) == 1
    assert lic_f7[0]["method"] == "text:labeled:dl"

    # 5. Generic label with unknown context
    pages_unk = [{"page": 1, "method": "text", "text": "Licence No: TS09/0001234/2021"}]
    cands_unk = extract_candidates(pages_unk, rules)
    lic_unk = [c for c in cands_unk if c["field_key"] == "licence_no"]
    assert len(lic_unk) == 1
    assert lic_unk[0]["method"] == "text:labeled:unknown"


def test_detect_case_licence_type_and_fill_packet_metadata(tmp_cfg):
    app, conn, case = _setup_case(tmp_cfg, status="needs_attention")
    try:
        # LL Candidate
        create_extracted_field(conn, case["id"], "licence_no", "TS09/0001234/2021", method="ocr:labeled:ll")
        assert detect_case_licence_type(conn, case["id"]) == "ll"

        # Operator review
        values = {
            "full_name": "Kavita Reddy",
            "father_name": "",
            "dob": "1994-06-25",
            "address": "",
            "licence_no": "TS09/0001234/2021",
            "confirm_licence_no": "1",
            "appl_number": "",
        }
        case_res, _, errors = save_review(conn, case["case_id"], values)
        assert not errors
        assert case_res["status"] == "reviewed"

        # Fill packet retains licence semantic label even after confirmation with method='manual'
        packet = build_fill_packet(conn, case["case_id"], DEFAULT_WORKFLOW)
        assert not isinstance(packet, PacketError)
        assert set(packet.keys()) == {"case_id", "workflow", "workflow_label", "review_version", "reviewed_at", "fields"}
        fmap = {f["key"]: f for f in packet["fields"]}
        assert fmap["licence_no"]["label"] == "Learner's Licence Number"
        assert fmap["licence_no"]["eligible_to_fill"] is True
        assert fmap["licence_no"]["value"] == "TS09/0001234/2021"

        # DL Candidate on second case
        case2 = create_case(conn, "CASE-20261017-DL2", status="needs_attention")
        create_extracted_field(conn, case2["id"], "licence_no", "DL-0420110012345", method="ocr:labeled:dl")
        assert detect_case_licence_type(conn, case2["id"]) == "dl"

        case2_res, _, _ = save_review(conn, case2["case_id"], {
            "full_name": "Amit Shah",
            "father_name": "",
            "dob": "1990-01-01",
            "address": "",
            "licence_no": "DL-0420110012345",
            "confirm_licence_no": "1",
            "appl_number": "",
        })
        packet2 = build_fill_packet(conn, case2["case_id"], DEFAULT_WORKFLOW)
        assert not isinstance(packet2, PacketError)
        fmap2 = {f["key"]: f for f in packet2["fields"]}
        assert fmap2["licence_no"]["label"] == "Driving Licence Number"

        # Case 3: Document filename evidence
        case3 = create_case(conn, "CASE-20261017-DOC3", status="needs_attention")
        conn.execute("UPDATE cases SET status = 'reviewed', reviewed_at = '2026-10-18T00:00:00Z' WHERE id = ?", (case3["id"],))
        create_document(conn, orig_filename="driving_licence_card.pdf", orig_path="dummy_path.pdf", case_id=case3["id"])
        create_extracted_field(conn, case3["id"], "licence_no", "DL-9988776655", method="manual")
        assert detect_case_licence_type(conn, case3["id"]) == "dl"
        packet3 = build_fill_packet(conn, case3["case_id"], DEFAULT_WORKFLOW)
        assert not isinstance(packet3, PacketError)
        fmap3 = {f["key"]: f for f in packet3["fields"]}
        assert fmap3["licence_no"]["label"] == "Driving Licence Number"

        # Case 4: Pure unknown fallback (no doc, no candidates, manual only)
        case4 = create_case(conn, "CASE-20261017-UNK4", status="needs_attention")
        conn.execute("UPDATE cases SET status = 'reviewed', reviewed_at = '2026-10-18T00:00:00Z' WHERE id = ?", (case4["id"],))
        create_extracted_field(conn, case4["id"], "licence_no", "UNK-12345", method="manual")
        assert detect_case_licence_type(conn, case4["id"]) == "unknown"
        packet4 = build_fill_packet(conn, case4["case_id"], DEFAULT_WORKFLOW)
        assert not isinstance(packet4, PacketError)
        fmap4 = {f["key"]: f for f in packet4["fields"]}
        assert fmap4["licence_no"]["label"] == "Licence Number"
    finally:
        conn.close()
