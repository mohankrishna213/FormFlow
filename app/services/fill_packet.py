"""Fill-packet service (Phase 6).

Builds a service-specific field packet from a reviewed case.  The packet
contains only operator-confirmed values that are eligible for the selected
workflow.  It is the data contract consumed by the future Chrome extension
(Phase 7) and by the ``Ready to fill`` Flask view.

Key design decisions:

- **Workflow allowlist**: ``WORKFLOWS`` defines which fields each service
  type includes.  The first supported workflow is ``dl_application``
  (driving licence application).
- **Eligibility**: A field is eligible only when its value is confirmed
  (manual row or ``cases.appl_number``), the case is reviewed, the review
  version is current, and no unresolved/fresh conflicts block it.
- **Missing optional fields**: Never fabricated; they appear in the packet
  with ``eligible_to_fill: false`` and a reason.
- **OCR candidates**: Never exposed.  Only operator-confirmed values.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from ..db import get_customer_fields
from .review import (
    FIELD_LABELS,
    MANUAL_FIELDS,
    REQUIRED_FIELDS,
    REVIEW_FIELDS,
    best_and_candidates,
    conflict_status,
    key_conflicts,
)

# ---------------------------------------------------------------------------
# Service-specific workflow allowlists
# ---------------------------------------------------------------------------
# Each workflow defines the semantic field keys relevant to that service.
# Phase 6 does NOT define Sarathi selectors/IDs; those belong to Phase 7.
# If more workflows are needed, add entries here.

WORKFLOWS: dict[str, dict[str, Any]] = {
    "auto": {
        "label": "Auto-Detect / Flexible",
        "fields": ("full_name", "father_name", "dob", "address",
                   "licence_no", "application_no"),
    },
    "dl_application": {
        "label": "Driving Licence Application",
        "fields": ("full_name", "father_name", "dob", "address",
                   "licence_no", "application_no"),
    },
    "app_status": {
        "label": "Application Status",
        "fields": ("application_no", "dob"),
    },
}

DEFAULT_WORKFLOW = "dl_application"


class PacketError:
    """Structured rejection — no applicant data leaked in error messages."""

    def __init__(self, reason: str, http_status: int = 400):
        self.reason = reason
        self.http_status = http_status

    def to_dict(self) -> dict:
        return {"error": self.reason}


def _confirmed_value(
    conn: sqlite3.Connection,
    case: sqlite3.Row,
    info: dict,
    field_key: str,
) -> tuple[str | None, str | None]:
    """Return (value, reason) for *field_key*.

    Only operator-confirmed values are eligible:
    - For application_no: the official confirmed value in cases.appl_number.
    - For validated identity fields (full_name, father_name, dob): on a
      reviewed case, the validated value accepted by the operator during
      Validate & Save (whether manual edit or operator-verified OCR candidate).
    - For other fields: recorded manual rows (method == 'manual') or customer
      reusable profile values (method == 'customer_profile').

    If unconfirmed or missing, value is None with an explanatory reason.
    """
    if field_key == "application_no":
        val = (case["appl_number"] or "").strip()
        if val:
            return val, None
        return None, "Value not available"

    best = info.get(field_key, {}).get("best")
    if not best:
        # Check customer profile fallback if case has customer_id
        if case["customer_id"]:
            c_fields = get_customer_fields(conn, case["customer_id"])
            if field_key in c_fields and c_fields[field_key]["value"]:
                return c_fields[field_key]["value"].strip(), None
        return None, "Value not available"

    is_confirmed = False
    if (best.get("method") or "") in ("manual", "customer_profile"):
        is_confirmed = True
    elif (
        field_key in ("full_name", "father_name", "dob")
        and case["status"] == "reviewed"
        and case["reviewed_at"]
    ):
        is_confirmed = True

    if not is_confirmed:
        return None, "Value not confirmed by operator"

    val = (best.get("value") or "").strip()
    if val:
        return val, None
    return None, "Value not available"


def detect_case_licence_type(conn: sqlite3.Connection, case_pk: int) -> str:
    """Determine whether the source document/evidence for licence_no is LL, DL, or unknown.

    Checks evidence in order of reliability:
    1. Candidate rows in extracted_fields for field_key='licence_no' with method containing ':ll' or ':dl'
       (or ending in '-ll'/'-dl', or manual tagging).
    2. Attached documents: inspect OCR sidecar text if available for 'FORM 3' / 'LEARNER' vs 'FORM 7' / 'DRIVING'.
    3. Attached documents: inspect filenames for 'dl' / 'driving' vs 'll' / 'learner' / 'learning'.
    4. Return 'unknown' if no evidence exists (or ambiguous).
    Never guesses from the licence number string format alone.
    """
    # 1. Extracted candidate rows for licence_no on this case
    rows = conn.execute(
        """SELECT method FROM extracted_fields
           WHERE case_id = ? AND field_key = 'licence_no'
           ORDER BY id DESC""",
        (case_pk,),
    ).fetchall()
    methods = [str(r["method"] or "").lower() for r in rows]
    has_ll_method = any(":ll" in m or m.endswith("-ll") or m == "ll" or "manual:ll" in m for m in methods)
    has_dl_method = any(":dl" in m or m.endswith("-dl") or m == "dl" or "manual:dl" in m for m in methods)
    if has_ll_method and not has_dl_method:
        return "ll"
    if has_dl_method and not has_ll_method:
        return "dl"

    # 2. Attached documents
    doc_rows = conn.execute(
        "SELECT id, orig_filename, orig_path, doctype FROM documents WHERE case_id = ? ORDER BY id",
        (case_pk,),
    ).fetchall()

    # 2a. OCR sidecar text inspection
    for d in doc_rows:
        doc_id = d["id"]
        sidecar_candidates = [
            Path("data/ocr") / f"doc_{doc_id}.json",
        ]
        if d["orig_path"]:
            try:
                sidecar_candidates.append(Path(d["orig_path"]).parent.parent / "ocr" / f"doc_{doc_id}.json")
            except Exception:
                pass
        for sp in sidecar_candidates:
            if sp.exists():
                try:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    pages = data.get("pages", [])
                    text = " ".join(p.get("text", "") for p in pages).lower()
                    is_ll = bool(re.search(r"\bform[\s\-]*3\b|\blearner['’]?s?\s*licen[cs]e|\blearning\s*licen[cs]e", text))
                    is_dl = bool(re.search(r"\bform[\s\-]*7\b|\bdriving\s*licen[cs]e", text))
                    if is_ll and not is_dl:
                        return "ll"
                    if is_dl and not is_ll:
                        return "dl"
                except Exception:
                    pass

    # 2b. Document filenames
    ll_hits = 0
    dl_hits = 0
    for d in doc_rows:
        fname = (d["orig_filename"] or "").lower()
        toks = set(re.findall(r"[a-z0-9]+", fname))
        if toks & {"ll", "learner", "learners", "learning"}:
            ll_hits += 1
        if toks & {"dl", "driving"}:
            dl_hits += 1

    if ll_hits > 0 and dl_hits == 0:
        return "ll"
    if dl_hits > 0 and ll_hits == 0:
        return "dl"

    # 3. Case ID clue (e.g. CASE-...-LL1 or CASE-...-DL1)
    case_row = conn.execute("SELECT case_id FROM cases WHERE id = ?", (case_pk,)).fetchone()
    if case_row and case_row["case_id"]:
        cid = case_row["case_id"].lower()
        if re.search(r"[-_]ll(?:\d+|[-_]|$)", cid):
            return "ll"
        if re.search(r"[-_]dl(?:\d+|[-_]|$)", cid):
            return "dl"

    return "unknown"


def _field_entry(
    field_key: str,
    value: str | None,
    eligible: bool,
    reason: str | None = None,
    licence_type: str | None = None,
) -> dict:
    label = FIELD_LABELS.get(field_key, field_key)
    if field_key == "licence_no" and value:
        if licence_type == "dl":
            label = "Driving Licence Number"
        elif licence_type == "ll":
            label = "Learner's Licence Number"
        elif licence_type == "unknown":
            label = "Licence Number"
    entry: dict[str, Any] = {
        "key": field_key,
        "label": label,
        "value": value,
        "eligible_to_fill": eligible,
    }
    if reason:
        entry["reason"] = reason
    return entry


def build_fill_packet(
    conn: sqlite3.Connection,
    case_id: str,
    workflow: str = DEFAULT_WORKFLOW,
) -> dict | PacketError:
    """Build a fill packet or return a ``PacketError``.

    The caller (API route or template view) decides how to present the
    result.  This function never raises for expected conditions.
    """
    # --- workflow validation ---
    wf = WORKFLOWS.get(workflow)
    if wf is None:
        return PacketError("invalid_workflow", 400)

    # --- case lookup ---
    case = conn.execute(
        "SELECT * FROM cases WHERE case_id = ?", (case_id,)
    ).fetchone()
    if case is None:
        return PacketError("case_not_found", 404)

    # --- reviewed-state enforcement ---
    if case["status"] != "reviewed":
        return PacketError("case_not_reviewed", 409)
    if not case["reviewed_at"]:
        return PacketError("review_metadata_missing", 409)

    # --- conflict and freshness analysis ---
    info = best_and_candidates(conn, case["id"])
    conflicts = key_conflicts(conn, case["id"])
    c_status = conflict_status(conn, case, info, conflicts)

    # Any unresolved or fresh conflict blocks the entire packet.
    for key, st in c_status.items():
        if st.get("need_pick"):
            return PacketError("unresolved_conflicts", 409)

    # --- source licence type detection (Phase 7C) ---
    lic_type = detect_case_licence_type(conn, case["id"])

    # --- build per-field entries ---
    if workflow == "auto":
        # Base standard fields
        allowed_keys = list(wf["fields"])
        seen_keys = set(allowed_keys)
        # Add all other confirmed eligible fields from case info
        for k in info:
            if k not in seen_keys:
                val, _ = _confirmed_value(conn, case, info, k)
                if val:
                    allowed_keys.append(k)
                    seen_keys.add(k)
        if case["customer_id"]:
            c_fields = get_customer_fields(conn, case["customer_id"])
            for k in c_fields:
                if k not in seen_keys and c_fields[k]["value"]:
                    allowed_keys.append(k)
                    seen_keys.add(k)
    else:
        allowed_keys = list(wf["fields"])

    fields: list[dict] = []
    for fk in allowed_keys:
        value, reason = _confirmed_value(conn, case, info, fk)

        # Eligibility: value present + no fresh conflict
        if value:
            fields.append(_field_entry(fk, value, eligible=True,
                                      licence_type=lic_type if fk == "licence_no" else None))
        else:
            if fk in c_status and c_status[fk].get("need_pick"):
                reason = "Unresolved conflict"
            fields.append(_field_entry(fk, None, eligible=False,
                                      reason=reason or "Value not available",
                                      licence_type=lic_type if fk == "licence_no" else None))

    return {
        "case_id": case["case_id"],
        "workflow": workflow,
        "workflow_label": wf["label"],
        "review_version": case["review_version"],
        "reviewed_at": case["reviewed_at"],
        "fields": fields,
    }
