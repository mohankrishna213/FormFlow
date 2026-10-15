"""Operator review, assignment and merge logic (Phase 5).

Single case-level Validate & Save; no per-field checkboxes anywhere.
Operator edits are stored as extracted_fields rows with method='manual'
(document NULL) so every value stays traceable. Displayed value per key =
latest manual row if any, else latest extracted row.
"""
from __future__ import annotations

import re
import sqlite3

from ..db import utcnow_iso
from .fields import normalize_name, parse_date_token, parse_name

REVIEW_FIELDS = ("full_name", "dob", "address", "licence_no")
REQUIRED_FIELDS = ("full_name", "dob")
APPL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/]{1,28}[A-Za-z0-9]$")
PER_PAGE = 20


def _norm(key: str, value: str) -> str:
    v = (value or "").strip()
    if key == "full_name":
        return normalize_name(v)
    return v.lower()


def best_and_candidates(
    conn: sqlite3.Connection, case_pk: int
) -> dict[str, dict]:
    """Per review key: best value, its provenance, and non-manual candidates."""
    out: dict[str, dict] = {}
    for key in REVIEW_FIELDS:
        rows = conn.execute(
            """SELECT * FROM extracted_fields WHERE case_id = ? AND field_key = ?
               ORDER BY id""",
            (case_pk, key),
        ).fetchall()
        manual = [r for r in rows if (r["method"] or "") == "manual"]
        cands = [r for r in rows if (r["method"] or "") != "manual"]
        best = dict(manual[-1]) if manual else (dict(cands[-1]) if cands else None)
        out[key] = {"best": best, "candidates": [dict(r) for r in cands]}
    return out


def key_conflicts(conn: sqlite3.Connection, case_pk: int) -> dict[str, list]:
    """Keys with >1 distinct normalized value among non-manual rows."""
    found: dict[str, list] = {}
    for key in REVIEW_FIELDS:
        vals: dict[str, dict] = {}
        for r in conn.execute(
            """SELECT * FROM extracted_fields
               WHERE case_id = ? AND field_key = ?
               AND (method IS NULL OR method != 'manual') ORDER BY id""",
            (case_pk, key),
        ).fetchall():
            vals.setdefault(_norm(key, r["value"] or ""), dict(r))
        if len(vals) > 1:
            found[key] = list(vals.values())
    return found


