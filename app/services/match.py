"""Conservative case matching (Phase 4).

Documented auto-attach rules -- a file is attached to an existing case ONLY
when ALL of these hold:
  1. The document yielded a full_name (confidence >= 0.6) AND a dob in ISO
     form (confidence >= 0.6). Name alone NEVER attaches.
  2. Exactly ONE existing case holds the same normalized name AND same dob.
  3. The document is not a photo/signature (those carry no identity text;
     the caller skips them before matching).

Anything else -- name-only, ambiguous type, multiple matches, conflicts --
creates or reuses a provisional case (customer NULL, status needs_attention)
and is exposed for manual assignment/merge in the review UI (Phase 5).
Every decision is recorded as a case event.
"""
from __future__ import annotations

import re
import sqlite3

from ..db import (
    create_case,
    create_extracted_field,
    generate_case_id,
    get_case,
    latest_field_value,
    list_case_fields,
    touch_case_for_edit,
    update_case_status,
    utcnow_iso,
)
from .fields import normalize_name


def _matchable(candidates: list[dict]) -> tuple[str | None, str | None, str | None, str | None]:
    name = dob = aadhaar = pan = None
    for c in candidates:
        if c["field_key"] == "full_name" and (c.get("confidence") or 0) >= 0.6:
            name = normalize_name(c["value"])
        if (c["field_key"] == "dob" and (c.get("confidence") or 0) >= 0.6
                and "-" in (c["value"] or "")):
            dob = c["value"].strip()
        if c["field_key"] == "aadhaar_no" and (c.get("confidence") or 0) >= 0.7:
            digits = re.sub(r"\D", "", c["value"] or "")
            if len(digits) == 12:
                aadhaar = digits
        if c["field_key"] == "pan_no" and (c.get("confidence") or 0) >= 0.7:
            p = (c["value"] or "").strip().upper()
            if len(p) == 10:
                pan = p
    return name, dob, aadhaar, pan


def find_matching_cases(
    conn: sqlite3.Connection,
    name_norm: str | None = None,
    dob_iso: str | None = None,
    aadhaar_digits: str | None = None,
    pan_norm: str | None = None,
) -> tuple[list[sqlite3.Row], str]:
    """Cases matching strong identity criteria (aadhaar_no, pan_no, or name+dob)."""
    hits = []
    reason = ""

    # 1. Match by unique 12-digit Aadhaar number
    if aadhaar_digits:
        rows = conn.execute(
            "SELECT DISTINCT case_id, value FROM extracted_fields WHERE field_key = 'aadhaar_no'"
        ).fetchall()
        for r in rows:
            clean_digits = re.sub(r"\D", "", r["value"] or "")
            if clean_digits == aadhaar_digits:
                c_row = conn.execute("SELECT * FROM cases WHERE id = ?", (r["case_id"],)).fetchone()
                if c_row and not any(h["id"] == c_row["id"] for h in hits):
                    hits.append(c_row)
        if hits:
            reason = "matched on aadhaar_no"

    # 2. Match by unique 10-char PAN number
    if pan_norm and not hits:
        rows = conn.execute(
            "SELECT DISTINCT case_id, value FROM extracted_fields WHERE field_key = 'pan_no'"
        ).fetchall()
        for r in rows:
            clean_pan = (r["value"] or "").strip().upper()
            if clean_pan == pan_norm:
                c_row = conn.execute("SELECT * FROM cases WHERE id = ?", (r["case_id"],)).fetchone()
                if c_row and not any(h["id"] == c_row["id"] for h in hits):
                    hits.append(c_row)
        if hits:
            reason = "matched on pan_no"

    # 3. Match by (full_name, dob)
    if name_norm and dob_iso and not hits:
        case_ids = {r["case_id"] for r in conn.execute(
            "SELECT DISTINCT case_id FROM extracted_fields WHERE field_key = 'full_name'"
        ).fetchall()}
        for (cid,) in [(c,) for c in case_ids]:
            nrow = latest_field_value(conn, cid, "full_name")
            drow = latest_field_value(conn, cid, "dob")
            if (nrow and drow and normalize_name(nrow["value"] or "") == name_norm
                    and (drow["value"] or "").strip() == dob_iso):
                c_row = conn.execute("SELECT * FROM cases WHERE id = ?", (cid,)).fetchone()
                if c_row and not any(h["id"] == c_row["id"] for h in hits):
                    hits.append(c_row)
        if hits:
            reason = "matched on name+dob"

    return hits, reason


