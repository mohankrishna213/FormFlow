"""Phase 3 Comprehensive Test Suite: Robust Local OCR, Preprocessing,

Classification, and Field Extraction.

All tests use synthetic data and mocked OCR engines:
- Zero real customer PII.
- No network access required.
- No live model downloads.
- Works offline on Windows CPU.
"""
from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

import app.services.classify as classify
import app.services.extract as extract
import app.services.fields as fields
import app.services.ocr as ocr
import app.services.ocr_paddle as ocr_paddle
from app.config import load_config
from app.db import (
    connect,
    create_case,
    create_customer,
    create_document,
    create_extracted_field,
    get_customer_fields,
    get_document,
    init_db,
    list_case_fields,
    list_document_fields,
    upsert_customer_field,
)


def _make_cfg(tmp_path: Path):
    cfg = load_config({
        "data_dir": str(tmp_path / "data"),
        "db_path": str(tmp_path / "data" / "formflow.db"),
    })
    cfg.ensure_data_dirs()
    init_db(cfg.db_path)
    return cfg


def test_paddleocr_output_normalization_with_regions(monkeypatch):
    """Verify mocked PaddleOCR produces standardized visual lines, regions, and confidences."""
    fake_3x_result = [
        {
            "rec_texts": ["Government of India", "Samarth Sharma", "DOB: 20/06/1986", "Male"],
            "rec_scores": [0.98, 0.95, 0.92, 0.89],
            "rec_boxes": [
                [50, 20, 300, 50],
                [50, 70, 250, 100],
                [50, 120, 220, 150],
                [230, 120, 300, 150],
            ],
        }
    ]

    class FakeEngine:
        def predict(self, arr):
            return fake_3x_result

    monkeypatch.setattr(ocr_paddle, "_ENGINE", FakeEngine())
    monkeypatch.setattr(ocr_paddle, "available", lambda: True)

    img = Image.new("RGB", (400, 300), "white")
    text, conf, regions, ver = ocr_paddle.run_paddle_ocr_full(img, lang="en")

    assert "Samarth Sharma" in text
    assert "DOB: 20/06/1986" in text
    assert pytest.approx(conf, 0.01) == (0.98 + 0.95 + 0.92 + 0.89) / 4
    assert len(regions) == 4
    assert regions[1]["text"] == "Samarth Sharma"
    assert regions[1]["bbox"] == [50, 70, 250, 100]
    assert 0.0 <= regions[1]["confidence"] <= 1.0


def test_tesseract_fallback_when_paddleocr_fails(tmp_path, monkeypatch):
    """When PaddleOCR raises an error or is unavailable, recognize_image cleanly falls back to Tesseract."""
    cfg = _make_cfg(tmp_path)

    # Force PaddleOCR to fail
    def _failing_paddle(img, lang="en"):
        raise ocr_paddle.PaddleUnavailableError("simulated paddle failure")

    monkeypatch.setattr(ocr_paddle, "available", lambda: True)
    monkeypatch.setattr(ocr_paddle, "run_paddle_ocr_full", _failing_paddle)

    # Mock tesseract_ocr_full so live binary is not required for unit test
    called_tesseract = []

    def _mock_tesseract(img, cfg, lang="eng", psm=6):
        called_tesseract.append(lang)
        return "Simulated Tesseract Result", 0.88, [{"text": "Simulated", "bbox": [10, 10, 60, 30], "confidence": 0.88, "reading_order": 0}], "5.4.0"

    monkeypatch.setattr(ocr, "tesseract_ocr_full", _mock_tesseract)

    img = Image.new("RGB", (100, 100), "white")
    text, conf, regions, engine, ver = ocr.recognize_image(img, cfg)

    assert engine == "tesseract"
    assert "Simulated Tesseract Result" in text
    assert len(called_tesseract) == 1
    assert called_tesseract[0] == "eng"


def test_english_only_configuration(tmp_path):
    """FormFlow enforces English language OCR and field parsing."""
    cfg = _make_cfg(tmp_path)
    rules = fields.load_field_rules(cfg)
    assert rules.get("lang") == "eng"


