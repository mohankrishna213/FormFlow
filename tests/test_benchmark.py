"""Phase 1 benchmark tests (synthetic data only, no PII).

Covers corrected metrics (recall/precision/coverage denominators),
redaction, truthful backend labels, missing fields, extra/conflicting
candidates, backend-failure-as-miss, opaque IDs, out-dir guard, and
OCR-vs-parser stage separation.
"""
import json

import pytest

from app.config import load_config
from app.services.benchmark import (
    CRITICAL_FIELDS,
    benchmark_file,
    collect_provenance,
    compare_variants,
    load_reference,
    normalize_field,
    ocr_stage_diagnostics,
    resolve_out_dir,
    score_pages,
    summarize,
)
from app.services.fields import load_field_rules


def make_cfg(tmp_cfg, **kw):
    d = dict(tmp_cfg)
    d.update(kw)
    cfg = load_config(d)
    cfg.ensure_data_dirs()
    return cfg


def rules(tmp_cfg):
    return load_field_rules(make_cfg(tmp_cfg))


CLEAN_PAGES = [{"page": 1, "method": "text", "text":
                "Name: Ravi Kumar\n"
                "Date of Birth: 17-08-1995\n"
                "Licence No: TG 1234567890\n"
                "Address: 12 MG Road\nBengaluru 560001\n"}]

CLEAN_REF = {"full_name": "Ravi Kumar", "father_name": None,
             "dob": "1995-08-17", "licence_no": "TG 1234567890",
             "application_no": None,
             "address": "12 MG Road, Bengaluru 560001"}


def test_critical_fields_include_gaps():
    assert set(CRITICAL_FIELDS) == {
        "full_name", "father_name", "dob", "licence_no",
        "application_no", "address"}


def test_exact_match_clean_labels(tmp_cfg):
    r = score_pages(CLEAN_PAGES, rules(tmp_cfg), CLEAN_REF,
                    latency_sec=0.01, case_id="synthetic-01")
    assert r["failed"] is False
    assert r["backend"] == "tesseract"
    assert r["preprocess_variant"] == "baseline"
    assert r["fields"]["full_name"]["exact_match"] is True
    assert r["fields"]["dob"]["exact_match"] is True
    assert r["fields"]["licence_no"]["exact_match"] is True
    assert r["fields"]["address"]["exact_match"] is True
    # Fields absent from the OCR text stay unpredicted (no invention).
    for absent in ("father_name", "application_no"):
        assert r["fields"][absent]["predicted_present"] is False
        assert r["fields"][absent]["exact_match"] is False
        assert r["fields"][absent]["false_positive"] is False


def test_missing_reference_fields_no_false_positives(tmp_cfg):
    pages = [{"page": 1, "method": "text", "text": "Hello world, no labels here\n"}]
    ref = {k: None for k in CRITICAL_FIELDS}
    r = score_pages(pages, rules(tmp_cfg), ref, case_id="synthetic-02")
    assert all(not v["predicted_present"] for v in r["fields"].values())
    assert all(not v["false_positive"] for v in r["fields"].values())
    assert all(not v["exact_match"] for v in r["fields"].values())
    s = summarize([r])
    for k in CRITICAL_FIELDS:
        assert s["per_field"][k]["recall"] is None  # E == 0
        assert s["per_field"][k]["precision_cases_clean"] is None  # P == 0


def test_ambiguous_date_flagged_not_exact(tmp_cfg):
    pages = [{"page": 1, "method": "text",
              "text": "Name: Ravi Kumar\nDate of Birth: 05/06/2020\n"}]
    ref = {"full_name": "Ravi Kumar", "father_name": None,
           "dob": "2020-06-05", "licence_no": None,
           "application_no": None, "address": None}
    r = score_pages(pages, rules(tmp_cfg), ref, case_id="synthetic-03")
    dob = r["fields"]["dob"]
    assert dob["predicted_present"] is True
    assert dob["exact_match"] is False
    assert dob["low_confidence"] is True
    assert dob["false_positive"] is True