def _event(conn: sqlite3.Connection, case_pk: int, kind: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
        (case_pk, kind, detail[:500], utcnow_iso()),
    )
    conn.commit()


def create_provisional_case(
    conn: sqlite3.Connection, reason: str, date_prefix: str | None = None
) -> sqlite3.Row:
    case_id = generate_case_id(conn, date_prefix)
    row = create_case(conn, case_id, customer_id=None, status="needs_attention")
    _event(conn, row["id"], "provisional",
           f"provisional intake; {reason}; needs operator assignment/merge")
    return get_case(conn, case_id)


def detect_conflicts(conn: sqlite3.Connection, case_pk: int) -> list[str]:
    """Field keys with >1 distinct normalized value on one case."""
    keys = [r["field_key"] for r in conn.execute(
        "SELECT DISTINCT field_key FROM extracted_fields WHERE case_id = ?", (case_pk,)
    ).fetchall()]
    bad = []
    for k in keys:
        vals = {(r["value"] or "").strip().lower()
                for r in conn.execute(
                    "SELECT value FROM extracted_fields WHERE case_id = ? AND field_key = ?",
                    (case_pk, k)).fetchall()}
        if len(vals) > 1:
            bad.append(k)
    return bad


def attach_or_provisional(
    conn: sqlite3.Connection,
    document_id: int,
    candidates: list[dict],
) -> tuple[sqlite3.Row, str]:
    """Store candidates on a matched or provisional case.

    Returns (case_row, outcome) where outcome is 'attached', 'provisional',
    or 'attached_with_conflict'.
    """
    name_norm, dob_iso, aadhaar_digits, pan_norm = _matchable(candidates)
    case = None
    outcome = "provisional"
    match_reason = ""
    hits, match_reason = find_matching_cases(
        conn,
        name_norm=name_norm,
        dob_iso=dob_iso,
        aadhaar_digits=aadhaar_digits,
        pan_norm=pan_norm,
    )
    if len(hits) == 1:
        case = hits[0]
        outcome = "attached"
    if case is None:
        reason = ("no unique strong identity match"
                  if not (hits or (name_norm and dob_iso) or aadhaar_digits or pan_norm)
                  else "match ambiguous across cases" if hits else "no identity fields")
        case = create_provisional_case(conn, reason)
    for c in candidates:
        create_extracted_field(
            conn, case["id"], c["field_key"], c["value"],
            document_id=document_id, source_page=c.get("source_page"),
            method=c.get("method"), confidence=c.get("confidence"),
        )
    if outcome == "attached":
        conflicts = [k for k in detect_conflicts(conn, case["id"])
                     if k in {c["field_key"] for c in candidates}]
        if conflicts:
            outcome = "attached_with_conflict"
            _event(conn, case["id"], "conflict",
                   f"new document disagrees on {', '.join(conflicts)}; operator review required")
            if case["status"] == "reviewed":
                touch_case_for_edit(conn, case["case_id"])
            elif case["status"] != "needs_attention":
                update_case_status(conn, case["case_id"], "needs_attention")
        else:
            _event(conn, case["id"], "auto_attached",
                    f"document #{document_id} {match_reason}")
            # New evidence on a reviewed case always requires re-review.
            if case["status"] == "reviewed":
                touch_case_for_edit(conn, case["case_id"])
    case = get_case(conn, case["case_id"])
    return case, outcome
