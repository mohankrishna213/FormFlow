"""Phase 3 reference-verifier tests (synthetic values only; no real PII).

The verifier must never echo applicant values into reports or sidecars;
these tests pin that contract plus the stale/unverified status logic.
"""
import json

import pytest

from app.config import REPO_ROOT
from app.services.reference_verify import (
    _write_guard,
    main,
    record_verification,
    verification_status,
    verify_reference,
)

REF = {
    "full_name": "Ravi Kumar",
    "father_name": "Suresh Kumar",
    "dob": "1995-08-17",
    "licence_no": "TG 1234567890",
    "application_no": "3542873326",
    "address": "12 MG Road, Bengaluru 560001",
}


def write_ref(tmp_path, data=None):
    p = tmp_path / "ref_test.json"
    p.write_text(json.dumps(data if data is not None else REF), encoding="utf-8")
    return p


def test_verify_reference_redacted_structural_report(tmp_path):
    report = verify_reference(write_ref(tmp_path))
    assert report["schema"]["all_six_keys_present"] is True
    assert report["schema"]["missing_keys"] == []
    assert report["schema"]["value_types_ok"] is True
    assert report["warnings"] == []
    for k, entry in report["fields"].items():
        assert entry["present"] is True
        assert entry["structure_ok"] is True, k
        assert entry["normalized_len"] > 0
        assert entry["sha256_12"] is None  # opt-in only
    assert report["human_verification"]["status"] == "unverified"
    blob = json.dumps(report)
    for value in REF.values():
        assert value not in blob
    assert set(report["fields"]) == set(REF)


def test_verify_reference_flags_bad_shapes_without_values(tmp_path):
    bad = dict(REF)
    bad["dob"] = "not a date"
    bad["licence_no"] = "??"
    bad["application_no"] = "abc"
    report = verify_reference(write_ref(tmp_path, bad))
    assert report["fields"]["dob"]["structure_ok"] is False
    assert report["fields"]["dob"]["shape"] == "unparsed-date"
    assert report["fields"]["licence_no"]["structure_ok"] is False
    assert report["fields"]["application_no"]["structure_ok"] is False
    assert any(w.startswith("dob:") for w in report["warnings"])
    blob = json.dumps(report)
    for value in bad.values():
        assert value not in blob


def test_verify_reference_missing_and_unknown_keys(tmp_path):
    p = write_ref(tmp_path, {"full_name": "Ravi Kumar", "extra_key": "x"})
    report = verify_reference(p)
    assert report["schema"]["all_six_keys_present"] is False
    assert set(report["schema"]["missing_keys"]) == set(REF) - {"full_name"}
    assert report["schema"]["unknown_keys"] == ["extra_key"]
    assert "Ravi Kumar" not in json.dumps(report)


def test_record_verification_requires_operator(tmp_path):
    p = write_ref(tmp_path)
    with pytest.raises(ValueError):
        record_verification(p, "   ")
    with pytest.raises(ValueError, match="missing required keys"):
        record_verification(write_ref(tmp_path, {"full_name": "Ravi Kumar"}),
                            "operator-jane")


def test_record_verification_sidecar_and_status(tmp_path):
    p = write_ref(tmp_path)
    assert verification_status(p)["status"] == "unverified"
    sidecar = record_verification(p, "operator-jane")
    assert sidecar.exists()
    text = sidecar.read_text(encoding="utf-8")
    for value in REF.values():
        assert value not in text
    status = verification_status(p)
    assert status["status"] == "operator-verified"
    assert status["operator"] == "operator-jane"
    assert status["verified_at"]
    # Changing the reference after verification invalidates it.
    changed = dict(REF)
    changed["address"] = "9 Other Street, Mysuru 570001"
    p.write_text(json.dumps(changed), encoding="utf-8")
    stale = verification_status(p)
    assert stale["status"] == "stale"
    assert "re-verify" in stale["note"]


def test_write_guard_denies_repo_outside_data(tmp_path):
    with pytest.raises(ValueError, match="outside gitignored data"):
        _write_guard(REPO_ROOT / "reports" / "x.json.verification.json")
    _write_guard(tmp_path / "sidecar.json")  # outside repo: allowed


def test_main_status_and_confirm_flag(tmp_path, capsys):
    p = write_ref(tmp_path)
    assert main(["--reference", str(p), "--status"]) == 0
    assert "unverified" in capsys.readouterr().out
    # Recording requires an explicit operator name.
    assert main(["--reference", str(p), "--confirm-verified"]) == 2
    assert main(["--reference", str(p),
                 "--confirm-verified", "--operator", "operator-jane"]) == 0
    assert verification_status(p)["status"] == "operator-verified"