def test_extra_candidate_counts_as_false_positive_despite_match(tmp_cfg):
    pages = [{"page": 1, "method": "text",
              "text": "Name: Ravi Kumar\nDOB: 17-08-1995\nDate of Birth: 13-06-1999\n"}]
    ref = {"full_name": "Ravi Kumar", "father_name": None,
           "dob": "1995-08-17", "licence_no": None,
           "application_no": None, "address": None}
    r = score_pages(pages, rules(tmp_cfg), ref, case_id="synthetic-04")
    dob = r["fields"]["dob"]
    assert dob["num_candidates"] == 2
    assert dob["num_matching"] == 1
    assert dob["num_nonmatching"] == 1
    assert dob["has_conflict"] is True
    assert dob["extra_incorrect"] is True
    assert dob["exact_match"] is True  # found, but...
    assert dob["false_positive"] is True  # ...spurious extra still counts
    s = summarize([r])
    # Recall 1 (found), case-level precision 0 (no clean hit among
    # 1 predicted case).
    assert s["per_field"]["dob"]["recall"] == 1.0
    assert s["per_field"]["dob"]["precision_cases_clean"] == 0.0
    assert s["per_field"]["dob"]["false_positives"] == 1


def test_spurious_on_expected_absent_hurts_precision_not_recall(tmp_cfg):
    pages = [{"page": 1, "method": "text", "text": "Name: Ravi Kumar\n"}]
    ref = {k: None for k in CRITICAL_FIELDS}
    ref["full_name"] = "Someone Else"
    r = score_pages(pages, rules(tmp_cfg), ref, case_id="synthetic-05")
    assert r["fields"]["full_name"]["false_positive"] is True
    s = summarize([r])
    assert s["per_field"]["full_name"]["recall"] == 0.0
    assert s["per_field"]["full_name"]["precision_cases_clean"] == 0.0


def test_coverage_never_exceeds_one(tmp_cfg):
    a = score_pages(CLEAN_PAGES, rules(tmp_cfg), CLEAN_REF,
                    latency_sec=0.1, case_id="synthetic-06")
    pages = [{"page": 1, "method": "text", "text": "Name: Ravi Kumar\n"}]
    ref = {k: None for k in CRITICAL_FIELDS}
    b = score_pages(pages, rules(tmp_cfg), ref, latency_sec=0.1,
                    case_id="synthetic-07")
    s = summarize([a, b])
    for k, v in s["per_field"].items():
        if v["coverage_on_expected"] is not None:
            assert 0.0 <= v["coverage_on_expected"] <= 1.0
    assert 0.0 <= (s["overall_coverage"] or 0.0) <= 1.0


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_outputs_never_contain_raw_values_or_text(tmp_cfg):
    r = score_pages(CLEAN_PAGES, rules(tmp_cfg), CLEAN_REF,
                    case_id="synthetic-08")
    blob = json.dumps(r, ensure_ascii=False)
    for secret in ["Ravi Kumar", "1995-08-17", "TG 1234567890", "MG Road",
                   "Name: Ravi Kumar", "Date of Birth:"]:
        assert secret not in blob
    assert set(_keys(r)).isdisjoint({"value", "text", "expected", "predicted", "raw"})
    # OCR stage carries counts/booleans only.
    assert "per_page" in r["ocr_stage"]
    assert "label_present" in r["ocr_stage"]
    assert set(_keys(r["ocr_stage"])).isdisjoint(
        {"value", "text", "expected", "predicted", "raw"})
    s = summarize([r])
    assert "Ravi Kumar" not in json.dumps(s)


def test_unsupported_backend_and_variant_rejected(tmp_cfg):
    # 'paddleocr' is a real opt-in backend label now; an engine that does
    # not exist must still be rejected instead of producing a fake run.
    with pytest.raises(ValueError, match="unsupported backend"):
        score_pages(CLEAN_PAGES, rules(tmp_cfg), CLEAN_REF,
                    backend="florence-2", case_id="x")
    with pytest.raises(ValueError, match="unsupported preprocess_variant"):
        score_pages(CLEAN_PAGES, rules(tmp_cfg), CLEAN_REF,
                    preprocess_variant="opencv", case_id="x")
    cfg = make_cfg(tmp_cfg)
    from PIL import Image

    src = cfg.data_dir / "scan.png"
    Image.new("RGB", (60, 30), (255, 255, 255)).save(src, "PNG")
    with pytest.raises(ValueError, match="unsupported backend"):
        benchmark_file(src, {k: None for k in CRITICAL_FIELDS}, cfg,
                       backend="florence-2")


