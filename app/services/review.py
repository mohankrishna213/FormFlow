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
from typing import Any

from ..db import (
    get_customer_fields,
    list_case_extracted_keys,
    upsert_customer_field,
    utcnow_iso,
)
from .catalog import (
    CATEGORIES,
    CATEGORY_LABELS,
    MAX_FIELDS_PER_CASE,
    RESERVED_KEYS,
    FieldDef,
    get_catalog,
    sanitize_field_value,
    validate_field_key,
)
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


class FieldLabelsProxy(dict):
    """Dictionary proxy resolving labels from catalogue with legacy defaults."""

    def __init__(self, initial: dict[str, str] | None = None):
        super().__init__(initial or {})

    def __getitem__(self, key: str) -> str:
        if key in self:
            return super().__getitem__(key)
        return get_catalog().get_label(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self:
            return super().get(key, default)
        lbl = get_catalog().get_label(key)
        if lbl != key:
            return lbl
        return default if default is not None else key

    def __contains__(self, key: object) -> bool:
        return True


class FormKeyProxy(dict):
    """Dictionary proxy mapping field_key to HTML form input name."""

    def __init__(self, initial: dict[str, str] | None = None):
        super().__init__(initial or {})

    def __getitem__(self, key: str) -> str:
        if key == "application_no":
            return "appl_number"
        return super().get(key, key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "application_no":
            return "appl_number"
        return super().get(key, default if default is not None else key)


def form_key_for(field_key: str) -> str:
    if field_key == "application_no":
        return "appl_number"
    return field_key


def field_key_from_form(form_key: str) -> str:
    if form_key == "appl_number":
        return "application_no"
    return form_key


# Review-form input name per field key; application_no keeps the existing
# case-level form/column name.
FORM_KEY = FormKeyProxy({
    "full_name": "full_name",
    "father_name": "father_name",
    "dob": "dob",
    "address": "address",
    "licence_no": "licence_no",
    "application_no": "appl_number",
})

FIELD_LABELS = FieldLabelsProxy({
    "full_name": "Full name",
    "father_name": "Father's / related person's name",
    "dob": "Date of birth",
    "address": "Address",
    "licence_no": "Learner's Licence Number",
    "application_no": "Application number",
})
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


def get_case_field_keys(
    conn: sqlite3.Connection, case_pk: int, customer_id: int | None = None
) -> list[str]:
    """Return ordered list of candidate/catalogue keys active for this case."""
    cat = get_catalog()
    keys: list[str] = list(cat.list_keys())
    seen: set[str] = set(keys)

    for k in list_case_extracted_keys(conn, case_pk):
        if k not in seen:
            keys.append(k)
            seen.add(k)

    if customer_id:
        c_fields = get_customer_fields(conn, customer_id)
        for k in c_fields:
            if k not in seen:
                keys.append(k)
                seen.add(k)

    if len(keys) > MAX_FIELDS_PER_CASE:
        keys = keys[:MAX_FIELDS_PER_CASE]

    return keys


def best_and_candidates(
    conn: sqlite3.Connection, case_pk: int, keys: list[str] | None = None
) -> dict[str, dict]:
    """Per review key: best value, its provenance, and non-manual candidates.

    Considers case-level manual edits, OCR candidates, and customer-level
    reusable profile fallbacks. Case-level evidence always takes precedence over
    customer profile data.
    """
    case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_pk,)).fetchone()
    customer_id = case["customer_id"] if case else None

    if keys is None:
        keys = get_case_field_keys(conn, case_pk, customer_id)

    # Guarantee core REVIEW_FIELDS are always present in the result
    all_keys = list(keys)
    for rf in REVIEW_FIELDS:
        if rf not in all_keys:
            all_keys.append(rf)

    cust_fields = get_customer_fields(conn, customer_id) if customer_id else {}

    out: dict[str, dict] = {}
    for key in all_keys:
        rows = conn.execute(
            """SELECT * FROM extracted_fields WHERE case_id = ? AND field_key = ?
               ORDER BY id""",
            (case_pk, key),
        ).fetchall()
        manual = [r for r in rows if (r["method"] or "") == "manual"]
        cands = [r for r in rows if (r["method"] or "") != "manual"]

        best = None
        if manual:
            best = dict(manual[-1])
        elif cands:
            best = dict(cands[-1])
        elif key in cust_fields and cust_fields[key]["value"]:
            best = {
                "id": None,
                "case_id": case_pk,
                "document_id": cust_fields[key]["source_document_id"],
                "field_key": key,
                "value": cust_fields[key]["value"],
                "source_page": None,
                "method": "customer_profile",
                "confidence": 1.0,
                "created_at": cust_fields[key]["confirmed_at"],
            }

        out[key] = {"best": best, "candidates": [dict(r) for r in cands]}
    return out


