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

import sqlite3
from typing import Any

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
    "dl_application": {
        "label": "Driving Licence Application",
        "fields": ("full_name", "father_name", "dob", "address",
                   "licence_no", "application_no"),
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
    - For other fields: only recorded manual rows (method == 'manual').

    If unconfirmed or missing, value is None with an explanatory reason.
    """
    if field_key == "application_no":
        val = (case["appl_number"] or "").strip()
        if val:
            return val, None
        return None, "Value not available"

    best = info.get(field_key, {}).get("best")
    if not best:
        return None, "Value not available"

    is_confirmed = False
    if (best.get("method") or "") == "manual":
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


def _field_entry(
    field_key: str,
    value: str | None,
    eligible: bool,
    reason: str | None = None,
) -> dict:
    entry: dict[str, Any] = {
        "key": field_key,
        "label": FIELD_LABELS.get(field_key, field_key),
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

    # --- build per-field entries ---
    allowed_keys = wf["fields"]
    fields: list[dict] = []
    for fk in allowed_keys:
        if fk not in REVIEW_FIELDS:
            continue  # safety: ignore unknown keys in config

        value, reason = _confirmed_value(conn, case, info, fk)

        # Eligibility: value present + no fresh conflict
        if value:
            fields.append(_field_entry(fk, value, eligible=True))
        else:
            if fk in c_status and c_status[fk].get("need_pick"):
                reason = "Unresolved conflict"
            fields.append(_field_entry(fk, None, eligible=False, reason=reason or "Value not available"))

    return {
        "case_id": case["case_id"],
        "workflow": workflow,
        "workflow_label": wf["label"],
        "review_version": case["review_version"],
        "reviewed_at": case["reviewed_at"],
        "fields": fields,
    }
