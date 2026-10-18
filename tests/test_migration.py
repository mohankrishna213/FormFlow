"""Phase 2 Migration & Backward Compatibility Tests.

Verifies:
- Migration from previous schema (without customer_fields) to Phase 2 schema.
- Zero data loss for legacy cases, customers, documents, and extracted fields.
- Idempotent migration execution.
- Operational validity of migrated database under new Phase 2 features.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from app.db import (
    _migrate,
    connect,
    get_customer,
    get_customer_fields,
    init_db,
    list_case_extracted_keys,
    upsert_customer_field,
    utcnow_iso,
)
from app.services.fill_packet import build_fill_packet
from app.services.review import best_and_candidates, save_review

PRE_PHASE2_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL UNIQUE,
    customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    appl_number TEXT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    review_version INTEGER NOT NULL DEFAULT 1,
    reviewed_at TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_customer ON cases(customer_id);

CREATE TABLE IF NOT EXISTS intake_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NULL,
    state TEXT NOT NULL DEFAULT 'waiting',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER REFERENCES cases(id) ON DELETE SET NULL,
    intake_job_id INTEGER REFERENCES intake_jobs(id) ON DELETE SET NULL,
    orig_filename TEXT NOT NULL,
    orig_path TEXT NOT NULL,
    prepared_path TEXT NULL,
    sha256_orig TEXT NULL,
    sha256_prepared TEXT NULL,
    mime TEXT NULL,
    byte_size_orig INTEGER NULL,
    byte_size_prepared INTEGER NULL,
    doctype TEXT NOT NULL DEFAULT 'unknown',
    profile_name TEXT NULL,
    profile_version TEXT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extracted_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    field_key TEXT NOT NULL,
    value TEXT NULL,
    source_page INTEGER NULL,
    method TEXT NULL,
    confidence REAL NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflict_acks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    upto_id INTEGER NOT NULL,
    value_norm TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    detail TEXT NULL,
    created_at TEXT NOT NULL
);
"""


def test_migration_from_pre_phase2_database(tmp_path: Path):
    """Legacy database without customer_fields safely migrates without data loss."""
    db_file = tmp_path / "legacy.db"
    now = utcnow_iso()

    # 1. Create a legacy database using pre-Phase 2 schema
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.executescript(PRE_PHASE2_SCHEMA)

    # 2. Seed legacy customer, case, document, and extracted fields
    cur = conn.execute(
        "INSERT INTO customers (display_name, created_at, updated_at) VALUES (?, ?, ?)",
        ("Anita Sharma", now, now),
    )
    cust_id = cur.lastrowid

    cur = conn.execute(
        """INSERT INTO cases (case_id, customer_id, appl_number, status, review_version, reviewed_at, created_at, updated_at)
           VALUES (?, ?, ?, 'reviewed', 1, ?, ?, ?)""",
        ("CASE-LEGACY-001", cust_id, "3984089026", now, now, now),
    )
    case_id_pk = cur.lastrowid

    cur = conn.execute(
        """INSERT INTO documents (case_id, orig_filename, orig_path, doctype, status, created_at, updated_at)
           VALUES (?, 'dl.pdf', 'data/dl.pdf', 'driving_licence', 'ready', ?, ?)""",
        (case_id_pk, now, now),
    )
    doc_id = cur.lastrowid

    conn.execute(
        """INSERT INTO extracted_fields (case_id, document_id, field_key, value, method, confidence, created_at)
           VALUES (?, ?, 'full_name', 'Anita Sharma', 'manual', NULL, ?)""",
        (case_id_pk, doc_id, now),
    )
    conn.execute(
        """INSERT INTO extracted_fields (case_id, document_id, field_key, value, method, confidence, created_at)
           VALUES (?, ?, 'dob', '1990-05-12', 'manual', NULL, ?)""",
        (case_id_pk, doc_id, now),
    )
    conn.execute(
        """INSERT INTO extracted_fields (case_id, document_id, field_key, value, method, confidence, created_at)
           VALUES (?, ?, 'licence_no', 'DL-0420110012345', 'manual', NULL, ?)""",
        (case_id_pk, doc_id, now),
    )
    conn.commit()

    # Verify customer_fields does NOT exist yet
    tables_before = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "customer_fields" not in tables_before
    conn.close()

    # 3. Perform migration via init_db
    init_db(db_file)

    # 4. Verify customer_fields table exists and has proper columns and index
    migrated_conn = connect(db_file)
    try:
        tables_after = {
            r[0] for r in migrated_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "customer_fields" in tables_after

        cols = {
            r["name"] for r in migrated_conn.execute("PRAGMA table_info(customer_fields)").fetchall()
        }
        expected_cols = {
            "id", "customer_id", "field_key", "value", "source_case_id",
            "source_document_id", "confirmed_at", "created_at", "updated_at",
        }
        assert expected_cols.issubset(cols)

        indices = {
            r["name"] for r in migrated_conn.execute("PRAGMA index_list(customer_fields)").fetchall()
        }
        assert "idx_customer_fields" in indices

        # 5. Verify all legacy data is 100% intact
        cust = get_customer(migrated_conn, cust_id)
        assert cust is not None
        assert cust["display_name"] == "Anita Sharma"

        case = migrated_conn.execute("SELECT * FROM cases WHERE case_id = 'CASE-LEGACY-001'").fetchone()
        assert case is not None
        assert case["appl_number"] == "3984089026"
        assert case["status"] == "reviewed"

        fields = migrated_conn.execute(
            "SELECT field_key, value FROM extracted_fields WHERE case_id = ? ORDER BY field_key",
            (case_id_pk,),
        ).fetchall()
        fdict = {r["field_key"]: r["value"] for r in fields}
        assert fdict["full_name"] == "Anita Sharma"
        assert fdict["dob"] == "1990-05-12"
        assert fdict["licence_no"] == "DL-0420110012345"

        # 6. Verify existing autofill API / fill packet continues to work seamlessly
        packet = build_fill_packet(migrated_conn, "CASE-LEGACY-001", "dl_application")
        assert not hasattr(packet, "http_status")
        fmap = {f["key"]: f for f in packet["fields"]}
        assert fmap["full_name"]["value"] == "Anita Sharma"
        assert fmap["full_name"]["eligible_to_fill"] is True
        assert fmap["dob"]["value"] == "1990-05-12"
        assert fmap["dob"]["eligible_to_fill"] is True
        assert fmap["licence_no"]["value"] == "DL-0420110012345"
        assert fmap["licence_no"]["eligible_to_fill"] is True
        assert fmap["application_no"]["value"] == "3984089026"
        assert fmap["application_no"]["eligible_to_fill"] is True

        # 7. Test migration idempotency (running _migrate multiple times is harmless)
        _migrate(migrated_conn)
        _migrate(migrated_conn)

        # 8. Test new customer_fields operations on migrated database
        upsert_customer_field(
            migrated_conn,
            customer_id=cust_id,
            field_key="mobile_no",
            value="9876543210",
            source_case_id=case_id_pk,
        )
        c_fields = get_customer_fields(migrated_conn, cust_id)
        assert "mobile_no" in c_fields
        assert c_fields["mobile_no"]["value"] == "9876543210"
    finally:
        migrated_conn.close()