def key_conflicts(
    conn: sqlite3.Connection, case_pk: int, keys: list[str] | None = None
) -> dict[str, list]:
    """Keys with >1 distinct normalized value among non-manual rows."""
    if keys is None:
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_pk,)).fetchone()
        customer_id = case["customer_id"] if case else None
        keys = get_case_field_keys(conn, case_pk, customer_id)

    all_keys = list(keys)
    for rf in REVIEW_FIELDS:
        if rf not in all_keys:
            all_keys.append(rf)

    found: dict[str, list] = {}
    for key in all_keys:
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
    for key, slot in info.items():
        best = slot.get("best")
        if best:
            if best.get("document_id"):
                best["_doc"] = doc_label(conn, best["document_id"])
            elif best.get("method") == "customer_profile":
                best["_doc"] = "reusable customer profile"
    for rows in conflicts.values():
        for r in rows:
            r["_doc"] = candidate_source(conn, r.get("document_id"))
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
    for key in info:
        form_key = form_key_for(key)
        if values is not None:
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
    """Read all submitted review inputs, confirmation checkboxes, and conflict radios."""
    values: dict[str, str] = {}
    for k in form.keys():
        if k in RESERVED_KEYS:
            continue
        v = form.get(k) or ""
        values[k] = sanitize_field_value(v)
    for k in FORM_KEY.values():
        if k not in values:
            values[k] = ""
    return values


def form_choices(values: dict[str, str] | None) -> dict[str, str]:
    """Posted conflict picks by field key (empty map for a fresh form)."""
    if not values:
        return {}
    out: dict[str, str] = {}
    for k, v in values.items():
        if k.startswith("choice_"):
            out[k[len("choice_"):]] = (v or "").strip()
    for key in REVIEW_FIELDS:
        if key not in out:
            out[key] = (values.get(f"choice_{key}") or "").strip()
    return out


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


def _clean_field(key: str, raw: str) -> tuple[str, str | None]:
    """Validate and clean field value according to cleaners and catalog rules."""
    if key in _CLEANERS:
        return _CLEANERS[key](raw)

    v = sanitize_field_value(raw)
    try:
        validate_field_key(key)
    except ValueError as e:
        return "", str(e)

    fdef = get_catalog().get(key)
    if fdef:
        if fdef.required and not v:
            return "", f"{fdef.label} is required."
        if fdef.data_type == "date" and v:
            parsed = parse_date_token(v)
            if parsed is None:
                return "", "Enter a real date (YYYY-MM-DD or DD-MM-YYYY)."
            if parsed[1]:
                return "", f"'{parsed[0]}' is ambiguous (day/month unclear). Use YYYY-MM-DD."
            return parsed[0], None
        if fdef.options and v:
            match = next((opt for opt in fdef.options if opt.lower() == v.lower()), None)
            if match:
                return match, None
            return v, None
        return v, None
    return v, None


