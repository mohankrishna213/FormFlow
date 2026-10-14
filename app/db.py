"""SQLite persistence for FormFlow (Phase 1).

Choice: stdlib sqlite3 + small repository layer (no SQLAlchemy).
Rationale: 5-15 applications/day on one PC needs no ORM; fewer
dependencies, explicit schema, WAL + parameterized queries, easy to
audit. If query complexity grows in later phases we can revisit with
owner approval.

SQLite is the source of truth; case folders on disk are convenience.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_CASE_STATUSES = {
    "queued",
    "processing",
    "needs_attention",
    "ready",
    "reviewed",
    "failed",
}

ALLOWED_DOC_STATUSES = {
    "queued",
    "processing",
    "needs_attention",
    "ready",
    "reviewed",
    "failed",
}

ALLOWED_JOB_STATES = {
    "waiting",
    "queued",
    "processing",
    "needs_attention",
    "ready",
    "failed",
}

SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_jobs_sha ON intake_jobs(sha256);

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
CREATE INDEX IF NOT EXISTS idx_docs_case ON documents(case_id);
CREATE INDEX IF NOT EXISTS idx_docs_sha ON documents(sha256_orig);

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
CREATE INDEX IF NOT EXISTS idx_fields_case ON extracted_fields(case_id);

CREATE TABLE IF NOT EXISTS case_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    detail TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_case ON case_events(case_id);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Path | str) -> Path | str:
    if str(db_path) != ":memory:":
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(p)
    else:
        conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return db_path


# --- repositories (parameterized, no string interpolation) ---


def create_customer(conn: sqlite3.Connection, display_name: str) -> sqlite3.Row:
    if not display_name or not display_name.strip():
        raise ValueError("display_name is required")
    now = utcnow_iso()
    cur = conn.execute(
        "INSERT INTO customers (display_name, created_at, updated_at) VALUES (?, ?, ?)",
        (display_name.strip(), now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM customers WHERE id = ?", (cur.lastrowid,)).fetchone()


def generate_case_id(conn: sqlite3.Connection, date_prefix: str | None = None) -> str:
    """Internal ID like CASE-20261014-001. Never an official application number."""
    day = date_prefix or datetime.now().strftime("%Y%m%d")
    prefix = f"CASE-{day}-"
    row = conn.execute(
        "SELECT case_id FROM cases WHERE case_id LIKE ? ORDER BY case_id DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    nxt = 1
    if row:
        try:
            nxt = int(row["case_id"].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            nxt = 1
    return f"{prefix}{nxt:03d}"


def create_case(
    conn: sqlite3.Connection,
    case_id: str,
    customer_id: int | None = None,
    appl_number: str | None = None,
    status: str = "queued",
) -> sqlite3.Row:
    if not case_id or not case_id.strip():
        raise ValueError("case_id is required")
    if status not in ALLOWED_CASE_STATUSES:
        raise ValueError(f"invalid status: {status}")
    appl = appl_number.strip() if appl_number and appl_number.strip() else None
    now = utcnow_iso()
    try:
        cur = conn.execute(
            """INSERT INTO cases
               (case_id, customer_id, appl_number, status, review_version, reviewed_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, NULL, ?, ?)""",
            (case_id.strip(), customer_id, appl, status, now, now),
        )
    except sqlite3.IntegrityError as e:
        raise ValueError(f"case_id already exists: {case_id}") from e
    conn.commit()
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.execute(
        "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
        (row["id"], "created", f"case {case_id} created", utcnow_iso()),
    )
    conn.commit()
    return row


def get_case(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()


def list_cases(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cases ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


def update_case_status(conn: sqlite3.Connection, case_id: str, new_status: str) -> sqlite3.Row:
    if new_status not in ALLOWED_CASE_STATUSES:
        raise ValueError(f"invalid status: {new_status}")
    row = get_case(conn, case_id)
    if row is None:
        raise ValueError(f"unknown case: {case_id}")
    now = utcnow_iso()
    # Editing flow: leaving 'reviewed' bumps review_version elsewhere;
    # here any status change clears reviewed_at unless target is reviewed.
    reviewed_at = row["reviewed_at"] if new_status == "reviewed" else None
    if new_status == "reviewed" and reviewed_at is None:
        reviewed_at = now
    conn.execute(
        "UPDATE cases SET status = ?, reviewed_at = ?, updated_at = ? WHERE case_id = ?",
        (new_status, reviewed_at, now, case_id),
    )
    conn.execute(
        "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
        (row["id"], "status", f"{row['status']} -> {new_status}", now),
    )
    conn.commit()
    return get_case(conn, case_id)


def mark_reviewed(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    row = get_case(conn, case_id)
    if row is None:
        raise ValueError(f"unknown case: {case_id}")
    now = utcnow_iso()
    conn.execute(
        "UPDATE cases SET status = 'reviewed', reviewed_at = ?, updated_at = ? WHERE case_id = ?",
        (now, now, case_id),
    )
    conn.execute(
        "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
        (row["id"], "reviewed", "case validated by operator", now),
    )
    conn.commit()
    return get_case(conn, case_id)


def touch_case_for_edit(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    """Call when operator edits a reviewed case: back to needs_attention + bump version."""
    row = get_case(conn, case_id)
    if row is None:
        raise ValueError(f"unknown case: {case_id}")
    now = utcnow_iso()
    if row["status"] == "reviewed":
        conn.execute(
            """UPDATE cases SET status = 'needs_attention', reviewed_at = NULL,
               review_version = review_version + 1, updated_at = ? WHERE case_id = ?""",
            (now, case_id),
        )
        conn.execute(
            "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (row["id"], "edit_after_review", "edit invalidated review; re-review required", now),
        )
        conn.commit()
        return get_case(conn, case_id)
    return row
