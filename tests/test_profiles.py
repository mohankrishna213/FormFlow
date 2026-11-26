"""Tests for dynamic profiles.json, Portal Presets, custom document types, and /profiles UI."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw

from app import create_app
from app.config import load_config
from app.services.classify import classify_document
from app.services.prepare import prepare_document
from app.services.profiles import (
    custom_classifier_rules,
    delete_document_profile,
    load_profile_store,
    load_profiles,
    upsert_document_profile,
)


def _csrf(client, url="/profiles") -> str:
    html = client.get(url).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no CSRF token on {url}"
    return m.group(1)


def _make_card_jpeg(path: Path, size=(1200, 800)) -> Path:
    im = Image.new("RGB", size, (240, 240, 240))
    dr = ImageDraw.Draw(im)
    for y in range(40, size[1] - 40, 18):
        for x in range(40, size[0] - 40, 28):
            dr.rectangle([x, y, x + 16, y + 8], fill=((x * 7) % 200, (y * 5) % 200, ((x + y) * 3) % 200))
    im.save(path, "JPEG", quality=95)
    return path


def test_legacy_flat_v4_profiles_json_still_loads_and_supports_custom_keys(tmp_cfg, tmp_path):
    prof_path = tmp_path / "flat_profiles.json"
    prof_path.write_text(json.dumps({
        "version": "4",
        "photo": {"max_bytes": 40960, "output": "JPEG"},
        "voter_id": {
            "label": "Voter ID",
            "export_tag": "voter_id",
            "max_bytes": 153600,
            "crop_mode": "document",
            "filename_hints": ["voter", "epic"],
        },
    }), encoding="utf-8")
    cfg = load_config({**dict(tmp_cfg), "processing_profile_path": prof_path})
    profiles, version = load_profiles(cfg)
    assert version == "4"
    assert profiles["photo"]["max_bytes"] == 40960
    assert "voter_id" in profiles
    assert profiles["voter_id"]["max_bytes"] == 153600
    assert profiles["voter_id"]["label"] == "Voter ID"


def test_profiles_nav_link_dev_mode_only_and_csrf_enforced(tmp_cfg, tmp_path):
    prof_path = tmp_path / "profiles.json"
    app = create_app({**dict(tmp_cfg), "processing_profile_path": prof_path})
    client = app.test_client()

    # Hidden in Production (Desk) mode nav
    desk_html = client.get("/").get_data(as_text=True)
    assert 'href="/profiles"' not in desk_html

    # Visible in Dev mode nav
    app_dev = create_app({**dict(tmp_cfg), "processing_profile_path": prof_path, "ui_mode": "dev"})
    dev_html = app_dev.test_client().get("/").get_data(as_text=True)
    assert 'href="/profiles"' in dev_html

    # All mutating /profiles POST routes enforce CSRF
    assert client.post("/profiles/preset/activate", data={"preset_key": "exam_scholarship"}).status_code == 400
    assert client.post("/profiles/doc/save", data={"label": "Voter ID", "max_kb": "150"}).status_code == 400
    assert client.post("/profiles/reprepare-all").status_code == 400


def test_portal_preset_switch_create_and_delete(tmp_cfg, tmp_path):
    prof_path = tmp_path / "profiles.json"
    app = create_app({**dict(tmp_cfg), "processing_profile_path": prof_path, "ui_mode": "dev"})
    cfg = app.config["FORMFLOW"]
    client = app.test_client()

    # Default preset is sarathi (photo < 50 KB)
    profiles, _ = load_profiles(cfg)
    assert profiles["photo"]["max_bytes"] == 51200

    # Switch to exam_scholarship (photo < 30 KB, signature < 20 KB)
    token = _csrf(client, "/profiles")
    r = client.post("/profiles/preset/activate", data={
        "csrf_token": token, "preset_key": "exam_scholarship",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Exam / Scholarship" in r.get_data(as_text=True)
    profiles_exam, _ = load_profiles(cfg)
    assert profiles_exam["photo"]["max_bytes"] == 30720
    assert profiles_exam["signature"]["max_bytes"] == 20480

    # Create a custom preset cloned from sarathi
    token = _csrf(client, "/profiles")
    r2 = client.post("/profiles/preset/create", data={
        "csrf_token": token,
        "label": "College Admission",
        "description": "University portal upload targets",
        "clone_from": "sarathi",
    }, follow_redirects=True)
    assert r2.status_code == 200
    store = load_profile_store(cfg)
    assert store["active_preset"] == "college_admission"
    assert "college_admission" in store["presets"]

    # Delete custom preset -> cleanly falls back to sarathi
    token = _csrf(client, "/profiles")
    r3 = client.post("/profiles/preset/delete", data={
        "csrf_token": token, "preset_key": "college_admission",
    }, follow_redirects=True)
    assert r3.status_code == 200
    store2 = load_profile_store(cfg)
    assert "college_admission" not in store2["presets"]
    assert store2["active_preset"] == "sarathi"


def test_custom_document_type_classify_prepare_export_and_crop_dropdown(tmp_cfg, tmp_path):
    prof_path = tmp_path / "profiles.json"
    app = create_app({**dict(tmp_cfg), "processing_profile_path": prof_path, "ui_mode": "dev"})
    cfg = app.config["FORMFLOW"]
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    client = app.test_client()

    # Add custom document type "Voter ID" via UI route
    token = _csrf(client, "/profiles")
    r = client.post("/profiles/doc/save", data={
        "csrf_token": token,
        "label": "Voter ID",
        "doc_key": "voter_id",
        "export_tag": "voter_id",
        "max_kb": "120",
        "max_dim": "1800",
        "crop_mode": "document",
        "auto_crop": "1",
        "filename_hints": "voter, epic",
        "ocr_keywords": "election commission, electors photo identity",
        "description": "Indian Voter ID card <120 KB",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Voter ID" in r.get_data(as_text=True)

    profiles, _ = load_profiles(cfg)
    assert "voter_id" in profiles
    assert profiles["voter_id"]["max_bytes"] == 120 * 1024
    dyn_rules = custom_classifier_rules(profiles)

    # Filename hint classification
    c1 = classify_document("customer_epic_front.jpg", ".jpg", custom_rules=dyn_rules)
    assert c1.category == "voter_id"
    assert c1.is_identity_doc is True

    # OCR keyword classification on unlabeled WhatsApp filename
    c2 = classify_document(
        "WhatsApp Image 2026-11-25 at 4.00.00 PM.jpeg",
        ".jpeg",
        ocr_hints={"text": "ELECTION COMMISSION OF INDIA ELECTORS PHOTO IDENTITY CARD"},
        custom_rules=dyn_rules,
    )
    assert c2.category == "voter_id"
    assert c2.is_identity_doc is True

    # Prepare + Inbox\Ready_voter_id_1.jpg export under custom 120 KB limit
    staged = cfg.data_dir / "staging" / "epic_scan.jpg"
    staged.parent.mkdir(parents=True, exist_ok=True)
    _make_card_jpeg(staged)
    doc = prepare_document(staged, cfg)
    assert doc["status"] == "ready"
    assert doc["profile_name"] == "voter_id"
    assert doc["byte_size_prepared"] <= 120 * 1024
    assert doc["inbox_export_path"] and Path(doc["inbox_export_path"]).name == "Ready_voter_id_1.jpg"
    assert Path(doc["inbox_export_path"]).is_file()

    # Manual crop dropdown dynamically lists the new Voter ID type!
    crop_html = client.get(f"/documents/{doc['id']}/crop").get_data(as_text=True)
    assert 'value="voter_id"' in crop_html
    assert "Voter ID" in crop_html

    # Protected built-ins cannot be deleted, but custom voter_id can
    try:
        delete_document_profile(cfg, "photo")
        assert False, "protected photo profile must not be deletable"
    except ValueError:
        pass
    delete_document_profile(cfg, "voter_id")
    profiles_after, _ = load_profiles(cfg)
    assert "voter_id" not in profiles_after


def test_optional_reprepare_all_applies_new_kb_limit_without_modifying_original(tmp_cfg, tmp_path):
    prof_path = tmp_path / "profiles.json"
    app = create_app({**dict(tmp_cfg), "processing_profile_path": prof_path, "ui_mode": "dev"})
    cfg = app.config["FORMFLOW"]
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    client = app.test_client()

    staged = cfg.data_dir / "staging" / "aadhaar_big.jpg"
    staged.parent.mkdir(parents=True, exist_ok=True)
    _make_card_jpeg(staged, size=(1600, 1000))
    orig_hash = hashlib.sha256(staged.read_bytes()).hexdigest()

    doc = prepare_document(staged, cfg, doctype_hint="aadhaar")
    assert doc["status"] == "ready"
    assert doc["byte_size_prepared"] <= 512000

    # Tighten aadhaar limit to 100 KB (realistic compression target for a 1600x1000 card)
    upsert_document_profile(cfg, "aadhaar", {"max_bytes": 100 * 1024})

    # Trigger optional bulk re-prepare via UI endpoint
    token = _csrf(client, "/profiles")
    r = client.post("/profiles/reprepare-all", data={"csrf_token": token}, follow_redirects=True)
    assert r.status_code == 200
    assert "Re-prepared 1 existing document" in r.get_data(as_text=True)

    # Prepared copy & Inbox export shrunk under 100 KB, staged original completely untouched
    from app.db import connect, get_document

    conn = connect(cfg.db_path)
    try:
        updated = get_document(conn, doc["id"])
    finally:
        conn.close()
    assert updated["status"] == "ready"
    assert updated["byte_size_prepared"] <= 100 * 1024
    assert Path(updated["inbox_export_path"]).stat().st_size <= 100 * 1024
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == orig_hash


def _make_text_pdf(path: Path, pages: list[str]) -> Path:
    import pymupdf

    doc = pymupdf.open()
    try:
        for text in pages:
            page = doc.new_page(width=595, height=842)
            page.insert_text((50, 80), text, fontsize=14)
        doc.save(path)
    finally:
        doc.close()
    return path


def _make_large_scanned_pdf(path: Path, n_pages: int = 2) -> Path:
    """Create a multi-page scanned PDF (>250 KB) with embedded high-res page images."""
    import io
    import pymupdf

    doc = pymupdf.open()
    try:
        for idx in range(n_pages):
            im = Image.new("RGB", (1400, 1900), (248, 248, 248))
            dr = ImageDraw.Draw(im)
            dr.rectangle([60, 60, 1340, 1840], outline=(40, 40, 40), width=4)
            for y in range(120, 1760, 26):
                for x in range(100, 1280, 44):
                    c = ((x * (idx + 3) + y * 5) % 160) + 30
                    dr.rectangle([x, y, x + 28, y + 12], fill=(c, (c * 3) % 180, (c * 7) % 180))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=98)
            page = doc.new_page(width=595, height=842)
            page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(path)
    finally:
        doc.close()
    return path


def test_default_pdf_output_modes_for_sarathi_and_profiles_ui_badges(tmp_cfg, tmp_path):
    prof_path = tmp_path / "profiles.json"
    app = create_app({**dict(tmp_cfg), "processing_profile_path": prof_path, "ui_mode": "dev"})
    cfg = app.config["FORMFLOW"]
    profiles, _ = load_profiles(cfg)

    # LL and TR accept both JPG and PDF by default; photo/signature/aadhaar/ssc/pan/dl are JPG-only
    assert profiles["learner_licence"]["output_mode"] == "jpg_or_pdf"
    assert profiles["tr_document"]["output_mode"] == "jpg_or_pdf"
    for k in ("photo", "signature", "aadhaar", "ssc", "pan", "driving_licence"):
        assert profiles[k]["output_mode"] == "jpg", f"expected jpg output_mode for {k}"

    html = app.test_client().get("/profiles").get_data(as_text=True)
    assert "JPG / PDF" in html
    assert "JPG only" in html
    assert 'name="output_mode"' in html


def test_scanned_ll_and_tr_pdfs_compress_under_200kb_keeping_all_pages(tmp_cfg, tmp_path):
    import pymupdf

    prof_path = tmp_path / "profiles.json"
    cfg = load_config({**dict(tmp_cfg), "processing_profile_path": prof_path})
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)

    # 1. Large 2-page scanned LL PDF (>250 KB) -> compressed under 200 KB as PDF, 2 pages intact
    ll_pdf = _make_large_scanned_pdf(staging / "ll_scanned_copy.pdf", n_pages=2)
    assert ll_pdf.stat().st_size > 204800
    orig_hash = hashlib.sha256(ll_pdf.read_bytes()).hexdigest()

    ll_doc = prepare_document(ll_pdf, cfg)
    assert ll_doc["status"] == "ready"
    assert ll_doc["profile_name"] == "learner_licence"
    assert Path(ll_doc["prepared_path"]).suffix.lower() == ".pdf"
    assert ll_doc["mime"] == "application/pdf"
    assert ll_doc["byte_size_prepared"] < 204800
    assert Path(ll_doc["inbox_export_path"]).name == "Ready_ll_1.pdf"
    assert hashlib.sha256(ll_pdf.read_bytes()).hexdigest() == orig_hash
    with pymupdf.open(ll_doc["prepared_path"]) as out_pdf:
        assert out_pdf.page_count == 2

    # 2. Large 2-page scanned TR PDF (>250 KB) -> compressed under 200 KB as PDF
    tr_pdf = _make_large_scanned_pdf(staging / "tr_temporary_reg.pdf", n_pages=2)
    assert tr_pdf.stat().st_size > 204800
    tr_doc = prepare_document(tr_pdf, cfg)
    assert tr_doc["status"] == "ready"
    assert tr_doc["profile_name"] == "tr_document"
    assert Path(tr_doc["prepared_path"]).suffix.lower() == ".pdf"
    assert tr_doc["byte_size_prepared"] < 204800
    assert Path(tr_doc["inbox_export_path"]).name == f"Ready_tr_{tr_doc['id']}.pdf"

    # 3. LL submitted as JPG stays JPG under jpg_or_pdf
    ll_jpg = _make_card_jpeg(staging / "learner_licence_card.jpg", size=(1200, 800))
    ll_img_doc = prepare_document(ll_jpg, cfg)
    assert ll_img_doc["status"] == "ready"
    assert ll_img_doc["profile_name"] == "learner_licence"
    assert Path(ll_img_doc["prepared_path"]).suffix.lower() == ".jpg"
    assert ll_img_doc["mime"] == "image/jpeg"
    assert ll_img_doc["byte_size_prepared"] < 204800
    assert Path(ll_img_doc["inbox_export_path"]).name == f"Ready_ll_{ll_img_doc['id']}.jpg"


def test_aadhaar_pdf_auto_converts_to_jpg_and_unlabeled_ll_pdf_fast_classifies(tmp_cfg, tmp_path):
    prof_path = tmp_path / "profiles.json"
    cfg = load_config({**dict(tmp_cfg), "processing_profile_path": prof_path})
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)

    # 1. 2-page Aadhaar PDF (JPG-only profile) -> auto-converted to crisp JPG < 500 KB
    aadhaar_pdf = _make_text_pdf(
        staging / "aadhaar_ecard.pdf",
        [
            "GOVERNMENT OF INDIA\nUnique Identification Authority of India\nName: RAVI KUMAR\nDOB: 15/08/1996\n1234 5678 9012",
            "Address: H No 1-2-3 Main Road Hyderabad Telangana 500001",
        ],
    )
    a_doc = prepare_document(aadhaar_pdf, cfg)
    assert a_doc["status"] == "ready"
    assert a_doc["profile_name"] == "aadhaar"
    assert Path(a_doc["prepared_path"]).suffix.lower() == ".jpg"
    assert a_doc["mime"] == "image/jpeg"
    assert a_doc["byte_size_prepared"] < 512000
    assert Path(a_doc["inbox_export_path"]).name == f"Ready_aadhaar_{a_doc['id']}.jpg"
    with Image.open(a_doc["prepared_path"]) as im:
        assert im.format == "JPEG"
        assert im.height > im.width  # 2-page front+back stitched vertically

    # 2. Unlabeled WhatsApp PDF containing Form 3 LL text -> fast-classified as learner_licence, stays PDF!
    unlabeled_ll = _make_text_pdf(
        staging / "WhatsApp_Document_2026.pdf",
        ["GOVERNMENT OF TELANGANA\nFORM 3\nLEARNER'S LICENCE\nLicence No: TS009LL2026001234"],
    )
    u_doc = prepare_document(unlabeled_ll, cfg)
    assert u_doc["status"] == "ready"
    assert u_doc["profile_name"] == "learner_licence"
    assert Path(u_doc["prepared_path"]).suffix.lower() == ".pdf"
    assert u_doc["mime"] == "application/pdf"
    assert Path(u_doc["inbox_export_path"]).name == f"Ready_ll_{u_doc['id']}.pdf"


def test_frontend_profiles_output_mode_pdf_only_converts_image_to_pdf(tmp_cfg, tmp_path):
    import pymupdf

    prof_path = tmp_path / "profiles.json"
    app = create_app({**dict(tmp_cfg), "processing_profile_path": prof_path, "ui_mode": "dev"})
    cfg = app.config["FORMFLOW"]
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    client = app.test_client()

    # Add a custom PDF-only document type via the /profiles UI endpoint
    token = _csrf(client, "/profiles")
    r = client.post("/profiles/doc/save", data={
        "csrf_token": token,
        "label": "Medical Certificate",
        "doc_key": "medical_cert",
        "export_tag": "medical",
        "max_kb": "200",
        "max_dim": "1800",
        "crop_mode": "document",
        "output_mode": "pdf",
        "auto_crop": "1",
        "filename_hints": "medical, form1a",
        "ocr_keywords": "medical certificate, form 1a",
        "description": "Form 1A medical certificate (PDF only)",
    }, follow_redirects=True)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Medical Certificate" in body
    assert "PDF only" in body

    profiles, _ = load_profiles(cfg)
    assert profiles["medical_cert"]["output_mode"] == "pdf"

    # Drop a JPG for medical_cert -> automatically packed into compressed single-page PDF
    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    med_jpg = _make_card_jpeg(staging / "form1a_medical_scan.jpg", size=(1200, 900))
    doc = prepare_document(med_jpg, cfg)
    assert doc["status"] == "ready"
    assert doc["profile_name"] == "medical_cert"
    assert Path(doc["prepared_path"]).suffix.lower() == ".pdf"
    assert doc["mime"] == "application/pdf"
    assert doc["byte_size_prepared"] < 200 * 1024
    assert Path(doc["inbox_export_path"]).name == "Ready_medical_1.pdf"
    with pymupdf.open(doc["prepared_path"]) as out_pdf:
        assert out_pdf.page_count == 1
