"""Phase 1 benchmark harness tests (synthetic data only, no PII).

Validates reproducible baseline metrics, redaction (no raw text/values
in outputs), and that text-PDF extraction is preserved.
"""
import json

from app.config import load_config
from app.services.benchmark import (
    CRITICAL_FIELDS,
    benchmark_file,
    collect_provenance,
    load_reference,
    normalize_field,
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

CLEAN_REF = {"full_name": "Ravi Kumar", "dob": "1995-08-17",
             "licence_no": "TG 1234567890", "application_no": None,
             "address": "12 MG Road, Bengaluru 560001"}


def test_exact_match_clean_labels(tmp_cfg):
    r = score_pages(CLEAN_PAGES, rules(tmp_cfg), CLEAN_REF,
                    latency_sec=0.01, case_id="clean")
    assert r["failed"] is False
    assert r["fields"]["full_name"]["exact_match"] is True
    assert r["fields"]["dob"]["exact_match"] is True
    assert r["fields"]["licence_no"]["exact_match"] is True
    assert r["fields"]["address"]["exact_match"] is True
    # application_no has no extractor: gap recorded, not guessed
    assert r["fields"]["application_no"]["predicted_present"] is False
    assert r["fields"]["application_no"]["exact_match"] is False


def test_missing_fields_no_false_positives(tmp_cfg):
    pages = [{"page": 1, "method": "text", "text": "Hello world, no labels here\n"}]
    ref = {k: None for k in CRITICAL_FIELDS}
    r = score_pages(pages, rules(tmp_cfg), ref, case_id="empty")
    assert all(not v["predicted_present"] for v in r["fields"].values())
    assert all(not v["false_positive"] for v in r["fields"].values())


def test_ambiguous_date_flagged_not_exact(tmp_cfg):
    pages = [{"page": 1, "method": "text",
              "text": "Name: Ravi Kumar\nDate of Birth: 05/06/2020\n"}]
    ref = {"full_name": "Ravi Kumar", "dob": "2020-06-05",
           "licence_no": None, "application_no": None, "address": None}
    r = score_pages(pages, rules(tmp_cfg), ref, case_id="ambig")
    dob = r["fields"]["dob"]
    # Ambiguous DD/MM keeps RAW token at low confidence: must not count
    # as an ISO exact match when the reference is normalized ISO.
    assert dob["predicted_present"] is True
    assert dob["exact_match"] is False
    assert dob["low_confidence"] is True


def test_conflicting_values_flagged(tmp_cfg):
    pages = [{"page": 1, "method": "text",
              "text": "Name: Ravi Kumar\nDOB: 17-08-1995\nDate of Birth: 13-06-1999\n"}]
    ref = {"full_name": "Ravi Kumar", "dob": "1995-08-17",
           "licence_no": None, "application_no": None, "address": None}
    r = score_pages(pages, rules(tmp_cfg), ref, case_id="conflict")
    assert r["fields"]["dob"]["has_conflict"] is True
    assert r["fields"]["dob"]["num_candidates"] == 2
    # One candidate matches, so exact_match is True but conflict stays visible.
    assert r["fields"]["dob"]["exact_match"] is True


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_outputs_never_contain_raw_values_or_text(tmp_cfg):
    r = score_pages(CLEAN_PAGES, rules(tmp_cfg), CLEAN_REF, case_id="redact")
    blob = json.dumps(r, ensure_ascii=False)
    for secret in ["Ravi Kumar", "1995-08-17", "TG 1234567890", "MG Road",
                   "Name: Ravi Kumar", "Date of Birth:"]:
        assert secret not in blob
    # Forbidden KEYS must never appear (method names like "text:labeled"
    # and "expected_len" are allowed; only exact keys are banned).
    assert set(_keys(r)).isdisjoint({"value", "text", "expected", "predicted", "raw"})
    s = summarize([r])
    assert "Ravi Kumar" not in json.dumps(s)


def test_provenance_records_config_without_text(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    prov = collect_provenance(cfg)
    assert prov["backend"] == "tesseract"
    assert prov["preprocess_variant"] == "baseline"
    assert prov["ocr_rules"]["psm"] == 6
    assert "tesseract" in prov
    blob = json.dumps(prov)
    assert "Ravi" not in blob


def test_summarize_math_and_latency(tmp_cfg):
    a = score_pages(CLEAN_PAGES, rules(tmp_cfg), CLEAN_REF,
                    latency_sec=0.1, case_id="a")
    # b: expected name differs from predicted -> false positive + miss.
    pages = [{"page": 1, "method": "text", "text": "Name: Ravi Kumar\n"}]
    ref = {k: None for k in CRITICAL_FIELDS}
    ref["full_name"] = "Someone Else"
    b = score_pages(pages, rules(tmp_cfg), ref, latency_sec=0.3, case_id="b")
    assert b["fields"]["full_name"]["predicted_present"] is True
    assert b["fields"]["full_name"]["exact_match"] is False
    assert b["fields"]["full_name"]["false_positive"] is True
    s = summarize([a, b])
    assert s["num_cases"] == 2
    assert s["failures"] == 0
    assert s["backend_failure_rate"] == 0.0
    assert s["per_field"]["full_name"]["exact_match"] == 1
    assert s["per_field"]["full_name"]["false_positives"] == 1
    assert s["latency_sec"]["mean"] >= 0.0
    assert s["latency_sec"]["p95"] >= s["latency_sec"]["mean"] - 0.001


def test_benchmark_file_failure_counted_not_raised(tmp_cfg):
    cfg = make_cfg(tmp_cfg, tesseract_cmd="tesseract-definitely-missing-xyz")
    from PIL import Image

    src = cfg.data_dir / "scan.png"
    Image.new("RGB", (300, 100), (255, 255, 255)).save(src, "PNG")
    ref = {k: None for k in CRITICAL_FIELDS}
    import app.services.ocr as ocr_mod

    orig = ocr_mod._COMMON_WINDOWS_PATHS
    ocr_mod._COMMON_WINDOWS_PATHS = ()
    try:
        r = benchmark_file(src, ref, cfg, case_id="missing-bin")
    finally:
        ocr_mod._COMMON_WINDOWS_PATHS = orig
    assert r["failed"] is True
    assert r["failure_kind"] == "unavailable"
    s = summarize([r])
    assert s["backend_failure_rate"] == 1.0


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

    r = benchmark_file(src, ref, cfg, ocr_fn=_boom, case_id="textpdf")
    assert r["failed"] is False
    assert r["page_methods"] == ["text"]
    assert r["fields"]["full_name"]["exact_match"] is True


def test_normalize_helpers_documented():
    assert normalize_field("full_name", "  RAVI  kumar ") == "ravi kumar"
    assert normalize_field("licence_no", "tg  1234567890 ") == "TG 1234567890"
    assert normalize_field("application_no", "ab-123 ") == "AB-123"


def test_load_reference_rejects_non_object(tmp_path):
    p = tmp_path / "ref.json"
    p.write_text("[1,2]", encoding="utf-8")
    try:
        load_reference(p)
        raise AssertionError("should have raised")
    except ValueError:
        pass