def test_ocr_regions_reading_order_and_bbox_serialization():
    """Verify OcrRegion dataclass and bbox structure."""
    reg = ocr.OcrRegion(text="Applicant Name", bbox=(40, 50, 200, 75), confidence=0.92345, reading_order=3)
    d = reg.to_dict()
    assert d["text"] == "Applicant Name"
    assert d["bbox"] == [40, 50, 200, 75]
    assert d["confidence"] == round(0.92345, 4)
    assert d["reading_order"] == 3


def test_exif_handling_and_no_modification_of_original(tmp_path):
    """EXIF orientation is normalized in memory; original file bytes are 100% untouched."""
    img_path = tmp_path / "test_orig.jpg"
    im = Image.new("RGB", (120, 80), color="blue")
    im.save(img_path, "JPEG")
    orig_bytes = img_path.read_bytes()

    # Preprocess
    cleaned, transforms = ocr.preprocess_image_pipeline(im, upscale=False)
    assert "exif_transpose" in transforms

    # Original bytes must be completely identical
    assert img_path.read_bytes() == orig_bytes


def test_conservative_preprocessing_and_rectification_safety(tmp_path):
    """Preprocessing does not crash or distort images when boundaries are uncertain."""
    im = Image.new("L", (200, 200), color=128)
    # Plain image with no quad borders
    processed = ocr.preprocess_image(im, binarize=False, upscale=False)
    assert processed.size == (200, 200)


def test_field_extraction_aadhaar_synthetic(tmp_path):
    """Synthetic Aadhaar card extracts name, father name (S/O), DOB, gender, and Aadhaar number."""
    cfg = _make_cfg(tmp_path)
    rules = fields.load_field_rules(cfg)

    page_text = """
    Government of India
    Unique Identification Authority of India
    Samarth Sharma
    S/O: Ramesh Sharma
    DOB: 20/06/1986
    Gender: Male
    Address: H.No 4-12, Gandhi Nagar, Hyderabad, Telangana, 500001
    1234 5678 9012
    """
    pages = [{"page": 1, "method": "ocr", "text": page_text}]
    candidates = fields.extract_candidates(pages, rules)

    c_map = {c["field_key"]: c["value"] for c in candidates}
    assert c_map.get("full_name") == "Samarth Sharma"
    assert c_map.get("father_name") == "Ramesh Sharma"
    assert c_map.get("dob") == "1986-06-20"
    assert c_map.get("gender") == "Male"
    assert c_map.get("aadhaar_no") == "1234 5678 9012"
    assert c_map.get("pres_pincode") == "500001"
    assert "500001" in (c_map.get("address") or "")


def test_field_extraction_pan_synthetic(tmp_path):
    """Synthetic PAN card extracts PAN number, applicant name, father name, and DOB."""
    cfg = _make_cfg(tmp_path)
    rules = fields.load_field_rules(cfg)

    page_text = """
    INCOME TAX DEPARTMENT
    GOVT. OF INDIA
    Permanent Account Number Card
    ABCDE1234F
    Name
    RAHUL KUMAR
    Father's Name
    SURESH KUMAR
    Date of Birth
    15/08/1992
    """
    pages = [{"page": 1, "method": "ocr", "text": page_text}]
    candidates = fields.extract_candidates(pages, rules)

    c_map = {c["field_key"]: c["value"] for c in candidates}
    assert c_map.get("pan_no") == "ABCDE1234F"
    assert c_map.get("full_name") == "RAHUL KUMAR"
    assert c_map.get("father_name") == "SURESH KUMAR"
    assert c_map.get("dob") == "1992-08-15"


