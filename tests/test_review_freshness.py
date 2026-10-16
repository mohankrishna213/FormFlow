"""Phase 5.1.1 tests: conflict-resolution freshness.

A resolved conflict must not re-block while the evidence set is unchanged,
but a contradictory candidate added after the resolution (attach/merge)
must re-require review -- including for the application number, whose
confirmed value lives in ``cases.appl_number``. Anchors are recorded in
``conflict_acks``; pre-existing manual values fall back to their row id.
Synthetic fixtures only: no real documents, raw OCR text or personal data.
"""
import re

from app import create_app
from app.db import (
    connect,
    create_case,
    create_document,
    create_extracted_field,
)
from app.services.review import KEEP_BLANK

CASE = "CASE-20261016-001"


def seed_case(conn, case_id=CASE, status="needs_attention", appl=None):
    return create_case(conn, case_id, appl_number=appl, status=status)


def add_field(conn, case_pk, key, value, method="text:labeled",
              confidence=0.85, document_id=None, source_page=None):
    return create_extracted_field(
        conn, case_pk, key, value, document_id=document_id,
        source_page=source_page, method=method, confidence=confidence)


def token_for(client, url):
    html = client.get(url).data.decode()
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no CSRF token on {url}"
    return m.group(1)


def review_html(client, case_id=CASE):
    r = client.get(f"/cases/{case_id}/review")
    assert r.status_code == 200
    return r.data.decode()


def input_value(html, name):
    m = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert m, f'input name="{name}" not found'
    return m.group(1)


def minimal_post(values=None):
    data = {"full_name": "Ravi Kumar", "dob": "1995-08-17",
            "address": "", "licence_no": "", "appl_number": ""}
    data.update(values or {})
    return data


def case_row(cfg, case_id=CASE):
    conn = connect(cfg.db_path)
    try:
        return conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    finally:
        conn.close()


def set_status(cfg, status, case_id=CASE):
    """Mirror the attach/merge flows, which flag a case after new evidence."""
    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "UPDATE cases SET status = ?, reviewed_at = NULL WHERE case_id = ?",
            (status, case_id))
        conn.commit()
    finally:
        conn.close()


def manual_rows(cfg, case_id=CASE, field_key=None):
    conn = connect(cfg.db_path)
    try:
        q = ("SELECT f.field_key, f.value FROM extracted_fields f"
             " JOIN cases c ON c.id = f.case_id"
             " WHERE c.case_id = ? AND f.method = 'manual'")
        params = [case_id]
        if field_key:
            q += " AND f.field_key = ?"
            params.append(field_key)
        return [dict(r) for r in conn.execute(q, params).fetchall()]
    finally:
        conn.close()


def ack_rows(cfg, case_id=CASE, field_key=None):
    conn = connect(cfg.db_path)
    try:
        q = ("SELECT a.field_key, a.upto_id, a.value_norm FROM conflict_acks a"
             " JOIN cases c ON c.id = a.case_id WHERE c.case_id = ? ORDER BY a.id")
        params = [case_id]
        if field_key:
            q += " AND a.field_key = ?"
            params.append(field_key)
        return [dict(r) for r in conn.execute(q, params).fetchall()]
    finally:
        conn.close()


def ack_count_for_pk(cfg, case_pk):
    conn = connect(cfg.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) n FROM conflict_acks WHERE case_id = ?",
            (case_pk,)).fetchone()["n"]
    finally:
        conn.close()


def seed_father_conflict(conn):
    """Ramesh (older) vs Mahesh (newer, current best/prefill)."""
    case = seed_case(conn)
    add_field(conn, case["id"], "full_name", "Ravi Kumar")
    add_field(conn, case["id"], "dob", "1995-08-17")
    r1 = add_field(conn, case["id"], "father_name", "Ramesh Kumar",
                   method="text:labeled", confidence=0.85)
    r2 = add_field(conn, case["id"], "father_name", "Mahesh Kumar",
                   method="ocr:labeled", confidence=0.7)
    return case, r1, r2


