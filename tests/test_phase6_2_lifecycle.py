"""Phase 6.2 regression tests: review confirmation semantics and evidence lifecycle.

Covers:
1. Review confirmation semantics:
   - First review with unchanged OCR-matching values is fill-eligible.
   - Subsequent review with changed values increments version and updates packet.
   - Explicit conflict choice confirmation.
   - Deliberately blank optional values remain unavailable, never fabricated.
   - Unconfirmed OCR candidates are never fill-eligible.
   - application_no confirmed in cases.appl_number, not candidate rows.
2. Evidence and review freshness across all mutation paths:
   - Newly attached agreeing evidence invalidates review and bumps review_version.
   - Newly attached conflicting evidence invalidates review and bumps review_version.
   - Manual document assignment (assign_document) invalidates review and bumps review_version.
   - Merge into reviewed target case (merge_cases) invalidates review and bumps review_version.
   - Packet API and events endpoint enforce review validity and reject stale versions.
   - Re-review restores fill-eligibility under the new review_version.
3. Session secret configuration:
   - Safe persistent local session secret generation.
   - FORMFLOW_SECRET_KEY override.
"""
from __future__ import annotations

import os
from pathlib import Path

from app import create_app
from app.config import FormFlowConfig, load_config
from app.db import (
    connect,
    create_case,
    create_document,
    create_extracted_field,
    get_case,
    utcnow_iso,
)
from app.services.api_auth import TOKEN_FILENAME, ensure_token
from app.services.fill_packet import (
    DEFAULT_WORKFLOW,
    PacketError,
    build_fill_packet,
)
from app.services.match import attach_or_provisional
from app.services.review import (
    assign_document,
    merge_cases,
    save_review,
)


