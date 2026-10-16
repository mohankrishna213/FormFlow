"""Operator review, assignment and merge logic (Phase 5).

Single case-level Validate & Save; no per-field checkboxes anywhere.
Operator edits are stored as extracted_fields rows with method='manual'
(document NULL) so every value stays traceable. Displayed value per key =
latest manual row if any, else latest extracted row.

Six-field review: full_name, father_name, dob, address, licence_no and
application_no. ``application_no`` is extracted under that key, but the
operator-confirmed official value lives in the existing ``cases.appl_number``
column (review form input ``appl_number``); no manual extracted_fields row
is written for it, so the OCR candidates stay evidence while the case column
stays the single official value.

Printed-label note: the Phase 4A parser emits the ``father_name`` key for
both the "Father's Name" family and the "Son/Wife/Daughter of" family, but
candidate rows do not record which printed label matched (the data model
has no column for it). The review UI therefore uses the neutral label
"Father's / related person's name" and never fabricates a relationship.

Conflict freshness (Phase 5.1.1): resolving a conflicted key records an
evidence anchor in ``conflict_acks`` -- the newest candidate row id the
operator acknowledged (``upto_id``) plus the normalized value they settled
on. Candidate rows are never deleted and row ids only grow (merge preserves
them), so "id > upto_id" means "evidence arrived after the acknowledgement".
A save is blocked only while that newer evidence disagrees with the settled
value; agreeing evidence never re-opens a resolved conflict, and an old
resolution is never treated as permanent proof. Keys resolved before this
table existed fall back to the row id of their latest manual value, so
upgrading does not force a re-resolve without cause.
"""
from __future__ import annotations

import re
import sqlite3

from ..db import utcnow_iso
from .fields import (
    normalize_name,
    parse_date_token,
    parse_licence_no,
    parse_name,
)

REVIEW_FIELDS = ("full_name", "father_name", "dob", "address", "licence_no",
                 "application_no")
# Operator edits become manual extracted_fields rows for these keys.
# application_no is persisted at case level (cases.appl_number) instead, so
# the confirmed value is not duplicated into the candidate table.
MANUAL_FIELDS = ("full_name", "father_name", "dob", "address", "licence_no")
# Review-form input name per field key; application_no keeps the existing
# case-level form/column name.
FORM_KEY = {
    "full_name": "full_name",
    "father_name": "father_name",
    "dob": "dob",
    "address": "address",
    "licence_no": "licence_no",
    "application_no": "appl_number",
}
FIELD_LABELS = {
    "full_name": "Full name",
    "father_name": "Father's / related person's name",
    "dob": "Date of birth",
    "address": "Address",
    "licence_no": "Driving licence number",
    "application_no": "Application number",
}
REQUIRED_FIELDS = ("full_name", "dob")
APPL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/]{1,28}[A-Za-z0-9]$")
PER_PAGE = 20
# Radio value meaning "keep the current (blank) value" -- a plain empty
# radio value is indistinguishable from "no radio picked" in form data.
KEEP_BLANK = "__keep__"


def _norm(key: str, value: str) -> str:
    v = (value or "").strip()
    if key in ("full_name", "father_name"):
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


def candidate_source(conn: sqlite3.Connection, document_id: int | None) -> str:
    """Display label for an extracted candidate row (manual rows excluded).

    Never claims "operator edit" for an OCR row whose document is unknown --
    provenance is either the real document or an explicit gap.
    """
    return doc_label(conn, document_id) if document_id else "source not recorded"


def with_sources(conn: sqlite3.Connection, info: dict, conflicts: dict):
    """Attach document filenames for display: best candidates + conflict rows."""
    for key in REVIEW_FIELDS:
        best = info.get(key, {}).get("best")
        if best and best.get("document_id"):
            best["_doc"] = doc_label(conn, best["document_id"])
    for rows in conflicts.values():
        for r in rows:
            r["_doc"] = candidate_source(conn, r["document_id"])
    return info, conflicts


