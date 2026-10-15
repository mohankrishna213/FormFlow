"""Local-only benchmark reference verifier (Phase 3).

Purpose: the benchmark reference (e.g. ``data/benchmark/ref_ll.json``) may
be agent-transcribed and MUST NOT be trusted implicitly. This module
provides:

  * :func:`verify_reference` -- structural/format checks that print NO
    applicant values (presence, normalized length, value shape, optional
    value hashes for out-of-band comparison). It cannot prove a value is
    correct against a document; only the operator can do that locally.
    A key **absent entirely** is a schema error; a key present with a
    ``null``/empty value means the document does not carry that field
    (allowed; listed as ``null_keys``; does not block verification).
  * :func:`verification_status` -- reads the optional sidecar
    ``<reference>.verification.json`` recording that an operator confirmed
    the values against the original document; reports
    ``operator-verified`` / ``stale`` / ``unverified``.
  * :func:`record_verification` -- called (CLI, explicit flags) after the
    operator has done that comparison; stores ONLY hashes/shapes, never
    values.
  * CLI ``python -m app.services.reference_verify --reference <path>``.

Privacy contract: reports contain no field values, no substrings of
values, and no raw OCR text. ``--show-hashes`` is an explicit opt-in for
local inspection (hashes of short personal values are sensitive).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .benchmark import CRITICAL_FIELDS, normalize_field
from .fields import (
    extract_name_sequence,
    parse_application_no,
    parse_date_token,
    parse_licence_no,
)

_VERIFIER_VERSION = "phase3-2"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _shape_and_check(field_key: str, value: str) -> tuple[str, bool]:
    """Return (shape description without values, structure_ok)."""
    if field_key in ("full_name", "father_name"):
        words = re.findall(r"\S+", value)
        ok = extract_name_sequence(value, max_words=6) is not None
        return f"name-words:{len(words)}", ok
    if field_key == "dob":
        parsed = parse_date_token(value)
        if parsed is None:
            return "unparsed-date", False
        return ("iso-date" if not parsed[1] else "ambiguous-date"), not parsed[1]
    if field_key == "licence_no":
        return f"code-chars:{len(value)}", parse_licence_no(value) is not None
    if field_key == "application_no":
        return f"digit-chars:{len(value)}", parse_application_no(value) is not None
    if field_key == "address":
        return f"text-chars:{len(value)}", len(value.strip()) >= 5
    return f"text-chars:{len(value)}", True


def _sidecar_path(ref_path: Path) -> Path:
    return Path(str(ref_path) + ".verification.json")


def _write_guard(path: Path) -> None:
    """Refuse sidecar writes inside the repo but outside gitignored data/."""
    from ..config import REPO_ROOT

    resolved = path.resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return  # outside repo: allowed
    try:
        resolved.relative_to((REPO_ROOT / "data").resolve())
    except ValueError:
        raise ValueError(
            f"refusing to write verification sidecar inside repo but "
            f"outside gitignored data/: {path}")


def verification_status(ref_path: Path) -> dict[str, Any]:
    """Read the operator-verification sidecar; never invents a status."""
    ref_path = Path(ref_path)
    sidecar = _sidecar_path(ref_path)
    out: dict[str, Any] = {
        "status": "unverified",
        "operator": None,
        "verified_at": None,
        "sidecar": sidecar.name,
    }
    if not sidecar.exists():
        return out
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        out["status"] = "unverified"
        out["note"] = "sidecar unreadable; treating as unverified"
        return out
    current = _sha256_file(ref_path)
    if data.get("ref_sha256") != current:
        out["status"] = "stale"
        out["note"] = "reference file changed after verification; re-verify"
        return out
    out["status"] = "operator-verified"
    out["operator"] = data.get("operator")
    out["verified_at"] = data.get("verified_at")
    return out


def verify_reference(ref_path: Path, show_hashes: bool = False) -> dict[str, Any]:
    """Redacted structural report. Raises ValueError if the file is unusable."""
    ref_path = Path(ref_path)
    raw_bytes = ref_path.read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("reference JSON must be an object")

    present = [k for k in CRITICAL_FIELDS if k in raw and str(raw.get(k) or "").strip()]
    # A key absent entirely is a schema error; a key present with a
    # null/empty value means the document does not carry that field --
    # that is legitimate and must not block operator verification.
    missing = [k for k in CRITICAL_FIELDS if k not in raw]
    null_keys = [k for k in CRITICAL_FIELDS
                 if k in raw and not str(raw.get(k) or "").strip()]
    unknown = sorted(k for k in raw if k not in CRITICAL_FIELDS)
    types_ok = all(
        raw.get(k) is None or isinstance(raw.get(k), str)
        for k in CRITICAL_FIELDS if k in raw
    )

    fields: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    normalized: dict[str, str] = {}
    for k in CRITICAL_FIELDS:
        v = raw.get(k)
        value = "" if v is None else str(v)
        is_present = value.strip() != ""
        entry: dict[str, Any] = {
            "present": is_present,
            "normalized_len": 0,
            "shape": None,
            "structure_ok": None,
            "sha256_12": None,
        }
        if is_present:
            norm = normalize_field(k, value)
            normalized[k] = norm
            shape, ok = _shape_and_check(k, value)
            entry.update(normalized_len=len(norm), shape=shape, structure_ok=ok)
            if show_hashes:
                entry["sha256_12"] = _sha256_text(norm)[:12]
            if not ok:
                warnings.append(f"{k}: value shape does not match its expected format")
        fields[k] = entry

    # Duplicate detection across fields (names only; never echoes values).
    seen: dict[str, str] = {}
    for k, norm in normalized.items():
        if not norm:
            continue
        if norm in seen:
            warnings.append(f"fields {seen[norm]} and {k} normalize to the same value")
        else:
            seen[norm] = k
    if unknown:
        warnings.append(
            "unknown keys are ignored by the harness: " + ", ".join(unknown))
    if not types_ok:
        warnings.append("one or more field values are neither string nor null")

    report = {
        "tool": f"FormFlow reference verifier {_VERIFIER_VERSION}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reference": {
            "name": ref_path.name,
            "bytes": len(raw_bytes),
            "sha256_12": hashlib.sha256(raw_bytes).hexdigest()[:12],
        },
        "schema": {
            "all_six_keys_present": not missing,
            "missing_keys": missing,
            "null_keys": null_keys,
            "unknown_keys": unknown,
            "value_types_ok": types_ok,
        },
        "fields": fields,
        "warnings": warnings,
        "verification": {
            "status": "structural-check-only",
            "note": ("Structural checks cannot confirm correctness. Compare "
                     "each value against the original document locally, then "
                     "run --confirm-verified --operator NAME to record it."),
        },
        "human_verification": verification_status(ref_path),
    }
    return report


def record_verification(ref_path: Path, operator: str) -> Path:
    """Record operator verification in the sidecar (hashes/shapes only)."""
    operator = (operator or "").strip()
    if not operator:
        raise ValueError("an operator name is required to record verification")
    ref_path = Path(ref_path)
    report = verify_reference(ref_path)
    if report["schema"]["missing_keys"]:
        raise ValueError(
            "cannot record verification: reference is missing required keys: "
            + ", ".join(report["schema"]["missing_keys"]))
    if not report["schema"]["value_types_ok"]:
        raise ValueError("cannot record verification: invalid value types")
    sidecar = _sidecar_path(ref_path)
    _write_guard(sidecar)
    payload = {
        "tool": f"FormFlow reference verifier {_VERIFIER_VERSION}",
        "operator": operator,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ref_sha256": _sha256_file(ref_path),
        "statement": (
            "Operator states the six field values in this reference were "
            "compared against the original document on this machine."),
        "fields_checked": {
            k: {"present": v["present"], "shape": v["shape"]}
            for k, v in report["fields"].items()
        },
    }
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(sidecar)
    return sidecar


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Local-only structural verification for a FormFlow "
                    "benchmark reference JSON. Never prints field values "
                    "(opt-in hashes excluded).")
    ap.add_argument("--reference", required=True)
    ap.add_argument("--show-hashes", action="store_true",
                    help="include 12-hex hashes of normalized values "
                         "(sensitive; local inspection only)")
    ap.add_argument("--status", action="store_true",
                    help="only print the current operator-verification status")
    ap.add_argument("--confirm-verified", action="store_true",
                    help="record that YOU compared the values against the "
                         "original document (requires --operator)")
    ap.add_argument("--operator", default=None,
                    help="operator name for --confirm-verified (recorded in "
                         "the local sidecar)")
    args = ap.parse_args(argv)

    ref = Path(args.reference)
    if not ref.is_file():
        print(f"reference not found: {ref}", file=sys.stderr)
        return 2
    if args.status and not args.confirm_verified:
        print(json.dumps(verification_status(ref), indent=2))
        return 0
    try:
        report = verify_reference(ref, show_hashes=args.show_hashes)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"reference invalid: {e}", file=sys.stderr)
        return 2
    if args.confirm_verified:
        if not args.operator:
            print("--confirm-verified requires --operator NAME", file=sys.stderr)
            return 2
        try:
            sidecar = record_verification(ref, args.operator)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"verification recorded locally: {sidecar}", file=sys.stderr)
        report = verify_reference(ref, show_hashes=args.show_hashes)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["human_verification"]["status"] != "operator-verified":
        print("NOTE: no operator verification on record; benchmark results "
              "are conditional on the transcribed reference.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
