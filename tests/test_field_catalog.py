"""Phase 2 tests: Extensible Field Catalogue, Dynamic Review, and Profile Precedence.

Covers:
- Reusable field catalogue definitions, canonical resolution, and aliases.
- Security constraints: key syntax, length bounds, reserved keys, value bounds.
- Retention and display of unrecognised / arbitrary extracted keys in review.
- Multi-candidate storage, provenance preservation, and conflict resolution.
- Customer-level reusable profile vs case-specific data precedence.
- Customer-scoped sync on review confirmation.
- Autofill eligibility invariants: confirmed vs unconfirmed OCR candidates in fill packets.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from app import create_app
from app.db import (
    connect,
    create_case,
    create_customer,
    create_document,
    create_extracted_field,
    get_customer_fields,
    init_db,
    upsert_customer_field,
)
from app.services.catalog import (
    CATEGORIES,
    MAX_FIELDS_PER_CASE,
    MAX_KEY_LENGTH,
    MAX_VALUE_LENGTH,
    RESERVED_KEYS,
    FieldDef,
    get_catalog,
    sanitize_field_value,
    validate_field_key,
)
from app.services.fill_packet import PacketError, build_fill_packet
from app.services.review import (
    FIELD_LABELS,
    FORM_KEY,
    KEEP_BLANK,
    best_and_candidates,
    categorized_fields,
    conflict_status,
    key_conflicts,
    review_prefill,
    save_review,
    with_sources,
)


def _setup_app(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    return app, conn


# ===========================================================================
# 1. Field Catalogue & Alias Model Tests
# ===========================================================================

def test_catalog_default_definitions_present():
    """Default catalogue contains canonical core and extended fields."""
    cat = get_catalog()
    assert cat.get("full_name") is not None
    assert cat.get("dob") is not None
    assert cat.get("father_name") is not None
    assert cat.get("gender") is not None
    assert cat.get("blood_group") is not None
    assert cat.get("mobile_no") is not None
    assert cat.get("email") is not None
    assert cat.get("address") is not None
    assert cat.get("pres_pincode") is not None
    assert cat.get("licence_no") is not None
    assert cat.get("application_no") is not None


def test_catalog_alias_resolution():
    """Aliases resolve deterministically to canonical catalogue keys."""
    cat = get_catalog()
    assert cat.resolve_key("name") == "full_name"
    assert cat.resolve_key("applicant_name") == "full_name"
    assert cat.resolve_key("birth_date") == "dob"
    assert cat.resolve_key("date_of_birth") == "dob"
    assert cat.resolve_key("sex") == "gender"
    assert cat.resolve_key("mobile") == "mobile_no"
    assert cat.resolve_key("phone") == "mobile_no"
    assert cat.resolve_key("email_id") == "email"
    assert cat.resolve_key("pincode") == "pres_pincode"
    assert cat.resolve_key("appl_no") == "application_no"
    assert cat.resolve_key("dl_no") == "licence_no"
    assert cat.resolve_key("ll_no") == "licence_no"


def test_catalog_labels_and_categories():
    """Labels and categories are assigned properly, with friendly fallbacks."""
    cat = get_catalog()
    assert cat.get_label("full_name") == "Full name"
    assert cat.get_label("mobile_no") == "Mobile number"
    assert cat.get_category("full_name") == "identity"
    assert cat.get_category("mobile_no") == "contact"
    assert cat.get_category("address") == "address"
    assert cat.get_category("licence_no") == "licence"
    assert cat.get_category("application_no") == "application"

    # Unregistered key gets dynamic friendly label and 'other' category
    assert cat.get_label("voter_card_no") == "Voter card no"
    assert cat.get_category("voter_card_no") == "other"


# ===========================================================================
# 2. Security Limits & Key/Value Validation Tests
# ===========================================================================

def test_field_key_validation_security_rules():
    """Field keys must adhere to strict alphanumeric/underscore syntax and bounds."""
    # Valid keys
    validate_field_key("full_name")
    validate_field_key("mobile_no")
    validate_field_key("tax_id_2026")

    # Invalid: empty or non-string
    with pytest.raises(ValueError, match="non-empty string"):
        validate_field_key("")

    # Invalid: starts with number or underscore
    with pytest.raises(ValueError, match="Invalid field key"):
        validate_field_key("123name")
    with pytest.raises(ValueError, match="Invalid field key"):
        validate_field_key("_field")

    # Invalid: uppercase or special characters
    with pytest.raises(ValueError, match="Invalid field key"):
        validate_field_key("FullName")
    with pytest.raises(ValueError, match="Invalid field key"):
        validate_field_key("field-name")
    with pytest.raises(ValueError, match="Invalid field key"):
        validate_field_key("field.name")

    # Invalid: too long (> 64 chars)
    with pytest.raises(ValueError, match="exceeds maximum length"):
        validate_field_key("a" * (MAX_KEY_LENGTH + 1))

    # Invalid: reserved keys
    for rk in RESERVED_KEYS:
        with pytest.raises(ValueError, match="reserved internal identifier"):
            validate_field_key(rk)


def test_field_value_sanitization():
    """Values are bounded in length and stripped of dangerous control characters."""
    # Bounded length (max 1000)
    huge_str = "x" * 2000
    cleaned = sanitize_field_value(huge_str)
    assert len(cleaned) == MAX_VALUE_LENGTH

    # Strips control characters except newlines/tabs
    dirty = "Hello\x00\x07World\t\nTest\x1b"
    cleaned = sanitize_field_value(dirty)
    assert cleaned == "HelloWorld\t\nTest"


# ===========================================================================
# 3. Arbitrary Extracted Keys & Provenance Tests
# ===========================================================================

def test_arbitrary_extracted_keys_retained_and_surfaced(tmp_cfg):
    """Unrecognised extracted keys are preserved with provenance and displayed in review."""
    app, conn = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-ARB-01", status="needs_attention")
        doc = create_document(conn, orig_filename="voter_slip.pdf", orig_path="data/voter_slip.pdf")

        # Standard fields
        create_extracted_field(conn, case["id"], "full_name", "Kiran Rao", document_id=doc["id"], confidence=0.9)
        create_extracted_field(conn, case["id"], "dob", "1993-04-15", document_id=doc["id"], confidence=0.9)

        # Arbitrary / non-catalogue keys
        create_extracted_field(
            conn, case["id"], "voter_epic_no", "WXY9876543",
            document_id=doc["id"], source_page=1, method="ocr:labeled", confidence=0.88,
        )
        create_extracted_field(
            conn, case["id"], "polling_booth_code", "PB-42",
            document_id=doc["id"], source_page=2, method="ocr:table", confidence=0.82,
        )

        info = best_and_candidates(conn, case["id"])
        conflicts = key_conflicts(conn, case["id"])
        info, conflicts = with_sources(conn, info, conflicts)

        # Both custom keys must be retained and present
        assert "voter_epic_no" in info
        assert "polling_booth_code" in info

        epic = info["voter_epic_no"]["best"]
        assert epic["value"] == "WXY9876543"
        assert epic["method"] == "ocr:labeled"
        assert epic["confidence"] == 0.88
        assert epic["source_page"] == 1
        assert epic["_doc"] == "voter_slip.pdf"

        # Verify categorization groups custom keys under 'other'
        cats = categorized_fields(info)
        cat_map = {c[0]: c[2] for c in cats}
        assert "voter_epic_no" in cat_map["other"]
        assert "polling_booth_code" in cat_map["other"]
    finally:
        conn.close()


# ===========================================================================
# 4. Multi-Candidate Provenance and Conflict Freshness on Custom Fields
# ===========================================================================

def test_custom_field_conflicts_and_resolution(tmp_cfg):
    """Multiple conflicting candidates on custom keys block save until resolved."""
    app, conn = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-CONF-CUSTOM", status="needs_attention")
        doc1 = create_document(conn, orig_filename="doc1.pdf", orig_path="doc1.pdf")
        doc2 = create_document(conn, orig_filename="doc2.pdf", orig_path="doc2.pdf")

        create_extracted_field(conn, case["id"], "full_name", "Ramesh Reddy", method="manual")
        create_extracted_field(conn, case["id"], "dob", "1985-02-10", method="manual")

        # Conflicting custom field candidates
        create_extracted_field(conn, case["id"], "mobile_no", "9876543210", document_id=doc1["id"], method="ocr:labeled")
        create_extracted_field(conn, case["id"], "mobile_no", "9123456780", document_id=doc2["id"], method="ocr:labeled")

        info = best_and_candidates(conn, case["id"])
        conflicts = key_conflicts(conn, case["id"])
        assert "mobile_no" in conflicts
        assert len(conflicts["mobile_no"]) == 2

        prefill = review_prefill(info, case)
        status = conflict_status(conn, case, info, conflicts)
        assert status["mobile_no"]["need_pick"] is True

        # Unchanged save without picking is blocked
        values = {
            "full_name": "Ramesh Reddy",
            "dob": "1985-02-10",
            "mobile_no": prefill.get("mobile_no", ""),
        }
        case_res, changed, errors = save_review(conn, "CASE-CONF-CUSTOM", values)
        assert "mobile_no" in errors
        assert "Conflicting values found" in errors["mobile_no"]

        # Resolve via candidate pick choice_mobile_no
        values["choice_mobile_no"] = "9876543210"
        case_res2, changed2, errors2 = save_review(conn, "CASE-CONF-CUSTOM", values)
        assert not errors2
        assert case_res2["status"] == "reviewed"

        # Confirmed row in extracted_fields
        latest = conn.execute(
            "SELECT * FROM extracted_fields WHERE case_id = ? AND field_key = 'mobile_no' ORDER BY id DESC LIMIT 1",
            (case["id"],),
        ).fetchone()
        assert latest["value"] == "9876543210"
        assert latest["method"] == "manual"
    finally:
        conn.close()


# ===========================================================================
# 5. Customer Profile vs Case-Specific Precedence Tests
# ===========================================================================

def test_customer_profile_fallback_and_case_precedence(tmp_cfg):
    """Case data always takes precedence over customer profile data."""
    app, conn = _setup_app(tmp_cfg)
    try:
        # 1. Create a customer with confirmed profile data
        cust = create_customer(conn, "Sunita Verma")
        upsert_customer_field(conn, cust["id"], "full_name", "Sunita Verma")
        upsert_customer_field(conn, cust["id"], "dob", "1991-07-22")
        upsert_customer_field(conn, cust["id"], "gender", "Female")
        upsert_customer_field(conn, cust["id"], "mobile_no", "9988776655")
        upsert_customer_field(conn, cust["id"], "address", "Flat 402, Green Towers, Hyderabad")

        # 2. Case 1: has customer_id, but no case-specific mobile_no in extracted_fields
        case1 = create_case(conn, "CASE-CUST-01", customer_id=cust["id"], status="needs_attention")
        create_extracted_field(conn, case1["id"], "full_name", "Sunita Verma", method="manual")
        create_extracted_field(conn, case1["id"], "dob", "1991-07-22", method="manual")

        info1 = best_and_candidates(conn, case1["id"])
        # mobile_no and gender must fall back to customer profile
        assert info1["mobile_no"]["best"] is not None
        assert info1["mobile_no"]["best"]["value"] == "9988776655"
        assert info1["mobile_no"]["best"]["method"] == "customer_profile"

        assert info1["gender"]["best"] is not None
        assert info1["gender"]["best"]["value"] == "Female"
        assert info1["gender"]["best"]["method"] == "customer_profile"

        # 3. Case 2: case-level evidence exists for mobile_no -> case evidence MUST override customer profile
        case2 = create_case(conn, "CASE-CUST-02", customer_id=cust["id"], status="needs_attention")
        create_extracted_field(conn, case2["id"], "full_name", "Sunita Verma", method="manual")
        create_extracted_field(conn, case2["id"], "dob", "1991-07-22", method="manual")
        doc = create_document(conn, orig_filename="recent_bill.pdf", orig_path="recent_bill.pdf")
        create_extracted_field(
            conn, case2["id"], "mobile_no", "9111222333",
            document_id=doc["id"], method="ocr:labeled", confidence=0.95,
        )

        info2 = best_and_candidates(conn, case2["id"])
        assert info2["mobile_no"]["best"] is not None
        # Must be the case-level OCR candidate, NOT the customer profile value!
        assert info2["mobile_no"]["best"]["value"] == "9111222333"
        assert info2["mobile_no"]["best"]["method"] == "ocr:labeled"
        assert info2["mobile_no"]["best"]["document_id"] == doc["id"]
    finally:
        conn.close()


def test_customer_scoped_fields_sync_on_save(tmp_cfg):
    """Customer-scoped fields sync to customer_fields, while case-scoped fields stay strictly on the case."""
    app, conn = _setup_app(tmp_cfg)
    try:
        cust = create_customer(conn, "Pooja Hegde")
        case = create_case(conn, "CASE-SYNC-01", customer_id=cust["id"], status="needs_attention")

        values = {
            "full_name": "Pooja Hegde",
            "dob": "1994-10-18",
            "father_name": "Manjunath Hegde",
            "gender": "Female",
            "mobile_no": "9000011111",
            "email": "pooja@example.com",
            "address": "Banjara Hills, Hyderabad",
            "stay_duration": "5 years",       # case-scoped
            "application_no": "9998887776",   # case-scoped (cases.appl_number)
            "licence_no": "TS-0920200001234", # case-scoped
        }

        saved_case, changed, errors = save_review(conn, "CASE-SYNC-01", values)
        assert not errors
        assert saved_case["status"] == "reviewed"

        # Check customer profile table
        c_fields = get_customer_fields(conn, cust["id"])

        # Customer-scoped fields MUST be synchronized
        assert c_fields["full_name"]["value"] == "Pooja Hegde"
        assert c_fields["dob"]["value"] == "1994-10-18"
        assert c_fields["father_name"]["value"] == "Manjunath Hegde"
        assert c_fields["gender"]["value"] == "Female"
        assert c_fields["mobile_no"]["value"] == "9000011111"
        assert c_fields["email"]["value"] == "pooja@example.com"
        assert c_fields["address"]["value"] == "Banjara Hills, Hyderabad"

        # Case-scoped fields MUST NOT be written to customer profile
        assert "application_no" not in c_fields
        assert "licence_no" not in c_fields
        assert "stay_duration" not in c_fields
    finally:
        conn.close()


# ===========================================================================
# 6. Fill Packet Eligibility & Workflow Independence Tests
# ===========================================================================

def test_unconfirmed_ocr_candidates_never_eligible_for_autofill(tmp_cfg):
    """Unconfirmed OCR candidates for any field are strictly ineligible in fill packets."""
    app, conn = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-ELIG-01", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "Arjun Reddy", method="manual")
        create_extracted_field(conn, case["id"], "dob", "1990-03-03", method="manual")

        # Unconfirmed OCR candidates
        create_extracted_field(conn, case["id"], "mobile_no", "9876500000", method="ocr:labeled", confidence=0.99)
        create_extracted_field(conn, case["id"], "address", "Road No 12, Jubilee Hills", method="ocr:labeled", confidence=0.98)
        create_extracted_field(conn, case["id"], "licence_no", "DL-1420110012345", method="ocr:labeled", confidence=0.97)

        # Review save without editing mobile_no, address, or confirming licence_no
        values = {
            "full_name": "Arjun Reddy",
            "dob": "1990-03-03",
            "father_name": "",
            "address": "Road No 12, Jubilee Hills",
            "licence_no": "DL-1420110012345",
            "appl_number": "",
        }
        case_row, changed, errs = save_review(conn, "CASE-ELIG-01", values)
        assert not errs
        assert case_row["status"] == "reviewed"

        # Build fill packet
        packet = build_fill_packet(conn, "CASE-ELIG-01", "auto")
        assert not isinstance(packet, PacketError)
        fmap = {f["key"]: f for f in packet["fields"]}

        # Confirmed identity fields are eligible
        assert fmap["full_name"]["eligible_to_fill"] is True
        assert fmap["full_name"]["value"] == "Arjun Reddy"
        assert fmap["dob"]["eligible_to_fill"] is True
        assert fmap["dob"]["value"] == "1990-03-03"

        # Unconfirmed fields MUST NOT be eligible despite being in review record!
        assert fmap["licence_no"]["eligible_to_fill"] is False
        assert fmap["licence_no"]["value"] is None
        assert fmap["licence_no"]["reason"] == "Value not confirmed by operator"

        assert fmap["address"]["eligible_to_fill"] is False
        assert fmap["address"]["value"] is None
        assert fmap["address"]["reason"] == "Value not confirmed by operator"

        # mobile_no was unconfirmed OCR candidate -> not in confirmed auto fields
        assert "mobile_no" not in fmap or fmap["mobile_no"]["eligible_to_fill"] is False
    finally:
        conn.close()


def test_auto_workflow_exposes_confirmed_extended_fields(tmp_cfg):
    """The 'auto' workflow exposes confirmed extended fields while fixed workflows stay isolated."""
    app, conn = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-AUTO-EXT", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "Deepak Chopra", method="manual")
        create_extracted_field(conn, case["id"], "dob", "1978-08-20", method="manual")
        # Operator confirms extended fields
        create_extracted_field(conn, case["id"], "mobile_no", "9848012345", method="manual")
        create_extracted_field(conn, case["id"], "gender", "Male", method="manual")
        conn.execute("UPDATE cases SET appl_number = '1234567890' WHERE id = ?", (case["id"],))

        values = {
            "full_name": "Deepak Chopra",
            "dob": "1978-08-20",
            "father_name": "",
            "address": "",
            "licence_no": "",
            "appl_number": "1234567890",
            "mobile_no": "9848012345",
            "gender": "Male",
        }
        case_res, _, errs = save_review(conn, "CASE-AUTO-EXT", values)
        assert not errs
        assert case_res["status"] == "reviewed"

        # 1. auto packet: contains extended confirmed fields
        auto_packet = build_fill_packet(conn, "CASE-AUTO-EXT", "auto")
        assert not isinstance(auto_packet, PacketError)
        auto_map = {f["key"]: f for f in auto_packet["fields"]}
        assert auto_map["mobile_no"]["eligible_to_fill"] is True
        assert auto_map["mobile_no"]["value"] == "9848012345"
        assert auto_map["gender"]["eligible_to_fill"] is True
        assert auto_map["gender"]["value"] == "Male"
        assert auto_map["application_no"]["eligible_to_fill"] is True

        # 2. app_status packet: strictly application_no and dob only
        status_packet = build_fill_packet(conn, "CASE-AUTO-EXT", "app_status")
        assert not isinstance(status_packet, PacketError)
        status_keys = [f["key"] for f in status_packet["fields"]]
        assert status_keys == ["application_no", "dob"]

        # 3. dl_application packet: strictly the 6 defined DL application fields
        dl_packet = build_fill_packet(conn, "CASE-AUTO-EXT", "dl_application")
        assert not isinstance(dl_packet, PacketError)
        dl_keys = [f["key"] for f in dl_packet["fields"]]
        assert dl_keys == ["full_name", "father_name", "dob", "address", "licence_no", "application_no"]
    finally:
        conn.close()