def latest_conflict_ack(
    conn: sqlite3.Connection, case_pk: int, field_key: str
) -> sqlite3.Row | None:
    """Newest recorded evidence anchor for (case, key), or None.

    One small append-only row is written per resolution (see ``save_review``);
    the newest row is the acknowledgement that counts.
    """
    return conn.execute(
        """SELECT * FROM conflict_acks WHERE case_id = ? AND field_key = ?
           ORDER BY id DESC LIMIT 1""",
        (case_pk, field_key),
    ).fetchone()


def conflict_status(
    conn: sqlite3.Connection, case: sqlite3.Row, info: dict, conflicts: dict
) -> dict[str, dict]:
    """Freshness of the recorded resolution for each conflicted key.

    Per key: ``anchored`` (an operator decision is on record), ``fresh``
    (candidate rows newer than the anchor disagree with the settled value),
    ``need_pick`` (an untouched save would be blocked), and the keep-current
    radio the UI should offer (``keep_radio``; ``KEEP_BLANK`` when the
    settled value is empty). A manual value or ``cases.appl_number`` alone is
    never proof that later evidence was reviewed -- only the anchor covers
    the evidence that existed when it was recorded.
    """
    prefill = review_prefill(info, case)
    out: dict[str, dict] = {}
    for key, rows in conflicts.items():
        anchored, upto, val_norm = False, None, ""
        ack = latest_conflict_ack(conn, case["id"], key)
        if ack is not None:
            anchored, upto, val_norm = True, ack["upto_id"], ack["value_norm"]
        elif key != "application_no":
            # Fallback for keys resolved before conflict_acks existed: the
            # latest manual value row (its id marks when the operator last
            # decided; an empty value anchors "deliberately blank").
            manual = conn.execute(
                """SELECT id, value FROM extracted_fields
                   WHERE case_id = ? AND field_key = ? AND method = 'manual'
                   ORDER BY id DESC LIMIT 1""",
                (case["id"], key),
            ).fetchone()
            if manual is not None:
                anchored, upto = True, manual["id"]
                val_norm = _norm(key, manual["value"] or "")
        fresh = False
        if anchored:
            for r in conn.execute(
                """SELECT value FROM extracted_fields
                   WHERE case_id = ? AND field_key = ?
                   AND (method IS NULL OR method != 'manual') AND id > ?""",
                (case["id"], key, upto),
            ).fetchall():
                if _norm(key, r["value"] or "") != val_norm:
                    fresh = True
                    break
        need_pick = (not anchored) or fresh
        keep_radio = None
        if need_pick:
            current = prefill.get(FORM_KEY[key], "")
            cand_norms = {_norm(key, r["value"] or "") for r in rows}
            if _norm(key, current) not in cand_norms:
                keep_radio = current or KEEP_BLANK
        out[key] = {
            "anchored": anchored,
            "fresh": fresh,
            "need_pick": need_pick,
            "keep_radio": keep_radio,
            "keep_blank": keep_radio == KEEP_BLANK,
        }
    return out


def review_prefill(info: dict, case: sqlite3.Row,
                   values: dict | None = None) -> dict[str, str]:
    """Form values for the review inputs, keyed by form input name.

    On validation errors the submitted values are returned unchanged; on a
    fresh form the latest display value is used. The application number
    prefers the saved official case value and falls back to the extracted
    candidate as an editable draft -- never invented.
    """
    out: dict[str, str] = {}
    for key in REVIEW_FIELDS:
        form_key = FORM_KEY[key]
        if values is not None:
            # A posted candidate pick is what the operator aimed at; show it
            # in the text box so a failed validation re-render preserves it.
            # The keep-blank control means "no value", not the stale text.
            choice = (values.get(f"choice_{key}") or "").strip()
            if choice == KEEP_BLANK:
                out[form_key] = ""
                continue
            out[form_key] = choice or values.get(form_key, "")
            continue
        best = info[key]["best"]
        value = (best["value"] or "") if best else ""
        if key == "application_no":
            value = (case["appl_number"] or "").strip() or value
        out[form_key] = value
    return out