# --- 1/2: resolve, then resave without new evidence ---

def test_conflict_resolved_and_saved(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        _, r1, r2 = seed_father_conflict(conn)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                    **minimal_post({"father_name": "Ramesh Kumar"})})
    assert r.status_code == 302
    assert manual_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "value": "Ramesh Kumar"}]
    assert ack_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "upto_id": r2["id"],
         "value_norm": "ramesh kumar"}]
    assert case_row(cfg)["status"] == "reviewed"


def test_resave_without_new_evidence_does_not_block(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_father_conflict(conn)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"father_name": "Ramesh Kumar"})}).status_code == 302
    for _ in range(2):
        token = token_for(client, f"/cases/{CASE}/review")
        r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                        **minimal_post({"father_name": "Ramesh Kumar"})})
        assert r.status_code == 302  # not blocked again
    assert len(manual_rows(cfg, field_key="father_name")) == 1  # no row spam
    assert len(ack_rows(cfg, field_key="father_name")) == 1    # anchor unchanged
    assert case_row(cfg)["status"] == "reviewed"


# --- 3: new contradictory evidence re-requires review ---

def test_new_contradicting_candidate_reopens_conflict(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case, r1, r2 = seed_father_conflict(conn)
    finally:
        conn.close()
    client = app.test_client()
    # Resolve to a correction that is not among the candidates.
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"father_name": "Suresh Kumar"})}).status_code == 302
    assert ack_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "upto_id": r2["id"],
         "value_norm": "suresh kumar"}]
    # A contradictory candidate arrives later (as attach/merge would add).
    conn = connect(cfg.db_path)
    try:
        r3 = add_field(conn, case["id"], "father_name", "Dinesh Kumar",
                       method="ocr:labeled", confidence=0.6)
    finally:
        conn.close()
    set_status(cfg, "needs_attention")
    html = review_html(client)
    assert "new evidence since your saved value" in html
    assert 'name="choice_father_name" value="Suresh Kumar"' in html  # keep option
    assert input_value(html, "father_name") == "Suresh Kumar"
    detail = client.get(f"/cases/{CASE}").data.decode()
    assert "related person&#39;s name disagrees:</span>" in detail  # label, not key
    assert ">father_name disagrees<" not in detail
    # Untouched resubmit is blocked again; nothing is silently acknowledged.
    token = token_for(client, f"/cases/{CASE}/review")
    blocked = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                          **minimal_post({"father_name": "Suresh Kumar"})})
    assert blocked.status_code == 200 and "Conflicting values" in blocked.data.decode()
    assert ack_rows(cfg, field_key="father_name")[0]["upto_id"] == r2["id"]
    # Keep the saved value explicitly: that re-acknowledges the new evidence.
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                     **minimal_post({"father_name": "Suresh Kumar",
                                     "choice_father_name": "Suresh Kumar"})})
    assert ok.status_code == 302
    assert ack_rows(cfg, field_key="father_name")[-1]["upto_id"] == r3["id"]
    assert manual_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "value": "Suresh Kumar"},
        {"field_key": "father_name", "value": "Suresh Kumar"}]
    token = token_for(client, f"/cases/{CASE}/review")
    again = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                        **minimal_post({"father_name": "Suresh Kumar"})})
    assert again.status_code == 302  # settled: no repeat blocking


# --- 4: the same freshness rule for application_no ---

