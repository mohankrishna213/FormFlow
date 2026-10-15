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
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight migrations for Phase 1 -> Phase 2 (no data loss)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(intake_jobs)").fetchall()}
    if "staged_path" not in cols:
        conn.execute("ALTER TABLE intake_jobs ADD COLUMN staged_path TEXT NULL")
    if "file_size" not in cols:
        conn.execute("ALTER TABLE intake_jobs ADD COLUMN file_size INTEGER NULL")
    doc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    if "ocr_status" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN ocr_status TEXT NULL DEFAULT 'pending'")
    if "ocr_error" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN ocr_error TEXT NULL")


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


# --- intake job repositories (Phase 2) ---


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM intake_jobs WHERE id = ?", (job_id,)).fetchone()


def get_job_by_path(conn: sqlite3.Connection, inbox_path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM intake_jobs WHERE inbox_path = ?", (inbox_path,)
    ).fetchone()


def find_job_by_hash(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM intake_jobs WHERE sha256 = ? ORDER BY id LIMIT 1", (sha256,)
    ).fetchone()


def list_jobs(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM intake_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


def job_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM intake_jobs GROUP BY state"
    ).fetchall()
    return {r["state"]: r["n"] for r in rows}


def create_job(
    conn: sqlite3.Connection,
    inbox_path: str,
    state: str = "waiting",
    sha256: str | None = None,
    last_error: str | None = None,
) -> sqlite3.Row:
    if state not in ALLOWED_JOB_STATES:
        raise ValueError(f"invalid job state: {state}")
    now = utcnow_iso()
    try:
        cur = conn.execute(
            """INSERT INTO intake_jobs
               (inbox_path, sha256, state, attempts, last_error, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?, ?)""",
            (inbox_path, sha256, state, last_error, now, now),
        )
    except sqlite3.IntegrityError:
        # Idempotency: same inbox path seen twice -> return existing row.
        conn.rollback()
        existing = get_job_by_path(conn, inbox_path)
        if existing is None:
            raise
        return existing
    conn.commit()
    return get_job(conn, cur.lastrowid)


def set_job_state(
    conn: sqlite3.Connection,
    job_id: int,
    state: str,
    last_error: str | None = None,
    sha256: str | None = None,
    staged_path: str | None = None,
    file_size: int | None = None,
    bump_attempts: bool = False,
) -> sqlite3.Row:
    if state not in ALLOWED_JOB_STATES:
        raise ValueError(f"invalid job state: {state}")
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"unknown job: {job_id}")
    now = utcnow_iso()
    new_sha = sha256 if sha256 is not None else job["sha256"]
    # sqlite3.Row has no .get(); guard migrated columns by key list.
    keys = job.keys()
    new_staged = staged_path if staged_path is not None else (job["staged_path"] if "staged_path" in keys else None)
    new_size = file_size if file_size is not None else (job["file_size"] if "file_size" in keys else None)
    attempts = job["attempts"] + (1 if bump_attempts else 0)
    conn.execute(
        """UPDATE intake_jobs SET state = ?, last_error = ?, sha256 = ?,
           staged_path = ?, file_size = ?, attempts = ?, updated_at = ?
           WHERE id = ?""",
        (state, last_error, new_sha, new_staged, new_size, attempts, now, job_id),
    )
    conn.commit()
    return get_job(conn, job_id)


def retry_job(conn: sqlite3.Connection, job_id: int, max_attempts: int = 5) -> sqlite3.Row:
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"unknown job: {job_id}")
    if job["state"] not in ("failed", "needs_attention"):
        raise ValueError(f"job {job_id} is {job['state']}; only failed/needs_attention can retry")
    if job["attempts"] >= max_attempts:
        raise ValueError(f"job {job_id} exceeded max attempts ({max_attempts})")
    return set_job_state(
        conn, job_id, "queued", last_error=None, bump_attempts=True
    )