def collect_form_values(form) -> dict[str, str]:
    """Read the six review inputs plus any conflict-selection radios.

    ``choice_<field_key>`` carries an explicit candidate pick from the
    conflict controls; it is validated as part of the review save.
    """
    values = {k: (form.get(k) or "") for k in FORM_KEY.values()}
    for key in REVIEW_FIELDS:
        values[f"choice_{key}"] = form.get(f"choice_{key}") or ""
    return values


def form_choices(values: dict[str, str] | None) -> dict[str, str]:
    """Posted conflict picks by field key (empty map for a fresh form)."""
    if not values:
        return {}
    return {key: (values.get(f"choice_{key}") or "").strip()
            for key in REVIEW_FIELDS}


def _clean_full_name(raw: str) -> tuple[str, str | None]:
    name = (raw or "").strip()
    if not name:
        return "", "Full name is required."
    parsed = parse_name(name)
    if not parsed:
        return "", "Enter the name as 2-5 letters-only words."
    return parsed, None


def _clean_father_name(raw: str) -> tuple[str, str | None]:
    name = (raw or "").strip()
    if not name:
        return "", None  # optional: a missing related-person name is fine
    parsed = parse_name(name)
    if not parsed:
        return "", "Enter the related person's name as 2-5 letters-only words."
    return parsed, None


def _clean_dob(raw: str) -> tuple[str, str | None]:
    dob_raw = (raw or "").strip()
    if not dob_raw:
        return "", "Date of birth is required."
    parsed = parse_date_token(dob_raw)
    if parsed is None:
        return "", "Enter a real date (YYYY-MM-DD or DD-MM-YYYY)."
    if parsed[1]:
        return "", (f"'{parsed[0]}' is ambiguous (day/month unclear). "
                    "Use YYYY-MM-DD, e.g. 1995-08-17.")
    return parsed[0], None


def _clean_address(raw: str) -> tuple[str, str | None]:
    addr = (raw or "").strip()
    return (addr[:500] if addr else ""), None


def _clean_licence_no(raw: str) -> tuple[str, str | None]:
    lic = (raw or "").strip()
    if not lic:
        return "", None
    parsed = parse_licence_no(lic)
    if not parsed:
        return "", "Licence number looks invalid; check the document and re-enter."
    return parsed, None


def _clean_application_no(raw: str) -> tuple[str, str | None]:
    appl = (raw or "").strip()
    if not appl:
        return "", None
    if not APPL_RE.match(appl):
        return "", "Application number looks invalid; check and re-enter."
    return appl, None


_CLEANERS = {
    "full_name": _clean_full_name,
    "father_name": _clean_father_name,
    "dob": _clean_dob,
    "address": _clean_address,
    "licence_no": _clean_licence_no,
    "application_no": _clean_application_no,
}


