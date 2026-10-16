"""Phase 6 tests: fill-packet service.

Covers packet structure, eligibility rules, reviewed-state enforcement,
conflict freshness, workflow allowlists, and data-isolation invariants.
Synthetic fixtures only — no real documents, PII, or external services.
"""
from __future__ import annotations

from app import create_app
from app.db import (
    connect,
    create_case,
    create_extracted_field,
    utcnow_iso,
)
from app.services.fill_packet import (
    DEFAULT_WORKFLOW,
    WORKFLOWS,
    PacketError,
    build_fill_packet,
)
from app.services.review import FIELD_LABELS, save_review


CASE = "CASE-20261016-FP1"


def _setup(tmp_cfg, case_id=CASE, status="needs_attention", appl=None):
    """Create app + case + connection, return (app, conn, case_row)."""
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    row = create_case(conn, case_id, appl_number=appl, status=status)
    return app, conn, row


def _seed_and_review(conn, case_id=CASE, extra_vals=None):
    """Seed minimal fields, review, and return refreshed case row."""
    case = conn.execute(
        "SELECT * FROM cases WHERE case_id = ?", (case_id,)
    ).fetchone()
    # Seed required extracted fields (OCR candidates)
    create_extracted_field(conn, case["id"], "full_name", "Ravi Kumar",
                          method="text:labeled", confidence=0.9)
    create_extracted_field(conn, case["id"], "dob", "1995-08-17",
                          method="text:labeled", confidence=0.9)
    # Now do the review save
    values = {
        "full_name": "Ravi Kumar", "father_name": "",
        "dob": "1995-08-17", "address": "123 Main St",
        "licence_no": "", "appl_number": "",
    }
    if extra_vals:
        values.update(extra_vals)
    case, changed, errors = save_review(conn, case_id, values)
    assert not errors, f"Review failed: {errors}"
    assert case["status"] == "reviewed"
    return case


# ------------------------------------------------------------------
# Packet structure & eligibility
# ------------------------------------------------------------------

def test_valid_reviewed_case_produces_correct_packet(tmp_cfg):
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn)
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert not isinstance(result, PacketError)
        assert result["case_id"] == CASE
        assert result["workflow"] == DEFAULT_WORKFLOW
        assert result["workflow_label"] == WORKFLOWS[DEFAULT_WORKFLOW]["label"]
        assert result["review_version"] == case["review_version"]
        assert result["reviewed_at"] is not None
        assert isinstance(result["fields"], list)
        # full_name and dob must be eligible
        fmap = {f["key"]: f for f in result["fields"]}
        assert fmap["full_name"]["eligible_to_fill"] is True
        assert fmap["full_name"]["value"] == "Ravi Kumar"
        assert fmap["dob"]["eligible_to_fill"] is True
        assert fmap["dob"]["value"] == "1995-08-17"
    finally:
        conn.close()


def test_confirmed_values_used_not_ocr_candidates(tmp_cfg):
    """Operator edits override OCR candidates in the packet."""
    app, conn, case_row = _setup(tmp_cfg)
    try:
        create_extracted_field(conn, case_row["id"], "full_name", "OCR Name",
                              method="text:labeled", confidence=0.5)
        create_extracted_field(conn, case_row["id"], "dob", "1990-01-01",
                              method="text:labeled", confidence=0.5)
        values = {
            "full_name": "Confirmed Name", "father_name": "",
            "dob": "1990-01-01", "address": "123 Main St",
            "licence_no": "", "appl_number": "",
        }
        case, changed, errors = save_review(conn, CASE, values)
        assert not errors, f"Review failed: {errors}"
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert not isinstance(result, PacketError)
        fmap = {f["key"]: f for f in result["fields"]}
        assert fmap["full_name"]["value"] == "Confirmed Name"
        assert fmap["full_name"]["value"] != "OCR Name"
    finally:
        conn.close()


def test_missing_optional_field_not_eligible(tmp_cfg):
    """Missing optional fields are not fabricated or marked eligible."""
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn)
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert not isinstance(result, PacketError)
        fmap = {f["key"]: f for f in result["fields"]}
        # licence_no was not provided
        assert fmap["licence_no"]["eligible_to_fill"] is False
        assert fmap["licence_no"]["value"] is None
        assert "reason" in fmap["licence_no"]
    finally:
        conn.close()


def test_application_no_and_licence_no_distinct(tmp_cfg):
    """application_no and licence_no remain distinct fields."""
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn, extra_vals={
            "licence_no": "KA0120190001234",
            "appl_number": "LD-2026/TEST-001",
        })
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert not isinstance(result, PacketError)
        fmap = {f["key"]: f for f in result["fields"]}
        assert fmap["licence_no"]["value"] == "KA0120190001234"
        assert fmap["application_no"]["value"] == "LD-2026/TEST-001"
        assert fmap["licence_no"]["key"] != fmap["application_no"]["key"]
    finally:
        conn.close()