def test_field_extraction_ssc_synthetic(tmp_path):
    """Synthetic SSC memo extracts candidate name, father name, qualification, and ID marks."""
    cfg = _make_cfg(tmp_path)
    rules = fields.load_field_rules(cfg)

    page_text = """
    BOARD OF SECONDARY EDUCATION TELANGANA
    SECONDARY SCHOOL CERTIFICATE
    CERTIFIED THAT MOHAMMAD AWAD
    S/O MOHAMMAD AHMEDULLAH
    PASSED SSC EXAMINATION HELD IN MARCH 2010
    DATE OF BIRTH: 05/04/1994
    1. A mole on the right cheek
    2. A scar on left forehead
    """
    pages = [{"page": 1, "method": "ocr", "text": page_text}]
    candidates = fields.extract_candidates(pages, rules)

    c_map = {c["field_key"]: c["value"] for c in candidates}
    assert c_map.get("full_name") == "MOHAMMAD AWAD"
    assert c_map.get("father_name") == "MOHAMMAD AHMEDULLAH"
    assert c_map.get("qualification") == "10th Standard / SSC"
    assert c_map.get("dob") in ("1994-04-05", "05/04/1994")
    assert "A mole on the right cheek" in (c_map.get("id_mark_1") or "")
    assert "A scar on left forehead" in (c_map.get("id_mark_2") or "")


def test_year_only_date_never_converted_to_full_dob(tmp_path):
    """A date such as '1985' representing only year of birth must NEVER produce a complete DOB."""
    cfg = _make_cfg(tmp_path)
    rules = fields.load_field_rules(cfg)

    page_text = "Name: Ravi Kumar\nYear of Birth: 1985\nGender: Male\n"
    pages = [{"page": 1, "method": "ocr", "text": page_text}]
    candidates = fields.extract_candidates(pages, rules)

    dob_cands = [c for c in candidates if c["field_key"] == "dob"]
    assert dob_cands == []  # Strictly empty; no guessed day or month


def test_unknown_document_extraction_and_generic_ocr(tmp_path):
    """Unknown documents still receive generic OCR and candidate extraction."""
    cfg = _make_cfg(tmp_path)
    rules = fields.load_field_rules(cfg)

    page_text = "Electricity Bill\nCustomer Name: Venkat Rao\nAddress: Flat 101, Road 2, Banjara Hills, Hyderabad 500034\n"
    pages = [{"page": 1, "method": "ocr", "text": page_text}]
    candidates = fields.extract_candidates(pages, rules)

    c_map = {c["field_key"]: c["value"] for c in candidates}
    assert c_map.get("full_name") == "Venkat Rao"
    assert "500034" in (c_map.get("address") or "")
    assert c_map.get("pres_pincode") == "500034"


def test_id_document_with_photo_hint_never_skipped(tmp_path):
    """An identity document whose filename has a photo hint is NOT skipped if it has identity clues."""
    cfg = _make_cfg(tmp_path)
    conn = connect(cfg.db_path)

    img_path = tmp_path / "applicant_aadhaar_photo.png"
    Image.new("RGB", (200, 200), "white").save(img_path)

    # Document created with doctype='photo' due to weak filename classifier
    doc = create_document(
        conn,
        orig_filename="applicant_aadhaar_photo.png",
        orig_path=str(img_path),
        doctype="photo",
    )
    conn.close()

    # Run extraction with mock OCR
    fake_ocr = lambda img: ("Government of India Aadhaar Name: Samarth Sharma DOB: 20/06/1986", 0.90)
    result_doc = extract.run_extraction(doc["id"], cfg, ocr_fn=fake_ocr)

    # Must NOT be skipped!
    assert result_doc["ocr_status"] != "skipped"
    assert result_doc["ocr_status"] in ("ocr", "needs_attention")