def recover_interrupted(conn: sqlite3.Connection) -> int:
    """Restart recovery: processing/waiting jobs go back to queued safely."""
    now = utcnow_iso()
    cur = conn.execute(
        """UPDATE intake_jobs SET state = 'queued',
           last_error = 'interrupted; requeued on restart', updated_at = ?
           WHERE state IN ('processing', 'waiting')""",
        (now,),
    )
    conn.commit()
    return cur.rowcount


# --- document repositories (Phase 3) ---

_ALLOWED_DOC_COLUMNS = {
    "case_id", "intake_job_id", "orig_filename", "orig_path",
    "prepared_path", "sha256_orig", "sha256_prepared", "mime",
    "byte_size_orig", "byte_size_prepared", "doctype", "profile_name",
    "profile_version", "status", "error", "ocr_status", "ocr_error",
}


def create_document(
    conn: sqlite3.Connection,
    orig_filename: str,
    orig_path: str,
    doctype: str = "unknown",
    status: str = "queued",
    **extra,
) -> sqlite3.Row:
    if status not in ALLOWED_DOC_STATUSES:
        raise ValueError(f"invalid document status: {status}")
    if not orig_filename or not orig_path:
        raise ValueError("orig_filename and orig_path are required")
    now = utcnow_iso()
    cols = ["orig_filename", "orig_path", "doctype", "status", "created_at", "updated_at"]
    vals: list = [orig_filename, orig_path, doctype, status, now, now]
    for k, v in extra.items():
        if k in _ALLOWED_DOC_COLUMNS and k not in cols:
            cols.append(k)
            vals.append(v)
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO documents ({', '.join(cols)}) VALUES ({placeholders})", vals
    )
    conn.commit()
    return conn.execute("SELECT * FROM documents WHERE id = ?", (cur.lastrowid,)).fetchone()


def get_document(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


def latest_document_for_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE intake_job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()


def list_documents(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


def update_document(conn: sqlite3.Connection, doc_id: int, **fields) -> sqlite3.Row:
    if get_document(conn, doc_id) is None:
        raise ValueError(f"unknown document: {doc_id}")
    sets, vals = [], []
    for k, v in fields.items():
        if k in _ALLOWED_DOC_COLUMNS:
            sets.append(f"{k} = ?")
            vals.append(v)
    if "status" in fields and fields["status"] not in ALLOWED_DOC_STATUSES:
        raise ValueError(f"invalid document status: {fields['status']}")
    if not sets:
        return get_document(conn, doc_id)
    vals.append(utcnow_iso())
    vals.append(doc_id)
    conn.execute(
        f"UPDATE documents SET {', '.join(sets)}, updated_at = ? WHERE id = ?", vals
    )
    conn.commit()
    return get_document(conn, doc_id)


# --- extracted field repositories (Phase 4) ---
# Rows are drafts: no verified/validated columns exist by design.


def create_extracted_field(
    conn: sqlite3.Connection,
    case_id: int,
    field_key: str,
    value: str | None,
    document_id: int | None = None,
    source_page: int | None = None,
    method: str | None = None,
    confidence: float | None = None,
) -> sqlite3.Row:
    if not field_key or not field_key.strip():
        raise ValueError("field_key is required")
    cur = conn.execute(
        """INSERT INTO extracted_fields
           (case_id, document_id, field_key, value, source_page, method, confidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (case_id, document_id, field_key.strip(), value, source_page,
         method, confidence, utcnow_iso()),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM extracted_fields WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def list_case_fields(conn: sqlite3.Connection, case_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM extracted_fields WHERE case_id = ? ORDER BY id", (case_pk,)
    ).fetchall()


def list_document_fields(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM extracted_fields WHERE document_id = ? ORDER BY id", (doc_id,)
    ).fetchall()


def latest_field_value(
    conn: sqlite3.Connection, case_pk: int, field_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM extracted_fields
           WHERE case_id = ? AND field_key = ? ORDER BY id DESC LIMIT 1""",
        (case_pk, field_key),
    ).fetchone()