def doc_label(conn: sqlite3.Connection, document_id: int | None) -> str:
    if not document_id:
        return "operator edit"
    row = conn.execute(
        "SELECT orig_filename FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    return row["orig_filename"] if row else f"document #{document_id}"


def validate_review(
    values: dict[str, str],
    current_best: dict[str, str],
    conflicts: dict[str, list],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (cleaned, errors). Never fills missing values silently."""
    cleaned: dict[str, str] = {}
    errors: dict[str, str] = {}

    name = (values.get("full_name") or "").strip()
    if not name:
        errors["full_name"] = "Full name is required."
    else:
        parsed = parse_name(name)
        if not parsed:
            errors["full_name"] = "Enter the name as 2-5 letters-only words."
        else:
            cleaned["full_name"] = parsed

    dob_raw = (values.get("dob") or "").strip()
    if not dob_raw:
        errors["dob"] = "Date of birth is required."
    else:
        parsed = parse_date_token(dob_raw)
        if parsed is None:
            errors["dob"] = "Enter a real date (YYYY-MM-DD or DD-MM-YYYY)."
        elif parsed[1]:
            errors["dob"] = (
                f"'{parsed[0]}' is ambiguous (day/month unclear). "
                "Use YYYY-MM-DD, e.g. 1995-08-17."
            )
        else:
            cleaned["dob"] = parsed[0]

    addr = (values.get("address") or "").strip()
    cleaned["address"] = addr[:500] if addr else ""

    lic = (values.get("licence_no") or "").strip()
    if lic:
        from .fields import parse_licence_no

        parsed_lic = parse_licence_no(lic)
        if not parsed_lic:
            errors["licence_no"] = (
                "Licence number looks invalid; check the document and re-enter.")
        else:
            cleaned["licence_no"] = parsed_lic
    else:
        cleaned["licence_no"] = ""

    appl = (values.get("appl_number") or "").strip()
    if appl:
        if not APPL_RE.match(appl):
            errors["appl_number"] = "Application number looks invalid; check and re-enter."
        else:
            cleaned["appl_number"] = appl
    else:
        cleaned["appl_number"] = ""

    for key, rows in conflicts.items():
        if key in errors:
            continue
        submitted = cleaned.get(key, "")
        if submitted == (current_best.get(key) or ""):
            opts = "; ".join(
                f"'{r['value']}' (from {r.get('_doc', '?')}"
                + (f", page {r['source_page']}" if r["source_page"] else "") + ")"
                for r in rows
            )
            errors[key] = (
                f"Conflicting values found and this field was not changed: {opts}. "
                "Pick one value and Validate & Save again."
            )
    return cleaned, errors


def save_review(
    conn: sqlite3.Connection, case_id: str, values: dict[str, str]
) -> tuple[sqlite3.Row, bool, dict[str, str]]:
    """Validate + save transactionally. Returns (case, changed, errors)."""
    case = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if case is None:
        raise ValueError(f"unknown case: {case_id}")
    info = best_and_candidates(conn, case["id"])
    current_best = {k: (v["best"]["value"] if v["best"] else "") or "" for k, v in info.items()}
    conflicts = key_conflicts(conn, case["id"])
    for rows in conflicts.values():
        for r in rows:
            r["_doc"] = doc_label(conn, r["document_id"])
    cleaned, errors = validate_review(values, current_best, conflicts)
    if errors:
        return case, False, errors

    now = utcnow_iso()
    changed = False
    try:
        for key in REVIEW_FIELDS:
            new_val = cleaned.get(key, "")
            if new_val != (current_best.get(key) or ""):
                conn.execute(
                    """INSERT INTO extracted_fields
                       (case_id, document_id, field_key, value, source_page,
                        method, confidence, created_at)
                       VALUES (?, NULL, ?, ?, NULL, 'manual', NULL, ?)""",
                    (case["id"], key, new_val or None, now),
                )
                changed = True
        new_appl = cleaned.get("appl_number") or None
        if (new_appl or "") != (case["appl_number"] or ""):
            conn.execute("UPDATE cases SET appl_number = ? WHERE id = ?", (new_appl, case["id"]))
            changed = True
        if case["status"] == "reviewed" and changed:
            conn.execute(
                """UPDATE cases SET status = 'needs_attention', reviewed_at = NULL,
                   review_version = review_version + 1, updated_at = ? WHERE id = ?""",
                (now, case["id"]),
            )
            conn.execute(
                "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (case["id"], "edit_after_review",
                 "operator edited a reviewed case; re-validation required", now),
            )
        conn.execute(
            "UPDATE cases SET status = 'reviewed', reviewed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, case["id"]),
        )
        conn.execute(
            "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (case["id"], "reviewed",
             "operator validated the case record (acknowledgement, not authenticity proof)", now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    case = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    return case, changed, {}


def assign_document(conn: sqlite3.Connection, doc_id: int, target_case: str) -> sqlite3.Row:
    target = conn.execute("SELECT * FROM cases WHERE case_id = ?", (target_case,)).fetchone()
    if target is None:
        raise ValueError(f"unknown case: {target_case}")
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if doc is None:
        raise ValueError(f"unknown document: {doc_id}")
    now = utcnow_iso()
    try:
        conn.execute("UPDATE documents SET case_id = ? WHERE id = ?", (target["id"], doc_id))
        conn.execute(
            "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (target["id"], "assigned",
             f"document '{doc['orig_filename']}' manually attached by operator", now),
        )
        if target["status"] == "reviewed":
            conn.execute(
                """UPDATE cases SET status = 'needs_attention', reviewed_at = NULL,
                   review_version = review_version + 1, updated_at = ? WHERE id = ?""",
                (now, target["id"]),
            )
            conn.execute(
                "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (target["id"], "edit_after_review",
                 "document attached after review; re-validation required", now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute("SELECT * FROM cases WHERE case_id = ?", (target_case,)).fetchone()


def merge_cases(conn: sqlite3.Connection, source_case: str, target_case: str) -> sqlite3.Row:
    if source_case == target_case:
        raise ValueError("cannot merge a case into itself")
    source = conn.execute("SELECT * FROM cases WHERE case_id = ?", (source_case,)).fetchone()
    target = conn.execute("SELECT * FROM cases WHERE case_id = ?", (target_case,)).fetchone()
    if source is None:
        raise ValueError(f"unknown case: {source_case}")
    if target is None:
        raise ValueError(f"unknown case: {target_case}")
    now = utcnow_iso()
    try:
        n_docs = conn.execute(
            "SELECT COUNT(*) n FROM documents WHERE case_id = ?", (source["id"],)
        ).fetchone()["n"]
        n_fields = conn.execute(
            "SELECT COUNT(*) n FROM extracted_fields WHERE case_id = ?", (source["id"],)
        ).fetchone()["n"]
        conn.execute("UPDATE documents SET case_id = ? WHERE case_id = ?",
                     (target["id"], source["id"]))
        conn.execute("UPDATE extracted_fields SET case_id = ? WHERE case_id = ?",
                     (target["id"], source["id"]))
        conn.execute("UPDATE case_events SET case_id = ? WHERE case_id = ?",
                     (target["id"], source["id"]))
        conn.execute(
            "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (target["id"], "merged",
             f"merged {source_case} into {target_case} "
             f"({n_docs} documents, {n_fields} fields moved; file history preserved)", now),
        )
        if target["status"] == "reviewed":
            conn.execute(
                """UPDATE cases SET status = 'needs_attention', reviewed_at = NULL,
                   review_version = review_version + 1, updated_at = ? WHERE id = ?""",
                (now, target["id"]),
            )
            conn.execute(
                "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (target["id"], "edit_after_review",
                 "merge changed a reviewed case; re-validation required", now),
            )
        conn.execute("DELETE FROM cases WHERE id = ?", (source["id"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute("SELECT * FROM cases WHERE case_id = ?", (target_case,)).fetchone()


def case_display_name(conn: sqlite3.Connection, case_pk: int) -> str:
    row = conn.execute(
        """SELECT value FROM extracted_fields
           WHERE case_id = ? AND field_key = 'full_name' AND method = 'manual'
           AND value IS NOT NULL ORDER BY id DESC LIMIT 1""",
        (case_pk,),
    ).fetchone()
    if row and row["value"]:
        return row["value"]
    row = conn.execute(
        """SELECT value FROM extracted_fields
           WHERE case_id = ? AND field_key = 'full_name' AND value IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (case_pk,),
    ).fetchone()
    return row["value"] if row and row["value"] else "—"


def next_action(status: str, n_fields: int) -> str:
    if status == "reviewed":
        return "Done"
    if status == "needs_attention":
        return "Resolve"
    if status in ("ready", "queued", "processing", "waiting", "failed"):
        return "Review" if n_fields else "Waiting for processing"
    return "Review"


def search_cases(
    conn: sqlite3.Connection, q: str = "", status: str = "",
    limit: int = PER_PAGE, offset: int = 0,
) -> list[sqlite3.Row]:
    q = (q or "").strip()
    status = (status or "").strip()
    clauses, params = [], []
    if status:
        clauses.append("c.status = ?")
        params.append(status)
    if q:
        clauses.append(
            "(c.case_id LIKE ? OR COALESCE(c.appl_number, '') LIKE ? OR EXISTS "
            "(SELECT 1 FROM extracted_fields f WHERE f.case_id = c.id "
            "AND f.field_key = 'full_name' AND f.value LIKE ?))"
        )
        like = f"%{q}%"
        params.extend([like, like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT c.* FROM cases c {where} ORDER BY c.created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()


def count_cases(conn: sqlite3.Connection, q: str = "", status: str = "") -> int:
    q = (q or "").strip()
    status = (status or "").strip()
    clauses, params = [], []
    if status:
        clauses.append("c.status = ?")
        params.append(status)
    if q:
        clauses.append(
            "(c.case_id LIKE ? OR COALESCE(c.appl_number, '') LIKE ? OR EXISTS "
            "(SELECT 1 FROM extracted_fields f WHERE f.case_id = c.id "
            "AND f.field_key = 'full_name' AND f.value LIKE ?))"
        )
        like = f"%{q}%"
        params.extend([like, like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT COUNT(*) n FROM cases c {where}", params
    ).fetchone()["n"]


def needs_attention_items(conn: sqlite3.Connection) -> dict:
    cases = conn.execute(
        "SELECT * FROM cases WHERE status = 'needs_attention' ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    jobs = conn.execute(
        "SELECT * FROM intake_jobs WHERE state IN ('failed', 'needs_attention')"
        " ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    docs = conn.execute(
        "SELECT * FROM documents WHERE status IN ('failed', 'needs_attention')"
        " OR ocr_status IN ('failed', 'needs_attention', 'unavailable')"
        " ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    unassigned = conn.execute(
        "SELECT * FROM documents WHERE case_id IS NULL ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    return {"cases": cases, "jobs": jobs, "docs": docs, "unassigned": unassigned}