def _setup_app(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    token_path = cfg.data_dir / TOKEN_FILENAME
    token = token_path.read_text(encoding="utf-8").strip()
    return app, conn, token


# ===========================================================================
# 1. Review confirmation semantics
# ===========================================================================

def test_first_review_unchanged_ocr_matching_values_are_fill_eligible(tmp_cfg):
    """Populated required fields accepted during Validate & Save are fill-eligible,
    even when matching OCR candidates and producing no method='manual' rows.
    """
    app, conn, token = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-REV-01", status="needs_attention")
        create_extracted_field(
            conn, case["id"], "full_name", "Ananya Sharma",
            method="text:labeled", confidence=0.92,
        )
        create_extracted_field(
            conn, case["id"], "dob", "1998-04-12",
            method="ocr:labeled", confidence=0.88,
        )

        # Before review, packet cannot be built
        res_before = build_fill_packet(conn, "CASE-REV-01")
        assert isinstance(res_before, PacketError)
        assert res_before.reason == "case_not_reviewed"

        # Operator validates the unchanged OCR candidate values
        values = {
            "full_name": "Ananya Sharma",
            "father_name": "",
            "dob": "1998-04-12",
            "address": "",
            "licence_no": "",
            "appl_number": "DL-2026-9999",
        }
        reviewed_case, changed, errors = save_review(conn, "CASE-REV-01", values)
        assert not errors
        assert reviewed_case["status"] == "reviewed"
        assert reviewed_case["reviewed_at"] is not None
        assert reviewed_case["appl_number"] == "DL-2026-9999"

        # Verify no manual rows were created for the unchanged full_name/dob
        manual_rows = conn.execute(
            "SELECT * FROM extracted_fields WHERE case_id = ? AND method = 'manual'",
            (case["id"],),
        ).fetchall()
        assert len(manual_rows) == 0

        # Build fill packet: full_name and dob must be eligible
        packet = build_fill_packet(conn, "CASE-REV-01")
        assert not isinstance(packet, PacketError)
        fmap = {f["key"]: f for f in packet["fields"]}

        assert fmap["full_name"]["eligible_to_fill"] is True
        assert fmap["full_name"]["value"] == "Ananya Sharma"
        assert "reason" not in fmap["full_name"]

        assert fmap["dob"]["eligible_to_fill"] is True
        assert fmap["dob"]["value"] == "1998-04-12"
        assert "reason" not in fmap["dob"]

        assert fmap["application_no"]["eligible_to_fill"] is True
        assert fmap["application_no"]["value"] == "DL-2026-9999"

        # Optional fields not provided remain unavailable
        assert fmap["father_name"]["eligible_to_fill"] is False
        assert fmap["father_name"]["value"] is None
        assert fmap["father_name"]["reason"] == "Value not available"
    finally:
        conn.close()


def test_subsequent_review_with_changed_values_bumps_version_and_updates_packet(tmp_cfg):
    """Subsequent edit of a reviewed case increments review_version and updates packet."""
    app, conn, token = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-REV-02", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "Original Name", method="text:labeled")
        create_extracted_field(conn, case["id"], "dob", "1990-01-01", method="text:labeled")

        # Initial review
        v1 = {"full_name": "Original Name", "father_name": "", "dob": "1990-01-01",
              "address": "", "licence_no": "", "appl_number": "APPL-1"}
        c1, _, errs = save_review(conn, "CASE-REV-02", v1)
        assert not errs
        assert c1["review_version"] == 1

        # Subsequent review with an edit
        v2 = {"full_name": "Edited Name", "father_name": "", "dob": "1990-01-01",
              "address": "456 Park Ave", "licence_no": "", "appl_number": "APPL-1"}
        c2, changed, errs2 = save_review(conn, "CASE-REV-02", v2)
        assert not errs2
        assert changed is True
        assert c2["status"] == "reviewed"
        assert c2["review_version"] == 2

        # Fill packet reflects review_version 2 and edited values
        packet = build_fill_packet(conn, "CASE-REV-02")
        assert not isinstance(packet, PacketError)
        assert packet["review_version"] == 2
        fmap = {f["key"]: f for f in packet["fields"]}
        assert fmap["full_name"]["value"] == "Edited Name"
        assert fmap["full_name"]["eligible_to_fill"] is True
        assert fmap["address"]["value"] == "456 Park Ave"
        assert fmap["address"]["eligible_to_fill"] is True
    finally:
        conn.close()


def test_deliberately_blank_optional_field_remains_unavailable_not_fabricated(tmp_cfg):
    """Deliberately blanking an optional field leaves it unavailable, never fabricated."""
    app, conn, token = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-REV-03", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "John Doe", method="text:labeled")
        create_extracted_field(conn, case["id"], "dob", "1992-02-02", method="text:labeled")
        create_extracted_field(conn, case["id"], "father_name", "Richard Doe", method="text:labeled")

        # Review with father_name deliberately left blank
        values = {
            "full_name": "John Doe",
            "father_name": "",
            "dob": "1992-02-02",
            "address": "",
            "licence_no": "",
            "appl_number": "",
        }
        case_row, _, errs = save_review(conn, "CASE-REV-03", values)
        assert not errs
        assert case_row["status"] == "reviewed"

        packet = build_fill_packet(conn, "CASE-REV-03")
        assert not isinstance(packet, PacketError)
        fmap = {f["key"]: f for f in packet["fields"]}
        assert fmap["father_name"]["eligible_to_fill"] is False
        assert fmap["father_name"]["value"] is None
        assert fmap["father_name"]["reason"] == "Value not available"
    finally:
        conn.close()


def test_ocr_candidate_without_operator_confirmation_never_eligible(tmp_cfg):
    """An OCR candidate on an optional field with no operator confirmation is not eligible."""
    app, conn, token = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-REV-04", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "Kavita Rao", method="manual")
        create_extracted_field(conn, case["id"], "dob", "1988-11-20", method="manual")
        # Add high-confidence OCR candidates for optional fields
        create_extracted_field(conn, case["id"], "licence_no", "DL-KA0119880001", method="ocr:labeled", confidence=0.99)
        create_extracted_field(conn, case["id"], "address", "MG Road Bengaluru", method="ocr:labeled", confidence=0.95)

        # Mark case reviewed directly (simulating raw state without manual confirmation on optional fields)
        now = utcnow_iso()
        conn.execute(
            "UPDATE cases SET status = 'reviewed', reviewed_at = ?, review_version = 1 WHERE id = ?",
            (now, case["id"]),
        )
        conn.commit()

        packet = build_fill_packet(conn, "CASE-REV-04")
        assert not isinstance(packet, PacketError)
        fmap = {f["key"]: f for f in packet["fields"]}

        # Confirmed required fields are eligible
        assert fmap["full_name"]["eligible_to_fill"] is True
        assert fmap["dob"]["eligible_to_fill"] is True

        # Unconfirmed optional OCR candidates are NOT eligible
        assert fmap["licence_no"]["eligible_to_fill"] is False
        assert fmap["licence_no"]["value"] is None
        assert fmap["licence_no"]["reason"] == "Value not confirmed by operator"

        assert fmap["address"]["eligible_to_fill"] is False
        assert fmap["address"]["value"] is None
        assert fmap["address"]["reason"] == "Value not confirmed by operator"
    finally:
        conn.close()


def test_application_no_confirmed_in_cases_table_not_candidate_rows(tmp_cfg):
    """application_no is confirmed strictly via cases.appl_number; OCR candidates in
    extracted_fields are never exposed as confirmed.
    """
    app, conn, token = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-REV-05", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "Sanjay Dutt", method="manual")
        create_extracted_field(conn, case["id"], "dob", "1975-07-29", method="manual")
        create_extracted_field(conn, case["id"], "application_no", "APPL-CANDIDATE-ONLY", method="text:labeled")

        now = utcnow_iso()
        conn.execute(
            "UPDATE cases SET status = 'reviewed', reviewed_at = ?, appl_number = NULL WHERE id = ?",
            (now, case["id"]),
        )
        conn.commit()

        # application_no must be ineligible when cases.appl_number is empty
        packet1 = build_fill_packet(conn, "CASE-REV-05")
        fmap1 = {f["key"]: f for f in packet1["fields"]}
        assert fmap1["application_no"]["eligible_to_fill"] is False
        assert fmap1["application_no"]["value"] is None
        assert fmap1["application_no"]["reason"] == "Value not available"

        # Setting cases.appl_number confirms it
        conn.execute("UPDATE cases SET appl_number = 'OFFICIAL-12345' WHERE id = ?", (case["id"],))
        conn.commit()

        packet2 = build_fill_packet(conn, "CASE-REV-05")
        fmap2 = {f["key"]: f for f in packet2["fields"]}
        assert fmap2["application_no"]["eligible_to_fill"] is True
        assert fmap2["application_no"]["value"] == "OFFICIAL-12345"
        assert "reason" not in fmap2["application_no"]

        # Ensure no manual row was inserted into extracted_fields for application_no
        appl_rows = conn.execute(
            "SELECT * FROM extracted_fields WHERE case_id = ? AND field_key = 'application_no' AND method = 'manual'",
            (case["id"],),
        ).fetchall()
        assert len(appl_rows) == 0
    finally:
        conn.close()


def test_conflict_choice_resolution_and_freshness_invalidation(tmp_cfg):
    """Explicit conflict resolution choice confirms value; subsequent fresh conflict blocks packet."""
    app, conn, token = _setup_app(tmp_cfg)
    try:
        case = create_case(conn, "CASE-REV-CONF", status="needs_attention")
        create_extracted_field(conn, case["id"], "full_name", "Meera Nair", method="manual")
        # Conflicting candidates for dob
        create_extracted_field(conn, case["id"], "dob", "1985-05-15", method="text:labeled", confidence=0.9)
        create_extracted_field(conn, case["id"], "dob", "1985-06-20", method="text:labeled", confidence=0.8)

        # Operator explicitly resolves conflict by picking the first candidate
        values = {
            "full_name": "Meera Nair",
            "father_name": "",
            "dob": "1985-05-15",
            "address": "",
            "licence_no": "",
            "appl_number": "",
            "choice_dob": "1985-05-15",
        }
        case_row, _, errs = save_review(conn, "CASE-REV-CONF", values)
        assert not errs
        assert case_row["status"] == "reviewed"

        # Packet has the resolved value and is eligible
        packet = build_fill_packet(conn, "CASE-REV-CONF")
        assert not isinstance(packet, PacketError)
        fmap = {f["key"]: f for f in packet["fields"]}
        assert fmap["dob"]["eligible_to_fill"] is True
        assert fmap["dob"]["value"] == "1985-05-15"

        # New conflicting evidence arrives after review
        create_extracted_field(conn, case["id"], "dob", "1985-12-31", method="text:labeled", confidence=0.9)

        # Even before background worker calls touch_case_for_edit, conflict freshness analysis
        # catches the fresh conflict and blocks the packet with 409 unresolved_conflicts
        packet_after = build_fill_packet(conn, "CASE-REV-CONF")
        assert isinstance(packet_after, PacketError)
        assert packet_after.reason == "unresolved_conflicts"
        assert packet_after.http_status == 409
    finally:
        conn.close()


# ===========================================================================
# 2. Evidence and review freshness across all mutation paths
# ===========================================================================

def _seed_reviewed_case(conn, case_id="CASE-MUT-01"):
    case = create_case(conn, case_id, status="needs_attention", appl_number="APPL-TEST")
    create_extracted_field(conn, case["id"], "full_name", "Deepak Chopra", method="text:labeled")
    create_extracted_field(conn, case["id"], "dob", "1960-10-22", method="text:labeled")
    values = {
        "full_name": "Deepak Chopra", "father_name": "", "dob": "1960-10-22",
        "address": "", "licence_no": "", "appl_number": "APPL-TEST",
    }
    case, _, errs = save_review(conn, case_id, values)
    assert not errs
    assert case["status"] == "reviewed"
    assert case["review_version"] == 1
    return case


def test_new_agreeing_evidence_invalidates_review_and_blocks_packet(tmp_cfg):
    """Newly attached agreeing evidence invalidates review, bumps version, and blocks packet."""
    app, conn, token = _setup_app(tmp_cfg)
    client = app.test_client()
    try:
        case = _seed_reviewed_case(conn, "CASE-AGREE-01")

        # Packet is eligible before mutation
        res_before = client.get(
            f"/api/cases/{case['case_id']}/fill-packet",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_before.status_code == 200

        # Attach new document with agreeing evidence
        doc = create_document(conn, orig_filename="agreeing.pdf", orig_path="agreeing.pdf")
        candidates = [
            {"field_key": "full_name", "value": "Deepak Chopra", "confidence": 0.9, "method": "text:labeled"},
            {"field_key": "dob", "value": "1960-10-22", "confidence": 0.9, "method": "text:labeled"},
        ]
        case_after, outcome = attach_or_provisional(conn, doc["id"], candidates)
        assert outcome == "attached"

        # Case must be invalidated
        assert case_after["status"] == "needs_attention"
        assert case_after["reviewed_at"] is None
        assert case_after["review_version"] == 2

        # Packet API must reject with 409
        res_after = client.get(
            f"/api/cases/{case['case_id']}/fill-packet",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_after.status_code == 409
        assert res_after.get_json()["error"] == "case_not_reviewed"

        # Events endpoint must reject stale review_version=1
        ev_stale = client.post(
            f"/api/cases/{case['case_id']}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={"event_type": "copy_attempt", "review_version": 1, "field_key": "full_name"},
        )
        assert ev_stale.status_code == 409
        assert ev_stale.get_json()["error"] == "stale_review_version"

        # Events endpoint with current review_version=2 still rejects because status is needs_attention
        ev_current = client.post(
            f"/api/cases/{case['case_id']}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={"event_type": "copy_attempt", "review_version": 2, "field_key": "full_name"},
        )
        assert ev_current.status_code == 409
        assert ev_current.get_json()["error"] == "case_not_reviewed"
    finally:
        conn.close()


def test_new_conflicting_evidence_invalidates_review_and_blocks_packet(tmp_cfg):
    """Newly attached conflicting evidence invalidates review, bumps version, and blocks packet."""
    app, conn, token = _setup_app(tmp_cfg)
    client = app.test_client()
    try:
        case = _seed_reviewed_case(conn, "CASE-CONF-01")
        # Add an initial licence_no to the case
        create_extracted_field(conn, case["id"], "licence_no", "DL-ORIGINAL-99", method="text:labeled")

        # Attach new document with matching name+dob but conflicting licence_no
        doc = create_document(conn, orig_filename="conflict.pdf", orig_path="conflict.pdf")
        candidates = [
            {"field_key": "full_name", "value": "Deepak Chopra", "confidence": 0.9, "method": "text:labeled"},
            {"field_key": "dob", "value": "1960-10-22", "confidence": 0.9, "method": "text:labeled"},
            {"field_key": "licence_no", "value": "DL-CONFLICT-00", "confidence": 0.9, "method": "text:labeled"},
        ]
        case_after, outcome = attach_or_provisional(conn, doc["id"], candidates)
        assert outcome == "attached_with_conflict"

        # Case must be invalidated
        assert case_after["status"] == "needs_attention"
        assert case_after["reviewed_at"] is None
        assert case_after["review_version"] == 2

        # Packet API must reject with 409
        res = client.get(
            f"/api/cases/{case['case_id']}/fill-packet",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 409
        assert res.get_json()["error"] == "case_not_reviewed"

        # Events endpoint rejects stale version
        ev = client.post(
            f"/api/cases/{case['case_id']}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={"event_type": "copy_attempt", "review_version": 1, "field_key": "full_name"},
        )
        assert ev.status_code == 409
        assert ev.get_json()["error"] == "stale_review_version"
    finally:
        conn.close()


def test_manual_document_assignment_invalidates_reviewed_case(tmp_cfg):
    """Manual document assignment (assign_document) to a reviewed case invalidates review."""
    app, conn, token = _setup_app(tmp_cfg)
    client = app.test_client()
    try:
        case = _seed_reviewed_case(conn, "CASE-ASSIGN-01")

        # Create an unassigned document
        doc = create_document(conn, orig_filename="manual_doc.pdf", orig_path="manual_doc.pdf")

        # Assign document manually to reviewed case
        target_case = assign_document(conn, doc["id"], case["case_id"])
        assert target_case["status"] == "needs_attention"
        assert target_case["reviewed_at"] is None
        assert target_case["review_version"] == 2

        # Fill packet rejected
        res = client.get(
            f"/api/cases/{case['case_id']}/fill-packet",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 409
        assert res.get_json()["error"] == "case_not_reviewed"

        # Events endpoint rejects stale version
        ev = client.post(
            f"/api/cases/{case['case_id']}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={"event_type": "fill_attempt", "review_version": 1},
        )
        assert ev.status_code == 409
        assert ev.get_json()["error"] == "stale_review_version"
    finally:
        conn.close()


def test_merging_into_reviewed_target_case_invalidates_review(tmp_cfg):
    """Merging a case into a reviewed target case invalidates review and bumps version."""
    app, conn, token = _setup_app(tmp_cfg)
    client = app.test_client()
    try:
        target = _seed_reviewed_case(conn, "CASE-TARGET-01")

        # Create a source case with a document
        source = create_case(conn, "CASE-SOURCE-01", status="needs_attention")
        doc = create_document(conn, orig_filename="source_doc.pdf", orig_path="p", case_id=source["id"])
        create_extracted_field(conn, source["id"], "address", "New St", document_id=doc["id"])

        # Merge source into reviewed target
        merged_target = merge_cases(conn, "CASE-SOURCE-01", "CASE-TARGET-01")
        assert merged_target["status"] == "needs_attention"
        assert merged_target["reviewed_at"] is None
        assert merged_target["review_version"] == 2

        # Packet API rejected
        res = client.get(
            f"/api/cases/{target['case_id']}/fill-packet",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 409
        assert res.get_json()["error"] == "case_not_reviewed"

        # Events endpoint rejects stale version
        ev = client.post(
            f"/api/cases/{target['case_id']}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={"event_type": "fill_attempt", "review_version": 1},
        )
        assert ev.status_code == 409
        assert ev.get_json()["error"] == "stale_review_version"
    finally:
        conn.close()


def test_re_review_after_mutation_restores_fillability_with_new_version(tmp_cfg):
    """Re-validating after a mutation restores fillability under the incremented review_version."""
    app, conn, token = _setup_app(tmp_cfg)
    client = app.test_client()
    try:
        case = _seed_reviewed_case(conn, "CASE-REREV-01")

        # Mutate: assign document
        doc = create_document(conn, orig_filename="doc.pdf", orig_path="p")
        assign_document(conn, doc["id"], case["case_id"])
        case_after_mut = get_case(conn, case["case_id"])
        assert case_after_mut["status"] == "needs_attention"
        assert case_after_mut["review_version"] == 2

        # Re-review with Validate & Save
        values = {
            "full_name": "Deepak Chopra", "father_name": "", "dob": "1960-10-22",
            "address": "Verified Address", "licence_no": "", "appl_number": "APPL-TEST",
        }
        re_reviewed, _, errs = save_review(conn, case["case_id"], values)
        assert not errs
        assert re_reviewed["status"] == "reviewed"
        # review_version remains 2 (the current version established by the mutation)
        assert re_reviewed["review_version"] == 2
        assert re_reviewed["reviewed_at"] is not None

        # Packet API succeeds with review_version: 2
        res = client.get(
            f"/api/cases/{case['case_id']}/fill-packet",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        packet = res.get_json()
        assert packet["review_version"] == 2
        fmap = {f["key"]: f for f in packet["fields"]}
        assert fmap["full_name"]["eligible_to_fill"] is True
        assert fmap["address"]["value"] == "Verified Address"
        assert fmap["address"]["eligible_to_fill"] is True

        # Event with old version 1 rejected as stale
        ev_stale = client.post(
            f"/api/cases/{case['case_id']}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={"event_type": "copy_attempt", "review_version": 1, "field_key": "full_name"},
        )
        assert ev_stale.status_code == 409
        assert ev_stale.get_json()["error"] == "stale_review_version"

        # Event with current version 2 accepted
        ev_current = client.post(
            f"/api/cases/{case['case_id']}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={"event_type": "copy_attempt", "review_version": 2, "field_key": "full_name"},
        )
        assert ev_current.status_code == 201
        assert ev_current.get_json()["ok"] is True
    finally:
        conn.close()


# ===========================================================================
# 3. Session secret configuration
# ===========================================================================

def test_session_secret_safe_local_persistence(tmp_path):
    """When no FORMFLOW_SECRET_KEY or override is configured, load_config generates
    a persistent local secret stored in data/.session_secret.
    """
    data_dir = tmp_path / "custom_data"
    cfg1 = load_config({"data_dir": data_dir, "db_path": data_dir / "test.db"})
    secret1 = cfg1.secret_key
    assert len(secret1) == 64  # 32 bytes hex
    assert secret1 != "dev-only-change-me"

    # Secret file was created
    secret_file = data_dir / ".session_secret"
    assert secret_file.exists()
    assert secret_file.read_text(encoding="utf-8").strip() == secret1

    # Reloading config with the same data_dir returns identical secret
    cfg2 = load_config({"data_dir": data_dir, "db_path": data_dir / "test.db"})
    assert cfg2.secret_key == secret1

    # Environment variable override takes precedence
    old_env = os.environ.get("FORMFLOW_SECRET_KEY")
    try:
        os.environ["FORMFLOW_SECRET_KEY"] = "prod-env-secret-12345"
        cfg_env = load_config({"data_dir": data_dir, "db_path": data_dir / "test.db"})
        assert cfg_env.secret_key == "prod-env-secret-12345"
    finally:
        if old_env is not None:
            os.environ["FORMFLOW_SECRET_KEY"] = old_env
        else:
            os.environ.pop("FORMFLOW_SECRET_KEY", None)

    # Explicit override takes precedence over everything
    cfg_override = load_config({
        "data_dir": data_dir,
        "db_path": data_dir / "test.db",
        "secret_key": "explicit-override-key",
    })
    assert cfg_override.secret_key == "explicit-override-key"