def test_application_no_freshness_preserves_confirmed_value(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        c1 = add_field(conn, case["id"], "application_no", "111111111111",
                       confidence=0.75)
        c2 = add_field(conn, case["id"], "application_no", "222222222222",
                       method="ocr:labeled", confidence=0.75)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                     **minimal_post({"appl_number": "222222222222",
                                     "choice_application_no": "111111111111"})})
    assert ok.status_code == 302
    assert case_row(cfg)["appl_number"] == "111111111111"
    assert ack_rows(cfg, field_key="application_no") == [
        {"field_key": "application_no", "upto_id": c2["id"],
         "value_norm": "111111111111"}]
    # A third, contradictory candidate arrives later.
    conn = connect(cfg.db_path)
    try:
        c3 = add_field(conn, case["id"], "application_no", "333333333333",
                       method="ocr:labeled", confidence=0.75)
    finally:
        conn.close()
    set_status(cfg, "needs_attention")
    html = review_html(client)
    # The form still shows the confirmed official value, not the newest OCR row.
    assert input_value(html, "appl_number") == "111111111111"
    assert 'name="choice_application_no"' in html  # renewed review required
    # Untouched resubmit is blocked; the confirmed value is untouched in the DB.
    token = token_for(client, f"/cases/{CASE}/review")
    blocked = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                          **minimal_post({"appl_number": "111111111111"})})
    assert blocked.status_code == 200 and "Conflicting values" in blocked.data.decode()
    assert case_row(cfg)["appl_number"] == "111111111111"
    # Re-confirm the saved value via its (still listed) candidate radio.
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                     **minimal_post({"appl_number": "111111111111",
                                     "choice_application_no": "111111111111"})})
    assert ok.status_code == 302
    assert case_row(cfg)["appl_number"] == "111111111111"
    assert ack_rows(cfg, field_key="application_no")[-1]["upto_id"] == c3["id"]
    assert manual_rows(cfg, field_key="application_no") == []  # still not duplicated
    token = token_for(client, f"/cases/{CASE}/review")
    again = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                        **minimal_post({"appl_number": "111111111111"})})
    assert again.status_code == 302


# --- 5: agreeing new evidence is not a conflict ---

def test_agreeing_new_evidence_does_not_reblock(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case, r1, r2 = seed_father_conflict(conn)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"father_name": "Ramesh Kumar"})}).status_code == 302
    # A second document agrees with the settled value.
    conn = connect(cfg.db_path)
    try:
        doc = create_document(conn, orig_filename="second.png", orig_path="p")
        add_field(conn, case["id"], "father_name", "Ramesh Kumar",
                  method="ocr:labeled", confidence=0.8, document_id=doc["id"])
        rows = conn.execute(
            """SELECT COUNT(*) n FROM extracted_fields
               WHERE case_id = ? AND field_key = 'father_name'
               AND (method IS NULL OR method != 'manual')""",
            (case["id"],)).fetchone()["n"]
    finally:
        conn.close()
    assert rows == 3  # evidence retained, not deleted
    html = review_html(client)
    assert "resolved" in html and "new evidence" not in html
    assert 'name="choice_father_name"' not in html  # no renewed review needed
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                    **minimal_post({"father_name": "Ramesh Kumar"})})
    assert r.status_code == 302
    assert case_row(cfg)["status"] == "reviewed"


# --- 6: assignment and merge keep marking review state ---

def test_assignment_marks_review_state_and_keeps_anchors(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_father_conflict(conn)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"father_name": "Ramesh Kumar"})}).status_code == 302
    conn = connect(cfg.db_path)
    try:
        doc = create_document(conn, orig_filename="extra.png", orig_path="p")
    finally:
        conn.close()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/assign-document", data={
        "csrf_token": token, "document_id": str(doc["id"])})
    assert r.status_code == 302
    row = case_row(cfg)
    assert row["status"] == "needs_attention" and row["review_version"] == 2
    assert row["reviewed_at"] is None
    # The anchor survives; the settled value is still not re-blocked.
    assert "resolved" in review_html(client)


