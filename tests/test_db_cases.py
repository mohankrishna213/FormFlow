"""Phase 1 DB tests: init, customer/case, status, optional appl_number."""
import sqlite3

import pytest

from app.db import (
    ALLOWED_CASE_STATUSES,
    connect,
    create_case,
    create_customer,
    generate_case_id,
    get_case,
    init_db,
    list_cases,
    mark_reviewed,
    touch_case_for_edit,
    update_case_status,
)


def _conn(tmp_cfg):
    init_db(tmp_cfg["db_path"])
    return connect(tmp_cfg["db_path"])


def test_init_creates_all_tables(tmp_cfg):
    init_db(tmp_cfg["db_path"])
    conn = connect(tmp_cfg["db_path"])
    try:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    for t in ("customers", "cases", "documents", "extracted_fields", "intake_jobs", "case_events"):
        assert t in tables


def test_create_customer_and_case(tmp_cfg):
    conn = _conn(tmp_cfg)
    try:
        cust = create_customer(conn, "Synthetic Test")
        cid = generate_case_id(conn, "20261014")
        assert cid == "CASE-20261014-001"
        case = create_case(conn, cid, customer_id=cust["id"])
        assert case["case_id"] == cid
        assert case["appl_number"] is None  # optional until supplied
        assert case["status"] == "queued"
        assert case["reviewed_at"] is None
    finally:
        conn.close()


def test_case_id_unique_and_sequential(tmp_cfg):
    conn = _conn(tmp_cfg)
    try:
        c1 = generate_case_id(conn, "20261014")
        create_case(conn, c1)
        c2 = generate_case_id(conn, "20261014")
        assert c2 == "CASE-20261014-002"
        with pytest.raises(ValueError):
            create_case(conn, c1)  # duplicate
    finally:
        conn.close()


def test_appl_number_optional_and_blank_normalized(tmp_cfg):
    conn = _conn(tmp_cfg)
    try:
        a = create_case(conn, generate_case_id(conn, "20261014"), appl_number="  ")
        assert a["appl_number"] is None
        b = create_case(conn, generate_case_id(conn, "20261014"), appl_number="TG123456789")
        assert b["appl_number"] == "TG123456789"
    finally:
        conn.close()


def test_status_changes_and_review_cycle(tmp_cfg):
    conn = _conn(tmp_cfg)
    try:
        cid = generate_case_id(conn, "20261014")
        create_case(conn, cid)
        row = update_case_status(conn, cid, "ready")
        assert row["status"] == "ready"
        row = mark_reviewed(conn, cid)
        assert row["status"] == "reviewed"
        assert row["reviewed_at"] is not None
        # Edit after review invalidates review + bumps version
        v0 = row["review_version"]
        row = touch_case_for_edit(conn, cid)
        assert row["status"] == "needs_attention"
        assert row["reviewed_at"] is None
        assert row["review_version"] == v0 + 1
        with pytest.raises(ValueError):
            update_case_status(conn, cid, "bogus")
    finally:
        conn.close()


def test_case_survives_reconnect(tmp_cfg):
    init_db(tmp_cfg["db_path"])
    conn = connect(tmp_cfg["db_path"])
    cid = generate_case_id(conn, "20261014")
    create_case(conn, cid)
    conn.close()
    # Simulate app restart: new connection sees the row
    conn2 = connect(tmp_cfg["db_path"])
    try:
        assert get_case(conn2, cid) is not None
        assert len(list_cases(conn2)) == 1
    finally:
        conn2.close()


def test_invalid_case_rejected(tmp_cfg):
    conn = _conn(tmp_cfg)
    try:
        with pytest.raises(ValueError):
            create_case(conn, "   ")
        with pytest.raises(ValueError):
            create_case(conn, "CASE-20261014-009", status="bogus")
        with pytest.raises(sqlite3.IntegrityError):
            # raw NULL violates NOT NULL at SQL level
            conn.execute(
                "INSERT INTO cases (case_id, status, review_version, created_at, updated_at)"
                " VALUES (NULL, 'queued', 1, 'x', 'x')"
            )
    finally:
        conn.close()
    assert ALLOWED_CASE_STATUSES  # contract states are represented