def validate_review(
    values: dict[str, str],
    prefill: dict[str, str],
    conflicts: dict[str, list],
    status: dict[str, dict],
    case_keys: list[str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (cleaned, errors) keyed by field key."""
    cleaned: dict[str, str] = {}
    errors: dict[str, str] = {}

    keys_to_check = set(case_keys) if case_keys else set(REVIEW_FIELDS)
    for k in values:
        if not k.startswith("choice_") and not k.startswith("confirm_"):
            fk = field_key_from_form(k)
            if fk not in RESERVED_KEYS:
                try:
                    validate_field_key(fk)
                    keys_to_check.add(fk)
                except ValueError:
                    pass

    for key in keys_to_check:
        form_k = form_key_for(key)
        raw = values.get(form_k)
        if raw is None and key not in REQUIRED_FIELDS:
            raw = prefill.get(form_k, "")
        val, err = _clean_field(key, raw or "")
        cleaned[key] = val
        if err:
            errors[key] = err

    for key, rows in conflicts.items():
        if key in errors:
            continue
        choice = (values.get(f"choice_{key}") or "").strip()
        if choice:
            val, err = _clean_field(key, "" if choice == KEEP_BLANK else choice)
            if err:
                errors[key] = err
            else:
                cleaned[key] = val
            continue
        st = status.get(key, {})
        if st.get("anchored") and not st.get("fresh"):
            continue
        form_k = form_key_for(key)
        if _norm(key, cleaned.get(key, "")) == _norm(key, prefill.get(form_k, "")):
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

    all_keys = get_case_field_keys(conn, case["id"], case["customer_id"])
    for k in values:
        if not k.startswith("choice_") and not k.startswith("confirm_"):
            fk = field_key_from_form(k)
            if fk not in all_keys and fk not in RESERVED_KEYS:
                try:
                    validate_field_key(fk)
                    all_keys.append(fk)
                except ValueError:
                    pass

    info = best_and_candidates(conn, case["id"], keys=all_keys)
    current_best = {k: (v["best"]["value"] if v["best"] else "") or "" for k, v in info.items()}
    conflicts = key_conflicts(conn, case["id"], keys=all_keys)
    for rows in conflicts.values():
        for r in rows:
            r["_doc"] = candidate_source(conn, r.get("document_id"))
    prefill = review_prefill(info, case)
    status = conflict_status(conn, case, info, conflicts)
    cleaned, errors = validate_review(values, prefill, conflicts, status, case_keys=all_keys)
    if errors:
        return case, False, errors

    now = utcnow_iso()
    changed = False
    # Explicit conflict picks are always recorded (even when the picked
    # value equals the current best): the new row marks the resolution
    # time, which is what evidence arriving later is measured against.
    chosen = {k for k in conflicts if (values.get(f"choice_{k}") or "").strip()}
    # Explicit confirmation for manual fields (e.g. operator confirming an unchanged candidate)
    explicit_confirm = {
        k for k in all_keys
        if (values.get(f"confirm_{k}") or "").strip().lower() in ("1", "true", "yes", "on")
        or (values.get(f"choice_{k}") or "").strip()
    }
    try:
        for key in all_keys:
            if key == "application_no":
                continue
            new_val = cleaned.get(key, "")
            best_row = info.get(key, {}).get("best")
            is_already_manual = ((best_row or {}).get("method") == "manual")
            is_explicit_confirm = (key in explicit_confirm and bool(new_val) and not is_already_manual)
            cur_val = current_best.get(key) or ""
            if (new_val != cur_val and (new_val != "" or cur_val != "")) or key in chosen or is_explicit_confirm:
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

        # Customer reusable profile sync (Phase 2):
        if case["customer_id"]:
            cat = get_catalog()
            for key in all_keys:
                if cat.is_customer_scoped(key):
                    val_to_sync = None
                    if key in ("full_name", "father_name", "dob") and cleaned.get(key):
                        val_to_sync = cleaned.get(key)
                    else:
                        latest_row = conn.execute(
                            """SELECT value, method FROM extracted_fields
                               WHERE case_id = ? AND field_key = ? ORDER BY id DESC LIMIT 1""",
                            (case["id"], key),
                        ).fetchone()
                        if latest_row and latest_row["method"] == "manual":
                            val_to_sync = latest_row["value"]
                    if val_to_sync is not None:
                        upsert_customer_field(
                            conn,
                            customer_id=case["customer_id"],
                            field_key=key,
                            value=val_to_sync,
                            source_case_id=case["id"],
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


def categorized_fields(info: dict) -> list[tuple[str, str, list[str]]]:
    """Group field keys in info into catalogue categories."""
    cat = get_catalog()
    groups: dict[str, list[str]] = {cat_id: [] for cat_id, _ in CATEGORIES}

    for key in info:
        c = cat.get_category(key)
        if c not in groups:
            c = "other"
        groups[c].append(key)

    result = []
    for cat_id, cat_title in CATEGORIES:
        keys_in_cat = groups.get(cat_id, [])
        if keys_in_cat:
            result.append((cat_id, cat_title, keys_in_cat))
    return result


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