def test_merge_reopens_target_and_drops_source_anchors(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case, r1, r2 = seed_father_conflict(conn)
        src = seed_case(conn, case_id="CASE-20261016-002")
        add_field(conn, src["id"], "full_name", "Ravi Kumar")
        add_field(conn, src["id"], "father_name", "Dinesh Kumar",
                  method="ocr:labeled", confidence=0.6)
        # The source had its own (now irrelevant) acknowledgement on record.
        conn.execute(
            """INSERT INTO conflict_acks (case_id, field_key, upto_id, value_norm, created_at)
               VALUES (?, 'father_name', 0, 'dinesh kumar', '2026-10-16T00:00:00+00:00')""",
            (src["id"],))
        conn.commit()
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"father_name": "Ramesh Kumar"})}).status_code == 302
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/merge", data={
        "csrf_token": token, "source": "CASE-20261016-002"})
    assert r.status_code == 302
    row = case_row(cfg)
    assert row["status"] == "needs_attention" and row["review_version"] == 2
    assert case_row(cfg, "CASE-20261016-002") is None  # source merged away
    assert ack_count_for_pk(cfg, src["id"]) == 0       # source anchors dropped
    assert len(ack_rows(cfg, field_key="father_name")) == 1  # target anchor intact
    # The merged contradictory row re-requires review of the target.
    html = review_html(client)
    assert "new evidence since your saved value" in html
    token = token_for(client, f"/cases/{CASE}/review")
    blocked = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                          **minimal_post({"father_name": "Ramesh Kumar"})})
    assert blocked.status_code == 200 and "Conflicting values" in blocked.data.decode()
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                     **minimal_post({"father_name": "Ramesh Kumar",
                                     "choice_father_name": "Ramesh Kumar"})})
    assert ok.status_code == 302
    token = token_for(client, f"/cases/{CASE}/review")
    again = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                        **minimal_post({"father_name": "Ramesh Kumar"})})
    assert again.status_code == 302


# --- 7: validation failures stay all-or-nothing ---

def test_validation_error_saves_nothing_and_not_reviewed(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case, r1, r2 = seed_father_conflict(conn)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"father_name": "Ramesh Kumar"})}).status_code == 302
    conn = connect(cfg.db_path)
    try:
        add_field(conn, case["id"], "father_name", "Dinesh Kumar",
                  method="ocr:labeled", confidence=0.6)
    finally:
        conn.close()
    set_status(cfg, "needs_attention")
    # Resolving the fresh conflict while dob is invalid: nothing may persist.
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                    **minimal_post({"father_name": "Suresh Kumar", "dob": "not-a-date"})})
    assert r.status_code == 200 and "Fix these" in r.data.decode()
    row = case_row(cfg)
    assert row["status"] == "needs_attention" and row["reviewed_at"] is None
    assert manual_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "value": "Ramesh Kumar"}]  # only the first resolve
    assert ack_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "upto_id": r2["id"],
         "value_norm": "ramesh kumar"}]  # anchor not advanced by the failure
    # With a valid dob the same edit saves and acknowledges the new evidence.
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                     **minimal_post({"father_name": "Suresh Kumar"})})
    assert ok.status_code == 302
    assert case_row(cfg)["status"] == "reviewed"


# --- 8: original OCR evidence is never removed ---

def test_original_evidence_retained_after_resolution(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        doc = create_document(conn, orig_filename="form-a.png", orig_path="p")
        add_field(conn, case["id"], "father_name", "Ramesh Kumar",
                  method="ocr:labeled", confidence=0.7, document_id=doc["id"],
                  source_page=2)
        add_field(conn, case["id"], "father_name", "Mahesh Kumar",
                  method="text:labeled", confidence=0.85)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"father_name": "Ramesh Kumar"})}).status_code == 302
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"father_name": "Ramesh Kumar"})}).status_code == 302
    conn = connect(cfg.db_path)
    try:
        rows = conn.execute(
            """SELECT value, method, confidence, document_id, source_page
               FROM extracted_fields WHERE case_id = ? AND field_key = 'father_name'
               ORDER BY id""",
            (case["id"],)).fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [
        ("Ramesh Kumar", "ocr:labeled", 0.7, doc["id"], 2),
        ("Mahesh Kumar", "text:labeled", 0.85, None, None),
        ("Ramesh Kumar", "manual", None, None, None),
    ]