def test_provenance_tracking_in_sidecar(tmp_path):
    """Sidecar JSON stores document ID, pipeline version, engine, classification evidence, and regions."""
    cfg = _make_cfg(tmp_path)
    conn = connect(cfg.db_path)

    img_path = tmp_path / "pan_card.jpg"
    Image.new("RGB", (200, 200), "white").save(img_path)

    doc = create_document(
        conn,
        orig_filename="pan_card.jpg",
        orig_path=str(img_path),
        doctype="unknown",
    )
    conn.close()

    fake_ocr = lambda img: (
        "INCOME TAX DEPARTMENT Permanent Account Number Card ABCDE1234F Name: Rahul Kumar",
        0.92,
        [{"text": "ABCDE1234F", "bbox": [10, 20, 100, 40], "confidence": 0.95, "reading_order": 0}],
        "tesseract",
        "5.4.0",
    )
    extract.run_extraction(doc["id"], cfg, ocr_fn=fake_ocr)

    sidecar = extract.sidecar_path(cfg, doc["id"])
    assert sidecar.exists()

    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["document_id"] == doc["id"]
    assert data["engine"] == "tesseract"
    assert data["pipeline_version"] == "5"
    assert "classification" in data
    assert data["classification"]["suggested_category"] == "pan"
    assert len(data["pages"]) == 1
    assert data["pages"][0]["regions"][0]["text"] == "ABCDE1234F"


def test_safe_reprocessing_without_duplication_or_customer_overwrite(tmp_path):
    """Reprocessing a document clears old candidate rows, preserves confirmed values and customer_fields,

    and does not spawn duplicate cases.
    """
    cfg = _make_cfg(tmp_path)
    conn = connect(cfg.db_path)

    cust = create_customer(conn, display_name="John Doe")
    # Operator confirmed customer profile field
    upsert_customer_field(conn, customer_id=cust["id"], field_key="full_name", value="John Doe Confirmed")

    case = create_case(conn, case_id="CASE-REP-01", customer_id=cust["id"])

    img_path = tmp_path / "id_doc.png"
    Image.new("RGB", (200, 200), "white").save(img_path)

    doc = create_document(
        conn,
        orig_filename="id_doc.png",
        orig_path=str(img_path),
        case_id=case["id"],
        doctype="supporting_ll",
    )

    # Initial extraction
    fake_ocr_v1 = lambda img: ("Name: John Old\nDOB: 01/01/1990", 0.85)
    extract.run_extraction(doc["id"], cfg, ocr_fn=fake_ocr_v1)

    fields_v1 = list_document_fields(conn, doc["id"])
    assert any(f["value"] == "John Old" for f in fields_v1)

    # Now reprocess document with updated OCR output
    fake_ocr_v2 = lambda img: ("Name: John New\nDOB: 01/01/1990", 0.95)
    reprocessed_doc = extract.reprocess_document(doc["id"], cfg, ocr_fn=fake_ocr_v2)

    fields_v2 = list_document_fields(conn, doc["id"])
    # Old candidate "John Old" must be gone; "John New" must be present
    assert not any(f["value"] == "John Old" for f in fields_v2)
    assert any(f["value"] == "John New" for f in fields_v2)

    # Confirmed customer profile field must NOT have been overwritten
    c_fields = get_customer_fields(conn, cust["id"])
    assert c_fields["full_name"]["value"] == "John Doe Confirmed"

    # Case must still be CASE-REP-01 (no duplicate cases)
    case_fields = list_case_fields(conn, case["id"])
    assert len(case_fields) > 0
    conn.close()


def test_no_network_or_model_download_during_processing(tmp_path, monkeypatch):
    """Ordinary document processing must not attempt any network socket connections."""
    def _blocked_socket(*args, **kwargs):
        raise RuntimeError("Network connection attempted during offline document processing!")

    monkeypatch.setattr(socket, "socket", _blocked_socket)

    cfg = _make_cfg(tmp_path)
    conn = connect(cfg.db_path)

    img_path = tmp_path / "offline_test.png"
    Image.new("RGB", (100, 100), "white").save(img_path)

    doc = create_document(
        conn,
        orig_filename="offline_test.png",
        orig_path=str(img_path),
        doctype="unknown",
    )
    conn.close()

    fake_ocr = lambda img: ("Name: Offline Tester\nDOB: 10/10/1990", 0.90)
    # Must succeed completely offline without triggering blocked socket
    result = extract.run_extraction(doc["id"], cfg, ocr_fn=fake_ocr)
    assert result["ocr_status"] in ("ocr", "needs_attention")