def validate_review(
    values: dict[str, str],
    prefill: dict[str, str],
    conflicts: dict[str, list],
    status: dict[str, dict],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (cleaned, errors) keyed by review field key.

    Never fills missing values silently. A conflicted key blocks an untouched
    submitted value (norm-equal to the ``prefill`` \u2014 what the form showed
    before editing) unless the operator picked a listed candidate
    (``choice_<key>``) or the current evidence was already acknowledged
    (``status``: anchored and not fresh). Evidence newer than the anchor that
    disagrees with the settled value re-blocks until it is reviewed too.
    """
    cleaned: dict[str, str] = {}
    errors: dict[str, str] = {}

    for key in REVIEW_FIELDS:
        val, err = _CLEANERS[key](values.get(FORM_KEY[key]) or "")
        cleaned[key] = val
        if err:
            errors[key] = err

    for key, rows in conflicts.items():
        if key in errors:
            continue
        choice = (values.get(f"choice_{key}") or "").strip()
        if choice:
            # The operator picked a listed candidate (or the keep-blank
            # control): the selection itself is the explicit resolution and
            # wins over the text input (it is the value the operator
            # actually aimed at, also without JavaScript).
            val, err = _CLEANERS[key]("" if choice == KEEP_BLANK else choice)
            if err:
                errors[key] = err
            else:
                cleaned[key] = val
            continue
        st = status.get(key, {})
        if st.get("anchored") and not st.get("fresh"):
            # This exact evidence set was already acknowledged; keep the
            # settled value instead of re-blocking the same rows forever.
            continue
        if _norm(key, cleaned.get(key, "")) == _norm(key, prefill.get(FORM_KEY[key], "")):
            opts = "; ".join(
                f"'{r['value']}' (from {r.get('_doc', '?')}"
                + (f", page {r['source_page']}" if r["source_page"] else "") + ")"
                for r in rows
            )
            if st.get("fresh"):
                errors[key] = (
                    f"Conflicting values found \u2014 new evidence arrived after your "
                    f"saved value: {opts}. Pick the value you verified (or type a "
                    "correction) and Validate & Save again."
                )
            else:
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
            r["_doc"] = candidate_source(conn, r["document_id"])
    prefill = review_prefill(info, case)
    status = conflict_status(conn, case, info, conflicts)
    cleaned, errors = validate_review(values, prefill, conflicts, status)
    if errors:
        return case, False, errors

    now = utcnow_iso()
    changed = False
    # Explicit conflict picks are always recorded (even when the picked
    # value equals the current best): the new row marks the resolution
    # time, which is what evidence arriving later is measured against.
    chosen = {k for k in conflicts if (values.get(f"choice_{k}") or "").strip()}
    try:
        for key in MANUAL_FIELDS:
            new_val = cleaned.get(key, "")
            if new_val != (current_best.get(key) or "") or key in chosen:
                conn.execute(
                    """INSERT INTO extracted_fields
                       (case_id, document_id, field_key, value, source_page,
                        method, confidence, created_at)
                       VALUES (?, NULL, ?, ?, NULL, 'manual', NULL, ?)""",
                    (case["id"], key, new_val or None, now),
                )
                changed = True
        new_appl = cleaned.get("application_no") or None
        if (new_appl or "") != (case["appl_number"] or ""):
            conn.execute("UPDATE cases SET appl_number = ? WHERE id = ?", (new_appl, case["id"]))
            changed = True
        # Acknowledge the evidence set for every conflicted key. Fresh
        # evidence (newer rows disagreeing with the settled value) cannot
        # reach this point without being picked or changed, so a successful
        # save may mark the current candidate rows as reviewed; agreeing
        # rows merely move the anchor forward.
        for key in conflicts:
            upto = conn.execute(
                """SELECT COALESCE(MAX(id), 0) AS upto FROM extracted_fields
                   WHERE case_id = ? AND field_key = ?
                   AND (method IS NULL OR method != 'manual')""",
                (case["id"], key),
            ).fetchone()["upto"]
            val_norm = _norm(key, cleaned.get(key, ""))
            last = latest_conflict_ack(conn, case["id"], key)
            if (last is None or last["upto_id"] != upto
                    or last["value_norm"] != val_norm):
                conn.execute(
                    """INSERT INTO conflict_acks
                       (case_id, field_key, upto_id, value_norm, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (case["id"], key, upto, val_norm, now),
                )
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
        # Source acknowledgements covered the source case's own evidence
        # history; they must not vouch for the target's combined evidence.
        # Merged rows keep their ids, so evidence that disagrees with the
        # target's settled values is naturally newer than the target anchors.
        conn.execute("DELETE FROM conflict_acks WHERE case_id = ?", (source["id"],))
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
