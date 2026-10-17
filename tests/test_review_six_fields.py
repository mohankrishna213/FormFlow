"""Phase 5.1 tests: six-field operator review integration.

Covers ``father_name`` and ``application_no`` (the OCR candidate key) next to
the original four review fields, the application_no -> cases.appl_number
mapping, conflict resolution controls, and safe empty/missing handling.
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
from app.services.fields import extract_candidates, load_field_rules
from app.services.match import attach_or_provisional

CASE = "CASE-20261015-001"


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


# --- rendering and field integration ---

def test_review_form_has_six_fields_and_labels(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    html = review_html(app.test_client())
    for name in ("full_name", "father_name", "dob", "address",
                 "licence_no", "appl_number"):
        assert f'name="{name}"' in html, name
    assert "Full name" in html and "Date of birth" in html
    assert "Father's / related person's name" in html.replace("&#39;", "'")
    assert "Address" in html and "Learner's Licence Number" in html
    assert "Application number" in html
    assert "Son/Wife/Daughter of" in html  # label-family hint, not fabricated data
    assert "checkbox" not in html.lower()  # no per-field checkboxes
    assert 'type="radio"' not in html  # no conflict controls without conflicts


def test_father_name_candidate_shows_source_and_confidence(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        doc = create_document(conn, orig_filename="form-a.png", orig_path="p")
        add_field(conn, case["id"], "father_name", "Ramesh Kumar",
                  document_id=doc["id"], source_page=2)
        add_field(conn, case["id"], "dob", "1999-06-13",
                  method="ocr:labeled-dmy", confidence=0.4)
    finally:
        conn.close()
    html = review_html(app.test_client())
    assert input_value(html, "father_name") == "Ramesh Kumar"
    assert "Source:" in html and "text:labeled" in html
    assert "conf 0.85" in html
    assert "form-a.png" in html and "page 2" in html
    assert "low, verify" in html  # low-confidence dob candidate is noticeable


def test_six_candidates_prefilled_and_appl_licence_kept_separate(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "father_name", "Ramesh Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        add_field(conn, case["id"], "address", "12 MG Road")
        add_field(conn, case["id"], "licence_no", "TG1234567890")
        add_field(conn, case["id"], "application_no", "3542873326",
                  confidence=0.75)
    finally:
        conn.close()
    html = review_html(app.test_client())
    assert input_value(html, "full_name") == "Ravi Kumar"
    assert input_value(html, "father_name") == "Ramesh Kumar"
    assert input_value(html, "dob") == "1995-08-17"
    assert input_value(html, "address") == "12 MG Road"
    lic = input_value(html, "licence_no")
    appl = input_value(html, "appl_number")
    assert lic == "TG1234567890"
    assert appl == "3542873326"
    assert lic != appl and appl not in lic and lic not in appl


def test_saved_appl_number_shown_over_candidate(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn, appl="TG000111222")
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        add_field(conn, case["id"], "application_no", "999888777666")
    finally:
        conn.close()
    client = app.test_client()
    assert input_value(review_html(client), "appl_number") == "TG000111222"
    detail = client.get(f"/cases/{CASE}").data.decode()
    assert "TG000111222" in detail


def test_missing_optional_fields_render_safely(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    html = review_html(app.test_client())
    for name in ("father_name", "address", "licence_no", "appl_number"):
        assert input_value(html, name) == ""
    assert 'value="None"' not in html


def test_case_detail_six_fields_and_missing_markers(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
    finally:
        conn.close()
    html = app.test_client().get(f"/cases/{CASE}").data.decode()
    html_clean = html.replace("&#39;", "'")
    for label in ("Full name", "Date of birth", "Address",
                  "Learner's Licence Number", "Application number"):
        assert label in html_clean, label
    assert "Father's / related person's name" in html_clean
    assert "<strong>full_name</strong>" not in html  # labels, not raw keys
    assert '<span class="warn">missing</span>' in html
    assert ">None<" not in html


# --- validation and persistence ---

def test_father_name_edit_saved_and_shown_on_reopen(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        add_field(conn, case["id"], "father_name", "Ramesh Kum")
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review",
                    data={"csrf_token": token,
                          **minimal_post({"father_name": "Ramesh Kumar"})})
    assert r.status_code == 302
    assert manual_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "value": "Ramesh Kumar"}]
    reopened = review_html(client)
    assert input_value(reopened, "father_name") == "Ramesh Kumar"
    # stale candidate is not prefilled (the manual value is displayed instead)
    assert 'name="father_name" value="Ramesh Kum"' not in reopened


def test_father_name_invalid_rejected_field_specific(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review",
                    data={"csrf_token": token,
                          **minimal_post({"father_name": "Ravi123"})})
    assert r.status_code == 200
    body = r.data.decode()
    assert "Fix these" in body and "related person" in body
    row = case_row(cfg)
    assert row["status"] == "needs_attention" and not row["reviewed_at"]
    assert manual_rows(cfg) == []


def test_father_name_absent_is_allowed(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review",
                    data={"csrf_token": token, **minimal_post()})  # no father_name
    assert r.status_code == 302
    assert case_row(cfg)["status"] == "reviewed"
    assert manual_rows(cfg, field_key="father_name") == []


def test_application_number_update_and_persist(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review",
                    data={"csrf_token": token,
                          **minimal_post({"appl_number": "TG987654321"})})
    assert r.status_code == 302
    assert case_row(cfg)["appl_number"] == "TG987654321"
    assert input_value(review_html(client), "appl_number") == "TG987654321"


def test_invalid_application_number_field_specific(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review",
                    data={"csrf_token": token, **minimal_post({"appl_number": "!!!"})})
    assert r.status_code == 200
    body = r.data.decode()
    assert "Fix these" in body and "Application number" in body
    row = case_row(cfg)
    assert row["status"] == "needs_attention" and row["appl_number"] is None
    assert manual_rows(cfg) == []


def test_appl_and_licence_saved_separately(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token, **minimal_post({
        "licence_no": "TG 1234567890", "appl_number": "3542873326"})})
    assert r.status_code == 302
    assert case_row(cfg)["appl_number"] == "3542873326"
    assert manual_rows(cfg, field_key="licence_no") == [
        {"field_key": "licence_no", "value": "TG 1234567890"}]
    # The application number is never duplicated into manual candidate rows.
    assert manual_rows(cfg, field_key="application_no") == []


def test_empty_ocr_candidate_does_not_erase_saved_appl(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn, appl="TG000111222")
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        # no application_no candidate rows at all (empty OCR result)
    finally:
        conn.close()
    client = app.test_client()
    assert input_value(review_html(client), "appl_number") == "TG000111222"
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review",
                    data={"csrf_token": token,
                          **minimal_post({"appl_number": "TG000111222"})})
    assert r.status_code == 302
    assert case_row(cfg)["appl_number"] == "TG000111222"


def test_no_redundant_manual_rows_on_resave(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    client = app.test_client()
    data = minimal_post({"father_name": "Ramesh Kumar"})
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review",
                       data={"csrf_token": token, **data}).status_code == 302
    token = token_for(client, f"/cases/{CASE}/review")
    assert client.post(f"/cases/{CASE}/review",
                       data={"csrf_token": token, **data}).status_code == 302
    assert len(manual_rows(cfg, field_key="father_name")) == 1


def test_validation_failure_leaves_no_partial_state(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token, **minimal_post({
        "father_name": "Ramesh Kumar", "dob": "not-a-date"})})
    assert r.status_code == 200 and "Fix these" in r.data.decode()
    row = case_row(cfg)
    assert row["status"] == "needs_attention" and not row["reviewed_at"]
    assert manual_rows(cfg) == []  # valid father_name was not saved either


def test_reviewed_case_change_requires_revalidation(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn, status="reviewed")
        add_field(conn, case["id"], "full_name", "Ravi Kumar", method="manual",
                  confidence=None)
        add_field(conn, case["id"], "dob", "1995-08-17", method="manual",
                  confidence=None)
        conn.execute("UPDATE cases SET reviewed_at = 'old', review_version = 1"
                     " WHERE case_id = ?", (CASE,))
        conn.commit()
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                    **minimal_post({"father_name": "Ramesh Kumar"})})
    assert r.status_code == 302
    row = case_row(cfg)
    assert row["status"] == "reviewed" and row["reviewed_at"] != "old"
    assert row["review_version"] == 2


# --- candidate conflicts ---

def seed_father_conflict(conn):
    case = seed_case(conn)
    add_field(conn, case["id"], "full_name", "Ravi Kumar")
    add_field(conn, case["id"], "dob", "1995-08-17")
    add_field(conn, case["id"], "father_name", "Ramesh Kumar",
              method="text:labeled", confidence=0.85)
    add_field(conn, case["id"], "father_name", "Mahesh Kumar",
              method="ocr:labeled", confidence=0.7)
    return case


def test_conflicting_father_name_surfaced_and_blocked_until_edited(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_father_conflict(conn)
    finally:
        conn.close()
    client = app.test_client()
    html = review_html(client)
    assert "'Ramesh Kumar'" in html and "'Mahesh Kumar'" in html
    assert 'name="choice_father_name"' in html  # explicit selection control
    token = token_for(client, f"/cases/{CASE}/review")
    # Unchanged latest candidate blocks.
    blocked = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                          **minimal_post({"father_name": "Mahesh Kumar"})})
    assert blocked.status_code == 200 and "Conflicting values" in blocked.data.decode()
    # Picking the other candidate value resolves it.
    token = token_for(client, f"/cases/{CASE}/review")
    resolved = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                           **minimal_post({"father_name": "Ramesh Kumar"})})
    assert resolved.status_code == 302
    assert manual_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "value": "Ramesh Kumar"}]


def test_conflict_can_confirm_current_best_via_selection(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_father_conflict(conn)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                    **minimal_post({"father_name": "Mahesh Kumar",
                                    "choice_father_name": "Mahesh Kumar"})})
    assert r.status_code == 302
    assert manual_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "value": "Mahesh Kumar"}]
    reopened = review_html(client)
    assert input_value(reopened, "father_name") == "Mahesh Kumar"
    assert 'name="choice_father_name"' not in reopened  # resolution recorded
    # Candidate provenance (both extraction rows and confidence) is retained.
    conn = connect(cfg.db_path)
    try:
        rows = conn.execute(
            """SELECT value, confidence FROM extracted_fields
               WHERE field_key = 'father_name' AND method != 'manual'
               ORDER BY id""").fetchall()
    finally:
        conn.close()
    assert [(r["value"], r["confidence"]) for r in rows] == [
        ("Ramesh Kumar", 0.85), ("Mahesh Kumar", 0.7)]


def test_conflict_pick_survives_validation_error_rerender(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        seed_father_conflict(conn)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    # Pick a candidate but submit an invalid dob: nothing saves, and the
    # re-render keeps the pick (text box and checked radio).
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token, **minimal_post({
        "father_name": "Ramesh Kumar", "choice_father_name": "Ramesh Kumar",
        "dob": "not-a-date"})})
    assert r.status_code == 200 and "Fix these" in r.data.decode()
    body = r.data.decode()
    assert input_value(body, "father_name") == "Ramesh Kumar"
    assert re.search(r'name="choice_father_name" value="Ramesh Kumar"[^>]*checked', body)
    assert case_row(cfg)["status"] == "needs_attention"
    # Fix the dob and resubmit: the retained pick still resolves the conflict.
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token, **minimal_post({
        "father_name": "Ramesh Kumar", "choice_father_name": "Ramesh Kumar"})})
    assert ok.status_code == 302
    assert manual_rows(cfg, field_key="father_name") == [
        {"field_key": "father_name", "value": "Ramesh Kumar"}]


def test_resolved_conflict_can_be_resaved(tmp_cfg):
    """Regression: after an explicit resolution the same value saves again.

    Before the fix, the unchanged-value check compared against the operator's
    own saved value, so every later Validate & Save was blocked again while
    the original extraction rows still disagreed.
    """
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_father_conflict(conn)
        add_field(conn, case["id"], "father_name", "Mahesh Kumar", method="manual",
                  confidence=None)
    finally:
        conn.close()
    client = app.test_client()
    html = review_html(client)
    assert "resolved" in html
    assert 'name="choice_father_name"' not in html  # nothing left to pick
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                    **minimal_post({"father_name": "Mahesh Kumar"})})
    assert r.status_code == 302
    assert case_row(cfg)["status"] == "reviewed"


def test_application_no_conflict_requires_explicit_resolution(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        add_field(conn, case["id"], "application_no", "111111111111",
                  confidence=0.75)
        add_field(conn, case["id"], "application_no", "222222222222",
                  method="ocr:labeled", confidence=0.75)
    finally:
        conn.close()
    client = app.test_client()
    # Draft (latest candidate) unchanged: blocked, nothing persisted.
    token = token_for(client, f"/cases/{CASE}/review")
    blocked = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                          **minimal_post({"appl_number": "222222222222"})})
    assert blocked.status_code == 200 and "Conflicting values" in blocked.data.decode()
    assert case_row(cfg)["appl_number"] is None
    # Entering the other candidate value resolves and persists it officially.
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                     **minimal_post({"appl_number": "111111111111"})})
    assert ok.status_code == 302
    assert case_row(cfg)["appl_number"] == "111111111111"


def test_application_no_conflict_selection_wins_and_resolves(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        add_field(conn, case["id"], "application_no", "111111111111",
                  confidence=0.75)
        add_field(conn, case["id"], "application_no", "222222222222",
                  method="ocr:labeled", confidence=0.75)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    # A radio pick is the explicit selection even when the text box still
    # holds the default draft (no-JavaScript fallback semantics).
    r = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                    **minimal_post({"appl_number": "222222222222",
                                    "choice_application_no": "111111111111"})})
    assert r.status_code == 302
    assert case_row(cfg)["appl_number"] == "111111111111"
    # Once saved, the official value governs: the conflict no longer blocks.
    html = review_html(client)
    assert 'name="choice_application_no"' not in html
    assert input_value(html, "appl_number") == "111111111111"
    token = token_for(client, f"/cases/{CASE}/review")
    again = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                        **minimal_post({"appl_number": "111111111111"})})
    assert again.status_code == 302


def test_address_conflict_behaves_as_before(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
        add_field(conn, case["id"], "address", "12 MG Road, PIN 500001",
                  confidence=0.7)
        add_field(conn, case["id"], "address", "99 Park Street, PIN 400001",
                  method="ocr:labeled-block", confidence=0.7)
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    blocked = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                          **minimal_post({"address": "99 Park Street, PIN 400001"})})
    assert blocked.status_code == 200 and "Conflicting values" in blocked.data.decode()
    token = token_for(client, f"/cases/{CASE}/review")
    ok = client.post(f"/cases/{CASE}/review", data={"csrf_token": token,
                     **minimal_post({"address": "12 MG Road, PIN 500001"})})
    assert ok.status_code == 302
    assert manual_rows(cfg, field_key="address") == [
        {"field_key": "address", "value": "12 MG Road, PIN 500001"}]


def test_missing_values_not_fabricated_on_save(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        case = seed_case(conn)
        add_field(conn, case["id"], "full_name", "Ravi Kumar")
        add_field(conn, case["id"], "dob", "1995-08-17")
    finally:
        conn.close()
    client = app.test_client()
    token = token_for(client, f"/cases/{CASE}/review")
    r = client.post(f"/cases/{CASE}/review",
                    data={"csrf_token": token, **minimal_post()})
    assert r.status_code == 302
    assert manual_rows(cfg) == []
    assert case_row(cfg)["appl_number"] is None


# --- end-to-end: extraction -> storage -> review -> save -> reopen ---

def test_extract_attach_review_save_end_to_end(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        rules = load_field_rules(cfg)
        pages = [{"page": 1, "method": "text", "text": "\n".join([
            "1. Full Name: RAVI KUMAR",
            "3. Father's Name: RAMESH KUMAR",
            "4. Date of Birth: 1995-08-17",
            "Application No: 3542873326",
            "6. Present Address: 12 MG Road, Hyderabad, PIN 500001",
            "7. Driving Licence No: TG1234567890",
        ])}]
        candidates = extract_candidates(pages, rules)
        doc = create_document(conn, orig_filename="form.png", orig_path="p")
        case, outcome = attach_or_provisional(conn, doc["id"], candidates)
        assert outcome == "provisional"  # no existing case to match
    finally:
        conn.close()

    client = app.test_client()
    html = review_html(client, case["case_id"])
    # extract_name_sequence preserves the document's inner case (all-caps in
    # this synthetic page); values are echoed, never rewritten by the UI.
    for value in ("RAVI KUMAR", "RAMESH KUMAR", "1995-08-17",
                  "TG1234567890", "3542873326"):
        assert value in html, value

    token = token_for(client, f"/cases/{case['case_id']}/review")
    r = client.post(f"/cases/{case['case_id']}/review", data={
        "csrf_token": token,
        "full_name": "RAVI KUMAR",
        "father_name": "RAMESH KUMAR",
        "dob": "1995-08-17",
        "address": "12 MG Road, Hyderabad, PIN 500001",
        "licence_no": "TG1234567890",
        "appl_number": "3542873326",
    })
    assert r.status_code == 302

    conn = connect(cfg.db_path)  # restart-equivalent reconnect
    try:
        saved = conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case["case_id"],)).fetchone()
        assert saved["status"] == "reviewed" and saved["reviewed_at"]
        assert saved["appl_number"] == "3542873326"
    finally:
        conn.close()
    assert input_value(review_html(client, case["case_id"]),
                       "appl_number") == "3542873326"
