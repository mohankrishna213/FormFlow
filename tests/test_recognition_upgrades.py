"""Tests for Option 1 offline recognition upgrades:
- RapidOCR-first fast classification & strict signature guard (no Tesseract needed)
- Dense multi-line document rejection in visual signature classifier
- Signature border-noise (dark desk/notebook top strip) rejection
- Bilingual & unlabeled PAN card layout parser + 'S Na' fragment rejection
- Offline Aadhaar Secure QR, Aadhaar XML QR, and PAN QR decoding + pixel scan
- YuNet ONNX face detector availability
- UTF-8 BOM resilience in config.local.json
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path

from PIL import Image, ImageDraw

from app.config import load_config
from app.services import document_cv, fields, ocr_paddle, qr_decode
from app.services.prepare import prepare_document


def test_pan_bilingual_and_garbled_label_rejects_s_na_and_extracts_names():
    rules = fields._DEFAULT_RULES

    # 1. Exact real-world failure mode: garbled "Father 'S Na" on cyan PAN card
    bilingual_pan_text = "\n".join([
        "INCOME TAX DEPARTMENT",
        "GOVT. OF INDIA",
        "Permanent Account Number Card",
        "APAPB0161N",
        "नाम / Name",
        "SURESH KUMAR REDDY",
        "Father 'S Na",
        "RAMESH KUMAR REDDY",
        "Date of Birth",
        "03/08/1970",
    ])
    cands = fields.extract_candidates([{"page": 1, "text": bilingual_pan_text, "method": "ocr"}], rules)
    by_key = {c["field_key"]: c["value"] for c in cands}
    assert by_key.get("pan_no") == "APAPB0161N"
    assert by_key.get("dob") == "1970-08-03"
    assert by_key.get("full_name") == "SURESH KUMAR REDDY"
    assert by_key.get("father_name") == "RAMESH KUMAR REDDY"
    assert by_key.get("father_name") != "S Na"

    # 2. Old unlabeled Indian PAN card (positional lines under header / PAN)
    old_pan_text = "\n".join([
        "INCOME TAX DEPARTMENT",
        "GOVT. OF INDIA",
        "ABCDE1234F",
        "VENKATESH RAO",
        "NARAYANA RAO",
        "15/06/1988",
        "Signature",
    ])
    cands2 = fields.extract_candidates([{"page": 1, "text": old_pan_text, "method": "ocr"}], rules)
    by_key2 = {c["field_key"]: c["value"] for c in cands2}
    assert by_key2.get("pan_no") == "ABCDE1234F"
    assert by_key2.get("full_name") == "VENKATESH RAO"
    assert by_key2.get("father_name") == "NARAYANA RAO"
    assert by_key2.get("dob") == "1988-06-15"


def test_extract_name_sequence_rejects_short_label_fragments():
    assert fields.extract_name_sequence("S Na") is None
    assert fields.extract_name_sequence("'S Na") is None
    assert fields.extract_name_sequence("A B") is None
    assert fields.extract_name_sequence("RAMA NAIDU") == "RAMA NAIDU"


def test_aadhaar_secure_qr_xml_qr_and_pan_qr_decoders():
    # 1. Official UIDAI V2 Secure QR big-integer zlib payload
    fields_raw = [
        b"V2", b"3", b"123420261125001", b"RAVI KUMAR SHARMA", b"15-08-1996",
        b"M", b"S/O MOHAN SHARMA", b"Hyderabad", b"Near Metro", b"1-2-34",
        b"Ameerpet", b"500016", b"Ameerpet SO", b"Telangana", b"Main Road",
        b"Khairatabad", b"Hyderabad", b"", b"",
    ]
    compressed = zlib.compress(b"\xff".join(fields_raw))
    big_int_payload = str(int.from_bytes(compressed, byteorder="big"))
    cands = qr_decode.decode_aadhaar_secure_qr(big_int_payload)
    assert cands is not None
    by_key = {c["field_key"]: c["value"] for c in cands}
    assert by_key["full_name"] == "RAVI KUMAR SHARMA"
    assert by_key["father_name"] == "MOHAN SHARMA"
    assert by_key["dob"] == "1996-08-15"
    assert by_key["gender"] == "Male"
    assert by_key["pres_pincode"] == "500016"
    assert by_key["pres_district"] == "Hyderabad"
    assert by_key["pres_state"] == "Telangana"
    assert "Ameerpet" in by_key["address"]

    # 2. Legacy Aadhaar XML QR (must NOT fabricate DOB when only yob is present)
    xml_payload = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<PrintLetterBarcodeData uid="234567890123" name="ANITA DEVI" gender="F" '
        'yob="1992" co="W/O SURESH KUMAR" house="4-5-6" street="MG Road" '
        'vtc="Warangal" dist="Warangal" state="Telangana" pc="506001"/>'
    )
    xml_cands = qr_decode.decode_aadhaar_xml_qr(xml_payload)
    assert xml_cands is not None
    xmap = {c["field_key"]: c["value"] for c in xml_cands}
    assert xmap["full_name"] == "ANITA DEVI"
    assert xmap["father_name"] == "SURESH KUMAR"
    assert xmap["gender"] == "Female"
    assert xmap["aadhaar_no"] == "2345 6789 0123"
    assert xmap["pres_pincode"] == "506001"
    assert "dob" not in xmap  # never fabricates full DOB from year-only yob!

    # 3. PAN QR payload
    pan_payload = "NSDL PAN\nPAN: ABCDE1234F\nName: KARTHIK VARMA\nFather's Name: PRASAD VARMA\nDOB: 21/04/1991"
    pan_cands = qr_decode.decode_pan_qr(pan_payload)
    assert pan_cands is not None
    pmap = {c["field_key"]: c["value"] for c in pan_cands}
    assert pmap["pan_no"] == "ABCDE1234F"
    assert pmap["full_name"] == "KARTHIK VARMA"
    assert pmap["father_name"] == "PRASAD VARMA"
    assert pmap["dob"] == "1991-04-21"


def test_qr_decode_from_rendered_qr_image_pixels():
    import zxingcpp

    xml_payload = (
        '<PrintLetterBarcodeData uid="987654321098" name="KIRAN KUMAR" gender="M" '
        'dob="10/05/1994" co="S/O RAJU" dist="Hyderabad" state="Telangana" pc="500001"/>'
    )
    bc = zxingcpp.create_barcode(xml_payload, zxingcpp.BarcodeFormat.QRCode)
    arr = zxingcpp.write_barcode_to_image(bc, scale=4)
    qr_im = Image.fromarray(arr).convert("RGB")
    # Place QR onto a white Aadhaar-like card
    card = Image.new("RGB", (600, 380), (255, 255, 255))
    card.paste(qr_im, (360, 120))

    assert qr_decode.detect_has_qr(card) is True
    cands, cat = qr_decode.extract_qr_candidates(card)
    assert cat == "aadhaar"
    by_key = {c["field_key"]: c["value"] for c in cands}
    assert by_key["full_name"] == "KIRAN KUMAR"
    assert by_key["dob"] == "1994-05-10"
    assert by_key["pres_pincode"] == "500001"


def test_dense_multiline_white_document_rejected_as_signature():
    # Simulate a white 10th memo / printed form sheet with 12 horizontal lines of printed text
    im = Image.new("RGB", (800, 1000), (245, 245, 245))
    dr = ImageDraw.Draw(im)
    for row in range(12):
        y = 70 + row * 72
        dr.rectangle([60, y, 740, y + 14], fill=(35, 35, 35))
        dr.rectangle([60, y + 24, 520, y + 34], fill=(55, 55, 55))

    vis_type, _, _ = document_cv.classify_visual_image(im, allow_signature=True)
    assert vis_type != "signature", "multi-line printed white sheet must never classify as signature"


def test_signature_crop_removes_top_brown_desk_border_strip():
    im = Image.new("RGB", (900, 600), (242, 240, 236))
    dr = ImageDraw.Draw(im)
    # Brown dark desk/binding strip along the top 6% of the page (like real WhatsApp photo)
    dr.rectangle([0, 0, 900, 36], fill=(95, 55, 25))
    # Centered blue pen signature strokes
    dr.line([(240, 310), (310, 270), (380, 330), (470, 280), (560, 320), (640, 290)], fill=(25, 45, 160), width=5)

    out_im, diag = document_cv.crop_and_clean_signature(im, {"clean_background": True})
    assert diag["crop_applied"] is True
    # Height must tighten well below full 600px (proving top brown strip was excluded!)
    assert out_im.height < 260
    # Top row of cropped signature should be crisp white paper, not brown desk!
    r, g, b = out_im.getpixel((out_im.width // 2, 2))
    assert min(r, g, b) > 220


def test_fast_classify_never_tags_white_doc_as_signature_when_tesseract_missing(tmp_cfg, tmp_path):
    # Simulate target PC with NO Tesseract installed
    cfg = load_config({**dict(tmp_cfg), "tesseract_cmd": str(tmp_path / "no_such_tesseract.exe")})
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)

    # White Aadhaar-like image with large clear text (unlabeled WhatsApp filename)
    im = Image.new("RGB", (900, 560), (250, 250, 250))
    dr = ImageDraw.Draw(im)
    dr.rectangle([10, 10, 890, 550], outline=(180, 180, 180), width=2)
    # Draw crisp lines of text; if RapidOCR is available it reads 'GOVERNMENT OF INDIA / AADHAAR',
    # and even if RapidOCR is absent (.venv Python 3.14), allow_signature=False prevents false 'signature'!
    for idx, line in enumerate([
        "GOVERNMENT OF INDIA",
        "Unique Identification Authority of India",
        "Name: SURESH KUMAR",
        "DOB: 03/08/1970",
        "MALE",
        "1234 5678 9012",
    ]):
        dr.text((60, 50 + idx * 75), line, fill=(20, 20, 20))
    p = staging / "WhatsApp_Image_2026-11-25_at_1.19.14_PM.jpeg"
    im.save(p, "JPEG", quality=92)

    doc = prepare_document(p, cfg, fast_classify=True)
    assert doc["doctype"] != "signature", "Aadhaar card must NEVER be classified as signature!"
    if ocr_paddle.available():
        assert doc["profile_name"] == "aadhaar"
        assert doc["status"] == "ready"


def test_yunet_model_bundled_and_config_bom_supported(tmp_path, monkeypatch):
    import cv2

    assert document_cv._YUNET_MODEL_PATH.is_file(), "YuNet ONNX model must exist under app/models/"
    det = cv2.FaceDetectorYN.create(str(document_cv._YUNET_MODEL_PATH), "", (320, 320))
    assert det is not None

    # UTF-8 BOM config.local.json (Windows PowerShell 5.1 Set-Content -Encoding UTF8)
    import app.config as cfg_mod

    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    (fake_root / "config.local.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"allow_reset": True}).encode("utf-8"))
    monkeypatch.setattr(cfg_mod, "REPO_ROOT", fake_root)
    loaded = cfg_mod.load_config()
    assert loaded.allow_reset is True