def test_backend_failure_counts_as_miss_not_exclusion(tmp_cfg):
    cfg = make_cfg(tmp_cfg, tesseract_cmd="tesseract-definitely-missing-xyz")
    from PIL import Image

    src = cfg.data_dir / "scan2.png"
    Image.new("RGB", (300, 100), (255, 255, 255)).save(src, "PNG")
    ref = {"full_name": "Ravi Kumar", "father_name": None,
           "dob": "1995-08-17", "licence_no": None,
           "application_no": None, "address": None}
    import app.services.ocr as ocr_mod

    orig = ocr_mod._COMMON_WINDOWS_PATHS
    ocr_mod._COMMON_WINDOWS_PATHS = ()
    try:
        r = benchmark_file(src, ref, cfg)
    finally:
        ocr_mod._COMMON_WINDOWS_PATHS = orig
    assert r["failed"] is True
    assert r["failure_kind"] == "unavailable"
    # Expected fields retained so the failure is a miss.
    assert r["fields"]["full_name"]["expected_present"] is True
    assert r["fields"]["dob"]["expected_present"] is True
    assert r["fields"]["full_name"]["predicted_present"] is False
    assert r["fields"]["full_name"]["exact_match"] is False
    s = summarize([r])
    assert s["backend_failure_rate"] == 1.0
    assert s["per_field"]["full_name"]["recall"] == 0.0
    assert s["per_field"]["full_name"]["coverage_on_expected"] == 0.0