# --- blank-value resolution (keep-blank control) ---

def test_keep_blank_resolution_has_no_deadlock(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        add_field(conn, case["id"], "address", "12 MG Road, PIN 500001",
                  confidence=0.7)
        a2 = add_field(conn, case["id"], "address", "99 Park Street, PIN 400001",
                       method="ocr:labeled-block", confidence=0.7)
    finally:
        conn.close()
    client = app.test_client()
    # Deliberately clearing the conflicted optional field resolves it.
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"address": ""})}).status_code == 302
    assert manual_rows(cfg, field_key="address") == [
        {"field_key": "address", "value": None}]
    assert ack_rows(cfg, field_key="address") == [
        {"field_key": "address", "upto_id": a2["id"], "value_norm": ""}]
    # New evidence arrives: the blank decision must be re-confirmable.
    conn = connect(cfg.db_path)
    try:
        a3 = add_field(conn, case["id"], "address", "3 New Street, PIN 600001",
                       method="ocr:labeled-block", confidence=0.7)
    finally:
        conn.close()
    set_status(cfg, "needs_attention")
    html = review_html(client)
    assert "Keep it blank" in html and f'value="{KEEP_BLANK}"' in html
    token = token_for(client, f"/cases/{CASE}/review")
    blocked = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                          **minimal_post()})
    assert blocked.status_code == 200 and "Conflicting values" in blocked.data.decode()
    # The blank decision survives a failed validation re-render: the radio
    # stays checked and the text box is not filled with the sentinel.
    token = token_for(client, f"/cases/{CASE}/review")
    failed = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                          **minimal_post({"choice_address": KEEP_BLANK,
                                          "dob": "not-a-date"})})
    assert failed.status_code == 200 and "Fix these" in failed.data.decode()
    body = failed.data.decode()
    assert re.search(rf'name="choice_address" value="{KEEP_BLANK}"[^>]*checked', body)
    assert input_value(body, "address") == ""
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                     **minimal_post({"choice_address": KEEP_BLANK})})
    assert ok.status_code == 302
    assert ack_rows(cfg, field_key="address")[-1]["upto_id"] == a3["id"]
    token = token_for(client, f"/cases/{CASE}/review")
    again = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                        **minimal_post()})
    assert again.status_code == 302


# --- upgrade path: resolutions recorded before conflict_acks existed ---

def test_legacy_manual_resolution_then_new_evidence_reopens(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case, r1, r2 = seed_father_conflict(conn)
        # Phase 5.1 style resolution: a manual row, no anchor row.
        add_field(conn, case["id"], "father_name", "Ramesh Kumar",
                  method="manual", confidence=None)
    finally:
        conn.close()
    client = app.test_client()
    html = review_html(client)
    assert "resolved" in html and 'name="choice_father_name"' not in html
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                       **minimal_post({"father_name": "Ramesh Kumar"})}).status_code == 302
    # Evidence newer than the manual decision contradicts it -> renewed review.
    conn = connect(cfg.db_path)
    try:
        add_field(conn, case["id"], "father_name", "Dinesh Kumar",
                  method="ocr:labeled", confidence=0.6)
    finally:
        conn.close()
    set_status(cfg, "needs_attention")
    assert "new evidence since your saved value" in review_html(client)
    token = token_for(client, f"/cases/{CASE}/review")
    blocked = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                          **minimal_post({"father_name": "Ramesh Kumar"})})
    assert blocked.status_code == 200 and "Conflicting values" in blocked.data.decode()
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                     **minimal_post({"father_name": "Dinesh Kumar"})})
    assert ok.status_code == 302
    assert ack_rows(cfg, field_key="father_name")[-1]["value_norm"] == "dinesh kumar"