def test_only_allowlisted_fields_returned(tmp_cfg):
    """Packet only contains fields in the workflow allowlist."""
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn)
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert not isinstance(result, PacketError)
        keys = {f["key"] for f in result["fields"]}
        allowed = set(WORKFLOWS[DEFAULT_WORKFLOW]["fields"])
        assert keys == allowed
    finally:
        conn.close()


def test_no_ocr_text_or_doc_paths_in_packet(tmp_cfg):
    """Packet must not contain OCR text, document paths, or other internals."""
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn)
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        import json
        raw = json.dumps(result)
        for banned in ("orig_path", "prepared_path", "sha256", "document_id",
                       "confidence", "source_page", "method"):
            assert banned not in raw, f"Leaked internal field: {banned}"
    finally:
        conn.close()


def test_correct_labels_from_field_labels(tmp_cfg):
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn)
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert not isinstance(result, PacketError)
        for f in result["fields"]:
            assert f["label"] == FIELD_LABELS[f["key"]]
    finally:
        conn.close()


# ------------------------------------------------------------------
# Rejection cases
# ------------------------------------------------------------------

def test_unreviewed_case_rejected(tmp_cfg):
    app, conn, _ = _setup(tmp_cfg, status="needs_attention")
    try:
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert isinstance(result, PacketError)
        assert result.reason == "case_not_reviewed"
        assert result.http_status == 409
    finally:
        conn.close()


def test_missing_case_rejected(tmp_cfg):
    app, conn, _ = _setup(tmp_cfg)
    try:
        result = build_fill_packet(conn, "CASE-NONEXISTENT", DEFAULT_WORKFLOW)
        assert isinstance(result, PacketError)
        assert result.reason == "case_not_found"
    finally:
        conn.close()


def test_invalid_workflow_rejected(tmp_cfg):
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn)
        result = build_fill_packet(conn, CASE, "nonexistent_workflow")
        assert isinstance(result, PacketError)
        assert result.reason == "invalid_workflow"
    finally:
        conn.close()


def test_stale_review_after_edit_rejected(tmp_cfg):
    """Editing a reviewed case invalidates it; packet must be rejected."""
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn)
        # Simulate an edit that invalidates the review
        from app.db import touch_case_for_edit
        touch_case_for_edit(conn, CASE)
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert isinstance(result, PacketError)
        assert result.reason == "case_not_reviewed"
    finally:
        conn.close()


def test_unresolved_conflict_blocks_packet(tmp_cfg):
    """A case with unresolved conflicts cannot produce a fill packet."""
    app, conn, _ = _setup(tmp_cfg)
    try:
        # Create conflicting candidates
        create_extracted_field(conn, 1, "full_name", "Alice Smith",
                              method="text:labeled", confidence=0.9)
        create_extracted_field(conn, 1, "full_name", "Alice Jones",
                              method="text:labeled", confidence=0.9)
        create_extracted_field(conn, 1, "dob", "1990-01-01",
                              method="text:labeled", confidence=0.9)
        # Review resolving the conflict
        values = {
            "full_name": "Alice Smith", "father_name": "",
            "dob": "1990-01-01", "address": "",
            "licence_no": "", "appl_number": "",
            "choice_full_name": "Alice Smith",
        }
        case, changed, errors = save_review(conn, CASE, values)
        assert not errors

        # Now add new conflicting evidence
        create_extracted_field(conn, 1, "full_name", "Alice Brown",
                              method="text:labeled", confidence=0.9)
        # Case must be set to needs_attention in a real scenario, but
        # let's verify the packet rejects even if status is still reviewed
        # (the conflict_status check catches fresh conflicts)
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert isinstance(result, PacketError)
        assert result.reason == "unresolved_conflicts"
    finally:
        conn.close()


def test_new_evidence_on_reviewed_case_requires_rereview(tmp_cfg):
    """New evidence attached via attach_or_provisional invalidates review."""
    from app.services.match import attach_or_provisional
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn)
        assert case["status"] == "reviewed"

        # Create a document and attach new agreeing evidence
        from app.db import create_document
        doc = create_document(conn, orig_filename="new.pdf", orig_path="new.pdf")
        candidates = [
            {"field_key": "full_name", "value": "Ravi Kumar",
             "method": "text:labeled", "confidence": 0.9},
            {"field_key": "dob", "value": "1995-08-17",
             "method": "text:labeled", "confidence": 0.9},
        ]
        case_after, outcome = attach_or_provisional(conn, doc["id"], candidates)
        # Even agreeing evidence must invalidate review
        assert case_after["status"] != "reviewed"
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert isinstance(result, PacketError)
        assert result.reason == "case_not_reviewed"
    finally:
        conn.close()


def test_packet_includes_review_version_and_timestamp(tmp_cfg):
    app, conn, _ = _setup(tmp_cfg)
    try:
        case = _seed_and_review(conn)
        result = build_fill_packet(conn, CASE, DEFAULT_WORKFLOW)
        assert not isinstance(result, PacketError)
        assert "review_version" in result
        assert isinstance(result["review_version"], int)
        assert "reviewed_at" in result
        assert result["reviewed_at"] is not None
    finally:
        conn.close()