def test_case_ids_opaque_by_default(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    import pymupdf

    src = cfg.data_dir / "identifying_filename_xyz.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "nothing here")
    doc.save(src)
    doc.close()
    r = benchmark_file(src, {k: None for k in CRITICAL_FIELDS}, cfg)
    blob = json.dumps(r)
    assert "identifying_filename_xyz" not in blob
    assert r["case_id"].startswith("case-")
    assert ".pdf" not in r["case_id"]


def test_resolve_out_dir_guards(tmp_cfg):
    from app.config import REPO_ROOT

    ok = resolve_out_dir(REPO_ROOT / "data" / "benchmark")
    assert ok.name == "benchmark"
    outside = resolve_out_dir(tmp_cfg["data_dir"] / "out")
    assert outside.exists() is False  # resolved, not created
    with pytest.raises(ValueError, match="outside gitignored data"):
        resolve_out_dir(REPO_ROOT / "reports")


def test_ocr_vs_parser_diagnostics(tmp_cfg):
    r = rules(tmp_cfg)
    # Label present but value unparseable -> parser-level gap, not an
    # OCR-recognition gap (the label text WAS recognized).
    pages = [{"page": 1, "method": "ocr",
              "text": "Name: 12345\nFather's Name: ***\n"}]
    diag = ocr_stage_diagnostics(pages, r)
    assert diag["label_present"]["full_name"] is True
    assert diag["label_present"]["father_name"] is True
    scored = score_pages(pages, r, {k: None for k in CRITICAL_FIELDS},
                         case_id="synthetic-09")
    assert scored["fields"]["full_name"]["predicted_present"] is False
    assert scored["fields"]["father_name"]["predicted_present"] is False
    # Label absent -> OCR-level gap (nothing to anchor a parse to).
    pages2 = [{"page": 1, "method": "ocr", "text": "random words here\n"}]
    diag2 = ocr_stage_diagnostics(pages2, r)
    assert diag2["label_present"]["full_name"] is False
    assert diag2["label_present"]["application_no"] is False
    # Phase 3: all six critical fields have parsers AND configured
    # labels, so every diagnostic here is a real boolean, never None.
    for k in CRITICAL_FIELDS:
        assert diag["label_present"][k] is not None


def test_all_six_fields_from_synthetic_document(tmp_cfg):
    pages = [{"page": 1, "method": "text",
              "text": "Government of Telangana\n"
                      "Form 3\n"
                      "1. Name: Ravi Kumar Sharma\n"
                      "2. Father's Name: Suresh Kumar Sharma\n"
                      "3. Date of Birth: 17-08-1995\n"
                      "4. Licence No: TG 1234567890\n"
                      "5. App.No. 3542873326\n"
                      "6. Present Address: 12 MG Road, Hyderabad 500001\n"}]
    ref = {"full_name": "Ravi Kumar Sharma",
           "father_name": "Suresh Kumar Sharma",
           "dob": "1995-08-17", "licence_no": "TG 1234567890",
           "application_no": "3542873326",
           "address": "12 MG Road, Hyderabad 500001"}
    r = score_pages(pages, rules(tmp_cfg), ref, case_id="synthetic-12")
    for k in CRITICAL_FIELDS:
        assert r["fields"][k]["exact_match"] is True, k
        assert r["fields"][k]["false_positive"] is False, k
        assert r["fields"][k]["num_candidates"] == 1, k


def test_provenance_records_config_without_text(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    prov = collect_provenance(cfg)
    assert prov["backend"] == "tesseract"
    assert prov["preprocess_variant"] == "baseline"
    assert prov["ocr_rules"]["psm"] == 6
    # Phase 3: all six critical fields have parsers and configured
    # labels; the gap flags stay explicit for both conditions.
    snap = prov["ocr_rules"]["fields"]
    for k in CRITICAL_FIELDS:
        assert snap[k]["parser_missing"] is False, k
        assert snap[k]["labels_missing"] is False, k
        assert snap[k]["labels"], k
    assert prov["reference"] is None  # no reference path given
    assert isinstance(prov["paddleocr"]["available"], bool)
    assert "Ravi" not in json.dumps(prov)


def test_provenance_with_reference_path_records_hash_not_name(tmp_cfg, tmp_path):
    cfg = make_cfg(tmp_cfg)
    ref = tmp_path / "Applicant Ravi Kumar local reference.json"
    ref.write_text(json.dumps(CLEAN_REF), encoding="utf-8")
    prov = collect_provenance(cfg, reference_path=ref)
    info = prov["reference"]
    assert info["sha256_12"] and len(info["sha256_12"]) == 12
    assert info["verification"]["status"] == "unverified"
    blob = json.dumps(prov)
    assert "Applicant" not in blob and "Ravi" not in blob
    assert "local reference" not in blob


def test_compare_variants_records_reference_info(tmp_cfg, tmp_path):
    from PIL import Image as _Image

    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "doc3.png"
    _Image.new("RGB", (400, 300), (255, 255, 255)).save(src, "PNG")
    ref_file = tmp_path / "ref.json"
    ref_file.write_text(json.dumps({k: None for k in CRITICAL_FIELDS}),
                        encoding="utf-8")
    bundle = compare_variants(
        src, {k: None for k in CRITICAL_FIELDS}, cfg,
        variants=("baseline",), ocr_fn=lambda img: ("hello", 0.5),
        reference_path=ref_file)
    info = bundle["reference_info"]
    assert len(info["sha256_12"]) == 12
    assert info["verification"]["status"] == "unverified"
    assert (bundle["results"][0]["provenance"]["reference"]["sha256_12"]
            == info["sha256_12"])
    assert "ref.json" not in json.dumps(bundle)


def test_text_pdf_path_preserved_in_benchmark(tmp_cfg):
    import pymupdf

    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "t.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Name: Ravi Kumar")
    page.insert_text((72, 92), "Date of Birth: 17-08-1995")
    page.insert_text((72, 112), "Licence No: TG 1234567890")
    page.insert_text((72, 132), "Address: 12 MG Road")
    page.insert_text((72, 152), "Bengaluru 560001")
    doc.save(src)
    doc.close()
    ref = dict(CLEAN_REF)

    def _boom(img):
        raise AssertionError("OCR must not run for text PDFs")

    r = benchmark_file(src, ref, cfg, ocr_fn=_boom)
    assert r["failed"] is False
    assert [p["method"] for p in r["ocr_stage"]["per_page"]] == ["text"]
    assert r["fields"]["full_name"]["exact_match"] is True


def test_normalize_helpers_documented():
    assert normalize_field("full_name", "  RAVI  kumar ") == "ravi kumar"
    assert normalize_field("father_name", "  RAVI  kumar ") == "ravi kumar"
    assert normalize_field("licence_no", "tg  1234567890 ") == "TG 1234567890"
    assert normalize_field("application_no", "ab-123 ") == "AB-123"


def test_load_reference_rejects_non_object(tmp_path):
    p = tmp_path / "ref.json"
    p.write_text("[1,2]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_reference(p)


def test_overall_rates_use_direct_slot_counts(tmp_cfg):
    a = score_pages(CLEAN_PAGES, rules(tmp_cfg), CLEAN_REF,
                    latency_sec=0.1, case_id="synthetic-10")
    pages = [{"page": 1, "method": "text", "text": "Name: Ravi Kumar\n"}]
    ref = {k: None for k in CRITICAL_FIELDS}
    b = score_pages(pages, rules(tmp_cfg), ref, latency_sec=0.1,
                    case_id="synthetic-11")
    s = summarize([a, b])
    # Direct slot math: full_name E=1 (a only; b expects nothing for
    # most fields), coverage/precision keys are case-level.
    assert "precision_cases_clean" in s["per_field"]["full_name"]
    assert "precision" not in s["per_field"]["full_name"]
    assert "overall_precision_cases_clean" in s
    assert "overall_precision" not in s
    assert s["metric_definitions"]["precision_cases_clean"].startswith("CASE-LEVEL")
    exp_slots = sum(1 for r in (a, b) for k in CRITICAL_FIELDS
                    if r["fields"][k]["expected_present"])
    covered_slots = sum(1 for r in (a, b) for k in CRITICAL_FIELDS
                        if r["fields"][k]["expected_present"]
                        and r["fields"][k]["predicted_present"])
    assert s["overall_coverage"] == round(covered_slots / exp_slots, 4)
    assert 0.0 <= (s["overall_coverage"] or 0.0) <= 1.0


def test_diagnostic_bundle_filename_opaque_and_no_overwrite(tmp_path):
    from app.services.benchmark import write_diagnostic_bundle

    out = tmp_path / "out"
    out.mkdir()
    pages = [{"page": 1, "method": "text", "text": "Name: Ravi Kumar\n"}]
    b1 = write_diagnostic_bundle(out, "abc123hashseed", pages, [],
                                 None, case_id="Applicant Ravi Kumar")
    b2 = write_diagnostic_bundle(out, "abc123hashseed", pages, [],
                                 None, case_id="Applicant Ravi Kumar")
    assert b1 != b2  # no silent overwrite
    assert b1.exists() and b2.exists()
    for b in (b1, b2):
        assert "Ravi" not in b.name and "Applicant" not in b.name
        assert b.name.startswith("diagnostic-raw-")


def test_failure_hint_sanitizes_input_path(tmp_path):
    from app.services.benchmark import _sanitize_failure_hint

    fake_src = tmp_path / "Identifying Applicant Name.jpg"
    hint = _sanitize_failure_hint(
        f"cannot read file '{fake_src}' because it vanished\nretry", fake_src)
    assert "Identifying" not in hint
    assert "\n" not in hint
    assert len(hint) <= 200


def test_load_reference_keeps_six_fields(tmp_path):
    p = tmp_path / "ref.json"
    p.write_text(json.dumps({"full_name": "A B", "father_name": "C D",
                             "dob": "2000-01-01"}), encoding="utf-8")
    ref = load_reference(p)
    assert set(ref) == set(CRITICAL_FIELDS)
    assert ref["father_name"] == "C D"
    assert ref["application_no"] is None
