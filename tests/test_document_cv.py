"""Phase 1 & 2 document CV cropping, cleaning, 50KB/500KB profiles, and crop UI tests."""
import hashlib
import json
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from app import create_app
from app.config import load_config
from app.db import connect, create_case, create_document, init_db
from app.services.document_cv import (
    apply_manual_crop,
    crop_and_clean_signature,
    crop_document_card,
    crop_passphoto,
    trim_dark_screenshot_bars,
)
from app.services.extract import run_extraction
from app.services.prepare import parse_prep_meta, prepare_document, reprepare_document
from app.services.profiles import load_profiles


def _make_cfg(tmp_cfg, **kw):
    d = dict(tmp_cfg)
    d.update({"stable_checks": 2, "stable_interval_sec": 0.0})
    d.update(kw)
    cfg = load_config(d)
    cfg.ensure_data_dirs()
    return cfg


def _stage(cfg, name: str, img: Image.Image, fmt: str = "JPEG", quality: int = 92) -> Path:
    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    p = staging / name
    if fmt == "JPEG":
        img.save(p, "JPEG", quality=quality)
    else:
        img.save(p, fmt)
    return p


def _synth_card_on_textured_bg(w: int = 800, h: int = 600) -> Image.Image:
    """Synthetic bright ID card sitting on a dark noisy textured desk/mesh."""
    rng = np.random.default_rng(123)
    bg = rng.integers(25, 85, size=(h, w, 3), dtype=np.uint8)
    # Give background a strong reddish texture like fabric/mesh
    bg[:, :, 0] = np.clip(bg[:, :, 0].astype(int) + 45, 0, 255).astype(np.uint8)
    img = Image.fromarray(bg, mode="RGB")
    draw = ImageDraw.Draw(img)
    # Draw crisp bright ID card in center (~50% area, ~1.58 aspect ratio)
    card_box = [140, 140, 660, 465]
    draw.rectangle(card_box, fill=(242, 245, 248), outline=(180, 185, 190), width=2)
    # Top tricolour/header stripe + simulated dark text bars + photo square
    draw.rectangle([160, 155, 640, 185], fill=(40, 150, 75))
    draw.rectangle([165, 215, 265, 345], fill=(90, 95, 110))
    for y in (225, 255, 285, 315, 390):
        draw.rectangle([290, y, 580, y + 12], fill=(35, 35, 40))
    return img


def _synth_phone_screenshot_card() -> Image.Image:
    """Synthetic phone screenshot with black UI bars + card on dark concrete."""
    card_scene = _synth_card_on_textured_bg(600, 480)
    full = Image.new("RGB", (600, 800), (5, 5, 5))
    full.paste(card_scene, (0, 160))
    draw = ImageDraw.Draw(full)
    # Simulate status bar text at top & 'Crop and adjust' pill at bottom
    draw.rectangle([40, 35, 140, 52], fill=(230, 230, 230))
    draw.rounded_rectangle([180, 690, 420, 730], radius=10, fill=(45, 45, 45))
    draw.rectangle([210, 705, 390, 718], fill=(220, 220, 220))
    return full


def _synth_signature_on_desk(w: int = 900, h: int = 650) -> Image.Image:
    """Synthetic blue pen signature on warm shadowed paper sitting on dark wood."""
    img = Image.new("RGB", (w, h), (55, 40, 30))
    draw = ImageDraw.Draw(img)
    draw.rectangle([100, 100, w - 100, h - 100], fill=(226, 222, 212))
    pts = [(260, 340), (295, 290), (330, 360), (380, 300), (430, 350), (500, 310), (580, 345)]
    draw.line(pts, fill=(20, 35, 140), width=5)
    draw.line([(270, 375), (600, 368)], fill=(20, 35, 140), width=3)
    return img


def test_profiles_v4_limits_and_crop_flags(tmp_cfg):
    cfg = _make_cfg(tmp_cfg)
    profiles, version = load_profiles(cfg)
    assert version == "5"
    assert profiles["photo"]["max_bytes"] == 51200
    assert profiles["photo"]["auto_crop"] is True
    assert profiles["signature"]["max_bytes"] == 51200
    assert profiles["signature"]["auto_crop"] is True
    assert profiles["signature"]["clean_background"] is True
    assert profiles["aadhaar"]["max_bytes"] == 512000
    assert profiles["aadhaar"]["auto_crop"] is True
    for k in ("ssc", "learner_licence", "pan", "driving_licence", "tr_document", "supporting_ll"):
        assert profiles[k]["max_bytes"] == 204800
        assert profiles[k]["auto_crop"] is True


def test_trim_dark_screenshot_bars_strips_bars_and_ignores_solid_dark():
    shot = _synth_phone_screenshot_card()
    trimmed, diag = trim_dark_screenshot_bars(np.array(shot))
    assert diag["applied"] is True
    assert trimmed.shape[0] < shot.height * 0.70

    solid_dark = np.full((300, 300, 3), 10, dtype=np.uint8)
    kept, diag2 = trim_dark_screenshot_bars(solid_dark)
    assert diag2["applied"] is False
    assert kept.shape == solid_dark.shape


def test_crop_document_card_strips_phone_ui_and_textured_background():
    shot = _synth_phone_screenshot_card()
    cropped, diag = crop_document_card(shot)
    assert diag["crop_applied"] is True
    # Should isolate the ~520x325 card out of the 600x800 screenshot
    assert cropped.width < 580
    assert cropped.height < 420
    aspect = cropped.width / cropped.height
    assert 1.25 <= aspect <= 1.95


def test_crop_document_card_preserves_stacked_front_and_back():
    sheet = Image.new("RGB", (600, 900), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    # Front card (top) and Back card (bottom)
    draw.rectangle([90, 90, 510, 350], fill=(225, 232, 240), outline=(40, 40, 40), width=3)
    draw.rectangle([120, 120, 480, 145], fill=(40, 140, 60))
    draw.rectangle([90, 510, 510, 770], fill=(225, 232, 240), outline=(40, 40, 40), width=3)
    draw.rectangle([330, 560, 470, 700], fill=(30, 30, 30))  # QR block
    out, diag = crop_document_card(sheet)
    assert diag["crop_applied"] is True
    # Must preserve vertical height spanning both cards (>= 640px) while trimming outer margins
    assert out.height >= 640
    assert out.width <= 480


def test_crop_document_card_trims_white_scan_margins_and_ignores_tiny_watermark():
    scan = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(scan)
    draw.rectangle([20, 220, 580, 560], fill=(230, 235, 240), outline=(50, 50, 50), width=2)
    draw.rectangle([140, 240, 460, 265], fill=(35, 130, 55))
    # Tiny bottom-right watermark separated by wide blank margin
    draw.rectangle([450, 775, 570, 788], fill=(120, 120, 120))
    out, diag = crop_document_card(scan)
    assert diag["crop_applied"] is True
    assert out.height < 420  # blank margins + isolated tiny watermark excluded


def test_crop_document_card_does_not_clip_full_frame_shadowed_page():
    # Simulates an A4 Form 3 page with slight darkening gradient toward bottom
    h, w = 800, 600
    grad = np.linspace(225, 175, h, dtype=np.uint8)[:, None]
    arr = np.repeat(np.repeat(grad, w, axis=1)[:, :, None], 3, axis=2)
    img = Image.fromarray(arr, mode="RGB")
    draw = ImageDraw.Draw(img)
    for y in range(60, 760, 35):
        draw.rectangle([50, y, 520, y + 8], fill=(35, 35, 35))
    out, diag = crop_document_card(img)
    assert diag["crop_applied"] is False
    assert out.size == img.size


def test_crop_and_clean_signature_extracts_and_whitens_under_50kb(tmp_cfg):
    cfg = _make_cfg(tmp_cfg)
    sig = _synth_signature_on_desk()
    src = _stage(cfg, "customer_signature.jpg", sig)
    orig_hash = hashlib.sha256(src.read_bytes()).hexdigest()

    doc = prepare_document(src, cfg, applicant_name="Asha", doctype_hint="signature")
    assert doc["status"] == "ready"
    assert doc["byte_size_prepared"] < 51200
    assert hashlib.sha256(src.read_bytes()).hexdigest() == orig_hash

    meta = parse_prep_meta(doc["prep_meta"])
    assert meta["crop_applied"] is True
    assert meta["cleaned_bg"] is True

    with Image.open(doc["prepared_path"]) as out:
        assert out.width < sig.width
        assert out.height < sig.height
        # Corners of cleaned signature should be clean bright white paper
        corner = np.array(out.convert("L"))[2, 2]
        assert corner >= 240


def test_crop_signature_refuses_random_noise():
    rng = random.Random(99)
    noise = Image.frombytes("RGB", (600, 250), rng.randbytes(600 * 250 * 3))
    out, diag = crop_and_clean_signature(noise)
    assert diag["crop_applied"] is False
    assert out.size == noise.size


def test_passphoto_trims_bars_and_meets_50kb_limit(tmp_cfg):
    cfg = _make_cfg(tmp_cfg)
    # Portrait photo framed inside a black phone gallery screenshot
    full = Image.new("RGB", (500, 800), (6, 6, 6))
    draw = ImageDraw.Draw(full)
    draw.rectangle([0, 150, 500, 650], fill=(205, 220, 235))
    draw.ellipse([160, 220, 340, 420], fill=(195, 145, 115))
    draw.rectangle([110, 430, 390, 650], fill=(40, 60, 120))
    src = _stage(cfg, "passport_photo.jpg", full)

    doc = prepare_document(src, cfg, doctype_hint="photo")
    assert doc["status"] == "ready"
    assert doc["byte_size_prepared"] < 51200
    meta = parse_prep_meta(doc["prep_meta"])
    assert meta["crop_applied"] is True
    with Image.open(doc["prepared_path"]) as out:
        assert out.height < full.height


def test_aadhaar_card_auto_crops_and_meets_500kb_limit(tmp_cfg):
    cfg = _make_cfg(tmp_cfg)
    shot = _synth_phone_screenshot_card()
    src = _stage(cfg, "aadhaar_front.jpg", shot)
    orig_hash = hashlib.sha256(src.read_bytes()).hexdigest()

    doc = prepare_document(src, cfg, doctype_hint="aadhaar")
    assert doc["status"] == "ready"
    assert doc["profile_name"] == "aadhaar"
    assert doc["byte_size_prepared"] < 512000
    assert hashlib.sha256(src.read_bytes()).hexdigest() == orig_hash
    meta = parse_prep_meta(doc["prep_meta"])
    assert meta["crop_applied"] is True
    with Image.open(doc["prepared_path"]) as out:
        assert out.height < 420


def test_unlabeled_phone_card_auto_reprepares_after_ocr(tmp_cfg):
    cfg = _make_cfg(tmp_cfg)
    shot = _synth_phone_screenshot_card()
    src = _stage(cfg, "WhatsApp_Image_20261123.jpg", shot)

    # Initial prepare has no filename clue -> unknown passthrough
    doc = prepare_document(src, cfg)
    assert doc["doctype"] == "unknown"
    assert doc["status"] == "needs_attention"

    fake_ocr = lambda img: (
        "Government of India\nAadhaar\nName: Ravi Kumar\nDOB: 15/08/1996\n1234 5678 9012",
        0.92,
    )
    run_extraction(doc["id"], cfg, ocr_fn=fake_ocr)

    conn = connect(cfg.db_path)
    try:
        updated = conn.execute("SELECT * FROM documents WHERE id = ?", (doc["id"],)).fetchone()
        assert updated["doctype"] == "supporting_ll"
        assert updated["status"] == "ready"
        assert updated["byte_size_prepared"] < 512000
        meta = parse_prep_meta(updated["prep_meta"])
        assert meta["crop_applied"] is True
    finally:
        conn.close()


def test_manual_crop_and_rotate_preserves_original_and_logs_event(tmp_cfg):
    cfg = _make_cfg(tmp_cfg)
    img = _synth_card_on_textured_bg(800, 600)
    src = _stage(cfg, "card.jpg", img)
    orig_hash = hashlib.sha256(src.read_bytes()).hexdigest()

    doc = prepare_document(src, cfg, doctype_hint="supporting_ll")
    conn = connect(cfg.db_path)
    try:
        case = create_case(conn, "CASE-20261123-001", status="needs_attention")
        conn.execute("UPDATE documents SET case_id = ? WHERE id = ?", (case["id"], doc["id"]))
        conn.commit()
    finally:
        conn.close()

    updated = reprepare_document(
        doc["id"],
        cfg,
        doctype_hint="aadhaar",
        manual_crop=(0.15, 0.20, 0.70, 0.60),
        rotate_deg=90,
        clean_signature_bg=False,
    )
    assert updated["status"] == "ready"
    assert updated["doctype"] == "aadhaar"
    assert updated["byte_size_prepared"] < 512000
    assert hashlib.sha256(src.read_bytes()).hexdigest() == orig_hash
    meta = parse_prep_meta(updated["prep_meta"])
    assert meta["crop_applied"] is True
    assert meta["method"] == "operator-manual-crop"

    with Image.open(updated["prepared_path"]) as out:
        # Rotated 800x600 -> 600x800, cropped 70% x 60% -> ~420x480
        assert 400 <= out.width <= 440
        assert 460 <= out.height <= 500

    conn = connect(cfg.db_path)
    try:
        ev = conn.execute(
            "SELECT * FROM case_events WHERE case_id = ? AND kind = 'document_prepared'",
            (case["id"],),
        ).fetchone()
        assert ev is not None
    finally:
        conn.close()


def test_crop_dashboard_routes_and_csrf(tmp_cfg):
    cfg = _make_cfg(tmp_cfg)
    init_db(cfg.db_path)
    src = _stage(cfg, "raw_sig.jpg", _synth_signature_on_desk())
    doc = prepare_document(src, cfg, doctype_hint="signature")

    app = create_app(tmp_cfg)
    client = app.test_client()

    # GET crop editor page
    r = client.get(f"/documents/{doc['id']}/crop")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "cropCanvas" in body
    assert "Apply Manual Crop" in body

    # Extract CSRF token from form
    import re
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', body)
    assert m is not None
    token = m.group(1)

    # POST without CSRF must fail
    r_bad = client.post(f"/documents/{doc['id']}/crop", data={"mode": "auto", "doctype": "signature"})
    assert r_bad.status_code == 400

    # POST manual crop with CSRF succeeds & redirects
    r_ok = client.post(
        f"/documents/{doc['id']}/crop",
        data={
            "csrf_token": token,
            "mode": "manual",
            "doctype": "signature",
            "crop_x": "0.20",
            "crop_y": "0.30",
            "crop_w": "0.60",
            "crop_h": "0.35",
            "rotate_deg": "0",
            "clean_bg": "1",
        },
        follow_redirects=False,
    )
    assert r_ok.status_code == 302
    assert f"/documents/{doc['id']}/crop?saved=1" in r_ok.headers["Location"]

    # Prepared endpoint sets no-store cache control & returns valid JPEG
    r_img = client.get(f"/documents/{doc['id']}/prepared")
    assert r_img.status_code == 200
    assert r_img.headers.get("Cache-Control") == "no-store"
    assert r_img.mimetype == "image/jpeg"
    assert len(r_img.data) < 51200


def test_ready_prefix_ignored_by_watcher_and_ingest_no_loop(tmp_cfg):
    import time
    from app.services.filters import is_processed_output
    from app.services.ingest import ingest_file
    from app.services.jobs import Worker
    from app.services.watcher import InboxWatcher

    cfg = _make_cfg(tmp_cfg)
    assert is_processed_output("READY_sample_aadhaar.jpg") is True
    assert is_processed_output("ready_photo.jpg") is True
    assert is_processed_output("WhatsApp_Image.jpg") is False

    p1 = cfg.inbox_dir / "READY_sample_aadhaar.jpg"
    p2 = cfg.inbox_dir / "ready_lower.jpg"
    p1.write_bytes(b"processed-1")
    p2.write_bytes(b"processed-2")

    w = Worker(cfg)
    watcher = InboxWatcher(cfg, w)
    assert watcher.scan_existing() == 0
    watcher.handler._schedule(str(p1))
    time.sleep(0.15)
    assert w.pending == 0
    assert ingest_file(p1, cfg, sleep_fn=lambda s: None) is None
    assert p1.exists()  # untouched in Inbox


def test_inbox_drop_exports_ready_file_and_never_reingests(tmp_cfg):
    from app.services.ingest import ingest_file
    from app.services.jobs import Worker
    from app.services.watcher import InboxWatcher

    cfg = _make_cfg(tmp_cfg)
    raw = cfg.inbox_dir / "WhatsApp Image 2026-11-23 at 10.21.32 PM.jpeg"
    _synth_phone_screenshot_card().save(raw, "JPEG", quality=92)

    job = ingest_file(raw, cfg, sleep_fn=lambda s: None)
    assert job["state"] == "ready"
    assert not raw.exists()  # moved to staging

    doc = prepare_document(job["staged_path"], cfg, job_id=job["id"])
    assert doc["doctype"] == "unknown"

    fake_ocr = lambda img: (
        "Government of India\nAadhaar\nName: Ravi Kumar\nDOB: 15/08/1996\n1234 5678 9012",
        0.92,
    )
    run_extraction(doc["id"], cfg, ocr_fn=fake_ocr)

    # Processed cropped file must appear directly in Inbox as Ready_aadhaar_1.jpg (converted from .jpeg)
    ready_files = [p for p in cfg.inbox_dir.iterdir() if p.name.upper().startswith("READY_")]
    assert len(ready_files) == 1
    out_file = ready_files[0]
    assert out_file.name == "Ready_aadhaar_1.jpg"
    assert out_file.stat().st_size < 512000
    with Image.open(out_file) as im:
        assert im.height < 420

    # Watcher scan must ignore the exported Ready_ file (zero infinite loop)
    watcher = InboxWatcher(cfg, Worker(cfg))
    assert watcher.scan_existing() == 0


def test_visual_auto_classification_unlabeled_signature_and_card(tmp_cfg):
    from app.services.ingest import ingest_file

    cfg = _make_cfg(tmp_cfg)
    # 1. Unlabeled WhatsApp signature on shadowed paper on dark wood desk (.jpeg -> .jpg)
    raw_sig = cfg.inbox_dir / "WhatsApp Image 2026-11-23 at 10.21.32 PM.jpeg"
    _synth_signature_on_desk().save(raw_sig, "JPEG", quality=92)
    job_sig = ingest_file(raw_sig, cfg, sleep_fn=lambda s: None)
    doc_sig = prepare_document(job_sig["staged_path"], cfg, job_id=job_sig["id"])
    assert doc_sig["doctype"] == "unknown"

    empty_ocr = lambda img: ("", 0.0)
    out_sig = run_extraction(doc_sig["id"], cfg, ocr_fn=empty_ocr)
    assert out_sig["ocr_status"] == "skipped"

    conn = connect(cfg.db_path)
    try:
        row_sig = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_sig["id"],)).fetchone()
        assert row_sig["doctype"] == "signature"
        assert row_sig["status"] == "ready"
        assert row_sig["case_id"] is not None  # auto-created burst case #1
        assert row_sig["byte_size_prepared"] < 51200
        exp_sig = Path(row_sig["inbox_export_path"])
        assert exp_sig.exists()
        assert exp_sig.parent.resolve() == cfg.inbox_dir.resolve()
        assert exp_sig.name == "Ready_signature_1.jpg"
        assert exp_sig.stat().st_size < 51200
    finally:
        conn.close()

    # 2. Unlabeled textured-background card in same burst -> grouped into same case #1 (<200KB)
    raw_card = cfg.inbox_dir / "IMG_9999.jpeg"
    _synth_phone_screenshot_card().save(raw_card, "JPEG", quality=92)
    job_card = ingest_file(raw_card, cfg, sleep_fn=lambda s: None)
    doc_card = prepare_document(job_card["staged_path"], cfg, job_id=job_card["id"])
    run_extraction(doc_card["id"], cfg, ocr_fn=empty_ocr)

    conn = connect(cfg.db_path)
    try:
        row_card = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_card["id"],)).fetchone()
        assert row_card["doctype"] == "supporting_ll"
        assert row_card["status"] == "ready"
        assert row_card["case_id"] == row_sig["case_id"]
        assert row_card["byte_size_prepared"] < 204800
        exp_card = Path(row_card["inbox_export_path"])
        assert exp_card.exists()
        assert exp_card.name == "Ready_doc_1.jpg"
    finally:
        conn.close()


def test_manual_crop_fallback_updates_and_replaces_inbox_ready_file(tmp_cfg):
    cfg = _make_cfg(tmp_cfg)
    src = _stage(cfg, "0123456789ab_raw_card.jpg", _synth_card_on_textured_bg(800, 600))
    orig_hash = hashlib.sha256(src.read_bytes()).hexdigest()

    doc = prepare_document(src, cfg, doctype_hint="supporting_ll")
    first_export = Path(doc["inbox_export_path"])
    assert first_export.exists()
    assert first_export.name == "Ready_doc_1.jpg"

    # Manual fallback changes doctype to aadhaar + custom crop -> replaces old Ready_ file cleanly
    updated = reprepare_document(
        doc["id"], cfg, doctype_hint="aadhaar",
        manual_crop=(0.15, 0.20, 0.70, 0.60), rotate_deg=0,
    )
    second_export = Path(updated["inbox_export_path"])
    assert second_export.exists()
    assert second_export.name == "Ready_aadhaar_1.jpg"
    assert not first_export.exists()  # stale old file cleaned up
    assert second_export.read_bytes() == Path(updated["prepared_path"]).read_bytes()

    # Second manual tweak with same doctype overwrites in place (no duplicates)
    updated2 = reprepare_document(
        doc["id"], cfg, doctype_hint="aadhaar",
        manual_crop=(0.20, 0.25, 0.55, 0.50), rotate_deg=0,
    )
    third_export = Path(updated2["inbox_export_path"])
    assert third_export.resolve() == second_export.resolve()
    assert third_export.read_bytes() == Path(updated2["prepared_path"]).read_bytes()
    assert len([p for p in cfg.inbox_dir.iterdir() if p.name.upper().startswith("READY_")]) == 1
    assert hashlib.sha256(src.read_bytes()).hexdigest() == orig_hash


def test_bilingual_aadhaar_and_telangana_dl_ll_field_extraction(tmp_cfg):
    from app.services.fields import extract_candidates, load_field_rules

    cfg = _make_cfg(tmp_cfg)
    rules = load_field_rules(cfg)

    # 1. Bilingual Aadhaar card with Hindi/Punjabi OCR noise, e-Aadhaar disclaimer, & 2-column lowercase trailing noise
    aadhaar_text = (
        "ab cd\n"
        "Aadhaar is proof of identity, not of citizenship or date of birth / DOB\n"
        "i Devisha Singh the ab cd ef gh\n"
        "¥ Date of BirthYDOB; 25/02/1996 {\n"
        "i i Female/ FEMALE te]\n"
        "4960 4285 5827\n"
        "VID: 9152 9413 7037 1304\n"
    )
    c_adh = {c["field_key"]: c["value"] for c in extract_candidates([{"page": 1, "text": aadhaar_text, "method": "ocr"}], rules)}
    assert c_adh["full_name"] == "Devisha Singh"
    assert c_adh["dob"] == "1996-02-25"
    assert c_adh["gender"] == "Female"
    assert c_adh["aadhaar_no"] == "4960 4285 5827"

    # 2. Bilingual Aadhaar with Father border noise & single-line name noise prefix
    aadhaar2_text = (
        "'~33 St se Nilanjana Dhar\n"
        "“3 | Father: KRISHNENDU DHAR\n"
        "Year of Birth: 1995\n"
        "= xen Female ee\n"
        "6567 2007 8381\n"
    )
    c_adh2 = {c["field_key"]: c["value"] for c in extract_candidates([{"page": 1, "text": aadhaar2_text, "method": "ocr"}], rules)}
    assert c_adh2["full_name"] == "Nilanjana Dhar"
    assert c_adh2["father_name"] == "KRISHNENDU DHAR"
    assert c_adh2["gender"] == "Female"
    assert c_adh2["aadhaar_no"] == "6567 2007 8381"

    # 3. Telangana paper DL without Name/Father labels + Reference No that must NOT fake an address PIN
    dl_text = (
        "INDIAN UNION DRIVING LICENSE\n"
        "TELANGANA STATE\n"
        "TS30520150004389\n"
        "PARUSHA AMULU PAGIDI\n"
        "PEDDA MYSAIAH\n"
        "Badge No : 5020\n"
        "Reference No. : DLRTS30$780121\n"
        "Date of Birth : 20/07/1979\n"
    )
    c_dl = {c["field_key"]: c["value"] for c in extract_candidates([{"page": 1, "text": dl_text, "method": "ocr"}], rules)}
    assert c_dl["licence_no"] == "TS30520150004389"
    assert c_dl["full_name"] == "PARUSHA AMULU PAGIDI"
    assert c_dl["father_name"] == "PEDDA MYSAIAH"
    assert c_dl["dob"] == "1979-07-20"
    assert "address" not in c_dl

    # 4. Form 3 LL with dropped slash in 'SonWife/Daughter of' and hyphenated 'Pin-500010' stop boundary
    ll_text = (
        "GOVERNMENT OF TELANGANA\n"
        "FORM 3 LEARNER'S LICENCE\n"
        "Licence Number LLRTG008210082024\n"
        "Display Name A SHIVA LAXMAN SRIRAM S\n"
        "SonWife/Daughter of ATCHUTUNI SRINIVASAMURTHY\n"
        "Date of Birth 01/07/2006\n"
        "Present Address 3-4-12/2\n"
        "FLAT NO 304 3RD FLOOR, ALWAL TIRUMALAGIRI\n"
        "TELANGANA Pin-500010\n"
        "Marks of Identification NA\n"
    )
    c_ll = {c["field_key"]: c["value"] for c in extract_candidates([{"page": 1, "text": ll_text, "method": "ocr"}], rules)}
    assert c_ll["licence_no"] == "LLRTG008210082024"
    assert c_ll["father_name"] == "ATCHUTUNI SRINIVASAMURTHY"
    assert c_ll["pres_pincode"] == "500010"
    assert "Marks of Identification" not in c_ll["address"]


def test_api_case_documents_and_prepared_download_auth_and_isolation(tmp_cfg):
    from app.services.api_auth import TOKEN_FILENAME
    from app.services.review import save_review

    app = create_app(dict(tmp_cfg))
    cfg = app.config["FORMFLOW"]
    token = (cfg.data_dir / TOKEN_FILENAME).read_text(encoding="utf-8").strip()
    hdrs = {"Authorization": f"Bearer {token}"}

    # Stage & prepare: 1 case card, 1 unassigned signature, 1 document on another case
    card_src = _stage(cfg, "aadhaar_front.jpg", _synth_phone_screenshot_card())
    sig_src = _stage(cfg, "customer_signature.jpg", _synth_signature_on_desk())
    other_src = _stage(cfg, "other_card.jpg", _synth_card_on_textured_bg())

    d_card = prepare_document(card_src, cfg, doctype_hint="aadhaar")
    d_sig = prepare_document(sig_src, cfg, doctype_hint="signature")
    d_other = prepare_document(other_src, cfg, doctype_hint="aadhaar")

    conn = connect(cfg.db_path)
    try:
        c1 = create_case(conn, "CASE-20261123-DOC1", status="needs_attention")
        c2 = create_case(conn, "CASE-20261123-DOC2", status="needs_attention")
        conn.execute("UPDATE documents SET case_id = ? WHERE id = ?", (c1["id"], d_card["id"]))
        conn.execute("UPDATE documents SET case_id = ? WHERE id = ?", (c2["id"], d_other["id"]))
        conn.commit()
    finally:
        conn.close()

    client = app.test_client()

    # Unauthenticated -> 401
    assert client.get("/api/cases/CASE-20261123-DOC1/documents").status_code == 401

    # Default Auto-Ready mode serves ready files even before manual Validate & Save (still case-isolated)
    r_unrev = client.get("/api/cases/CASE-20261123-DOC1/documents", headers=hdrs)
    assert r_unrev.status_code == 200
    assert d_other["id"] not in {d["id"] for d in r_unrev.get_json()["documents"]}
    assert client.get(f"/api/cases/CASE-20261123-DOC1/documents/{d_other['id']}/prepared", headers=hdrs).status_code == 403

    # Strict review mode (auto_ready_fill=False) blocks unreviewed case with 409
    cfg.auto_ready_fill = False
    assert client.get("/api/cases/CASE-20261123-DOC1/documents", headers=hdrs).status_code == 409
    cfg.auto_ready_fill = True

    # Mark c1 reviewed
    conn = connect(cfg.db_path)
    try:
        _, _, errs = save_review(conn, "CASE-20261123-DOC1", {
            "full_name": "Ravi Kumar", "father_name": "", "dob": "1995-08-17",
            "address": "", "licence_no": "", "appl_number": "",
        })
        assert not errs
    finally:
        conn.close()

    # Reviewed case returns its own card + unassigned ready signature, NEVER c2's document
    r = client.get("/api/cases/CASE-20261123-DOC1/documents", headers=hdrs)
    assert r.status_code == 200
    docs = r.get_json()["documents"]
    ids = {d["id"] for d in docs}
    assert d_card["id"] in ids
    assert d_sig["id"] in ids
    assert d_other["id"] not in ids

    # Fetch prepared bytes for case's card & unassigned signature -> 200 JPEG
    r_card = client.get(f"/api/cases/CASE-20261123-DOC1/documents/{d_card['id']}/prepared", headers=hdrs)
    assert r_card.status_code == 200
    assert r_card.mimetype == "image/jpeg"
    assert len(r_card.data) < 512000

    r_sig = client.get(f"/api/cases/CASE-20261123-DOC1/documents/{d_sig['id']}/prepared", headers=hdrs)
    assert r_sig.status_code == 200
    assert r_sig.mimetype == "image/jpeg"
    assert len(r_sig.data) < 51200

    # Cross-case document fetch -> 403 forbidden!
    r_forbidden = client.get(f"/api/cases/CASE-20261123-DOC1/documents/{d_other['id']}/prepared", headers=hdrs)
    assert r_forbidden.status_code == 403

    # file_attach_attempt audit event accepted
    r_ev = client.post(
        "/api/cases/CASE-20261123-DOC1/events",
        headers={**hdrs, "Content-Type": "application/json"},
        json={"event_type": "file_attach_attempt", "review_version": 1, "detail": "Attached photo"},
    )
    assert r_ev.status_code == 201


def test_whatsapp_customer_burst_groups_all_4_files_and_splits_customer_2(tmp_cfg):
    """Simulate real service desk: 4 WhatsApp .jpeg files for Customer 1 group into
    Case #1 (Ready_signature_1.jpg, Ready_photo_1.jpg, Ready_10th_memo_1.jpg, Ready_aadhaar_1.jpg),
    then Customer 2's Aadhaar with different identity automatically starts Case #2 (Ready_aadhaar_2.jpg).
    """
    from app.services.ingest import ingest_file

    cfg = _make_cfg(tmp_cfg)

    # Customer 1 — File 1: Signature (generic WhatsApp .jpeg)
    f_sig = cfg.inbox_dir / "WhatsApp Image 2026-11-23 at 10.21.32 PM.jpeg"
    _synth_signature_on_desk().save(f_sig, "JPEG", quality=92)
    j1 = ingest_file(f_sig, cfg, sleep_fn=lambda s: None)
    d1 = prepare_document(j1["staged_path"], cfg, job_id=j1["id"])
    run_extraction(d1["id"], cfg, ocr_fn=lambda im: ("", 0.0))

    # Customer 1 — File 2: Passphoto (labeled photo for deterministic cross-env testing)
    f_photo = cfg.inbox_dir / "WhatsApp_passphoto_102134.jpeg"
    Image.new("RGB", (413, 531), (200, 215, 230)).save(f_photo, "JPEG", quality=90)
    j2 = ingest_file(f_photo, cfg, sleep_fn=lambda s: None)
    d2 = prepare_document(j2["staged_path"], cfg, job_id=j2["id"])
    run_extraction(d2["id"], cfg, ocr_fn=lambda im: ("", 0.0))

    # Customer 1 — File 3: 10th SSC Grade Memo (generic WhatsApp .jpeg)
    f_ssc = cfg.inbox_dir / "WhatsApp Image 2026-11-23 at 10.21.35 PM.jpeg"
    _synth_card_on_textured_bg(800, 1000).save(f_ssc, "JPEG", quality=92)
    j3 = ingest_file(f_ssc, cfg, sleep_fn=lambda s: None)
    d3 = prepare_document(j3["staged_path"], cfg, job_id=j3["id"])
    ssc_ocr = lambda im: (
        "BOARD OF SECONDARY EDUCATION TELANGANA STATE\n"
        "Candidate's Name: RAVI KUMAR\n"
        "Father's Name: VENKATESH RAO\n"
        "Date of Birth: 15/08/2001\n"
        "TELUGU ENGLISH MATHS SCIENCE SOCIAL GRADE POINTS CGPA 9.5\n",
        0.91,
    )
    run_extraction(d3["id"], cfg, ocr_fn=ssc_ocr)

    # Customer 1 — File 4: Aadhaar Card (generic WhatsApp .jpeg)
    f_adh = cfg.inbox_dir / "WhatsApp Image 2026-11-23 at 10.21.36 PM.jpeg"
    _synth_phone_screenshot_card().save(f_adh, "JPEG", quality=92)
    j4 = ingest_file(f_adh, cfg, sleep_fn=lambda s: None)
    d4 = prepare_document(j4["staged_path"], cfg, job_id=j4["id"])
    adh_ocr = lambda im: (
        "Government of India\nAadhaar\nRavi Kumar\nYear of Birth: 2001\nMALE\n4567 8901 2345\n",
        0.90,
    )
    run_extraction(d4["id"], cfg, ocr_fn=adh_ocr)

    # Customer 2 — Different person dropped within the same 2-minute window MUST split into Case #2!
    f_c2 = cfg.inbox_dir / "WhatsApp Image 2026-11-23 at 10.21.50 PM.jpeg"
    c2_img = _synth_phone_screenshot_card()
    ImageDraw.Draw(c2_img).rectangle([10, 10, 20, 20], fill=(12, 34, 56))  # unique hash
    c2_img.save(f_c2, "JPEG", quality=92)
    j5 = ingest_file(f_c2, cfg, sleep_fn=lambda s: None)
    d5 = prepare_document(j5["staged_path"], cfg, job_id=j5["id"])
    c2_ocr = lambda im: (
        "Government of India\nAadhaar\nAnitha Reddy\nDOB: 22/04/1998\nFEMALE\n9876 5432 1098\n",
        0.92,
    )
    run_extraction(d5["id"], cfg, ocr_fn=c2_ocr)

    conn = connect(cfg.db_path)
    try:
        cases = conn.execute("SELECT * FROM cases ORDER BY id").fetchall()
        assert len(cases) == 2
        c1_id, c2_id = cases[0]["id"], cases[1]["id"]

        rows = {
            r["id"]: r
            for r in conn.execute("SELECT * FROM documents ORDER BY id").fetchall()
        }
        # All 4 Customer 1 files grouped into Case #1
        for did in (d1["id"], d2["id"], d3["id"], d4["id"]):
            assert rows[did]["case_id"] == c1_id
        # Customer 2 isolated in Case #2
        assert rows[d5["id"]]["case_id"] == c2_id

        # Check exact clean Inbox filenames and byte targets
        assert Path(rows[d1["id"]]["inbox_export_path"]).name == "Ready_signature_1.jpg"
        assert rows[d1["id"]]["byte_size_prepared"] < 51200

        assert Path(rows[d2["id"]]["inbox_export_path"]).name == "Ready_photo_1.jpg"
        assert rows[d2["id"]]["byte_size_prepared"] < 51200

        assert Path(rows[d3["id"]]["inbox_export_path"]).name == "Ready_10th_memo_1.jpg"
        assert rows[d3["id"]]["byte_size_prepared"] < 204800

        assert Path(rows[d4["id"]]["inbox_export_path"]).name == "Ready_aadhaar_1.jpg"
        assert rows[d4["id"]]["byte_size_prepared"] < 512000

        assert Path(rows[d5["id"]]["inbox_export_path"]).name == "Ready_aadhaar_2.jpg"
        assert rows[d5["id"]]["byte_size_prepared"] < 512000
    finally:
        conn.close()


def test_tr_pan_ll_dl_classification_and_200kb_naming(tmp_cfg):
    from app.services.classify import classify_document
    from app.services.fields import extract_candidates, load_field_rules
    from app.services.ingest import ingest_file

    cfg = _make_cfg(tmp_cfg)
    rules = load_field_rules(cfg)

    samples = [
        ("tr.jpeg", "TEMPORARY CERTIFICATE OF REGISTRATION\nForm 19\nTR No: TS08TR9876\nOwner Name: KIRAN KUMAR\nChassis No: ABC123456\nEngine No: ENG999", "tr_document", "Ready_tr_1.jpg"),
        ("pan.jpeg", "INCOME TAX DEPARTMENT\nPermanent Account Number Card\nABCDE1234F\nName: KIRAN KUMAR\nFather's Name: SURESH KUMAR", "pan", "Ready_pan_1.jpg"),
        ("ll.jpeg", "GOVERNMENT OF TELANGANA\nFORM 3 LEARNER'S LICENCE\nLicence Number LLRTG008210082024\nName: KIRAN KUMAR", "learner_licence", "Ready_ll_1.jpg"),
        ("dl.jpeg", "INDIAN UNION DRIVING LICENCE\nTELANGANA STATE\nTG00820250002043\nName: KIRAN KUMAR", "driving_licence", "Ready_dl_1.jpg"),
    ]
    for idx, (fname, txt, expected_cat, expected_ready_name) in enumerate(samples):
        cls = classify_document("WhatsApp_Image.jpeg", ".jpeg", ocr_hints={"text": txt})
        assert cls.category == expected_cat

        raw = cfg.inbox_dir / f"WhatsApp_Image_{idx}.jpeg"
        img = _synth_card_on_textured_bg(800, 600)
        ImageDraw.Draw(img).rectangle([5 + idx * 15, 5, 15 + idx * 15, 15], fill=(idx * 40, 20, 90))
        img.save(raw, "JPEG", quality=92)
        job = ingest_file(raw, cfg, sleep_fn=lambda s: None)
        doc = prepare_document(job["staged_path"], cfg, job_id=job["id"])
        run_extraction(doc["id"], cfg, ocr_fn=lambda im, t=txt: (t, 0.90))

        conn = connect(cfg.db_path)
        try:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc["id"],)).fetchone()
            assert row["byte_size_prepared"] < 204800
            assert Path(row["inbox_export_path"]).name == expected_ready_name
        finally:
            conn.close()

    # Verify TR number extracted cleanly as vehicle_reg_no
    tr_cands = {c["field_key"]: c["value"] for c in extract_candidates([{"page": 1, "text": samples[0][1], "method": "ocr"}], rules)}
    assert tr_cands["vehicle_reg_no"] == "TS08TR9876"
    assert tr_cands["full_name"] == "KIRAN KUMAR"


def test_slow_ocr_does_not_split_same_customer_burst_and_telangana_ssc_ocr_typos(tmp_cfg):
    """Regression:
    1. Two WhatsApp images downloaded from the same second share the exact same
       filename (e.g. Photo + Aadhaar both named '10.21.34 PM.jpeg') -> BOTH ingest.
    2. Even when OCR on a dense 10th SSC memo finishes 5+ minutes later (outside
       inbox_batch_window_sec of wall-clock time), burst grouping compares intake
       arrival timestamps so all 4 files stay in ONE single Case #1.
    3. Telangana SSC memo OCR label typos ('CANDAIUSNAME', 'FATHIRS NAME',
       'DATE of nirth') still extract full_name, father_name, dob, and qualification.
    """
    from app.services.fields import extract_candidates, load_field_rules
    from app.services.ingest import ingest_file

    cfg = _make_cfg(tmp_cfg, inbox_batch_window_sec=120.0)
    rules = load_field_rules(cfg)

    # Verify Telangana SSC OCR-typo parser directly
    ssc_text = (
        "BOARD OF SECONDARY EDUCATION TELANGANA STATE\n"
        "S.S.C. PUBLIC EXAMINATIONS\n"
        "Memorandum of Grades\n"
        "CANDAIUSNAME RAVI KUMAR\n"
        "FATHIRS NAME VENKATESH RAO\n"
        "SBOTSERS NAME LAKSHMI DEVI\n"
        "SCBOOL NAMIE ZPHS HIGH SCHOOL\n"
        "DATE of nirth 15/08/2001 FIFTEEN AUGUST TWO THOUSAND ONE\n"
        "TELUGU HINDI ENGLISH MATHEMATICS SCIENCE SOCIAL GRADE POINTS CGPA 9.2\n"
    )
    c_ssc = {c["field_key"]: c["value"] for c in extract_candidates([{"page": 1, "text": ssc_text, "method": "ocr"}], rules)}
    assert c_ssc["full_name"] == "RAVI KUMAR"
    assert c_ssc["father_name"] == "VENKATESH RAO"
    assert c_ssc["dob"] == "2001-08-15"
    assert c_ssc["qualification"] == "10th Standard / SSC"

    # Simulate all 4 files arriving within 25s (including SAME WhatsApp filename for Photo + Aadhaar)
    same_wa = "WhatsApp Image 2026-11-23 at 10.21.34 PM.jpeg"
    f_photo = cfg.inbox_dir / same_wa
    Image.new("RGB", (413, 531), (200, 215, 230)).save(f_photo, "JPEG", quality=90)
    j1 = ingest_file(f_photo, cfg, sleep_fn=lambda s: None)
    d1 = prepare_document(j1["staged_path"], cfg, job_id=j1["id"], doctype_hint="photo")

    # Aadhaar arrives 8s later with the EXACT SAME filename!
    f_adh = cfg.inbox_dir / same_wa
    _synth_phone_screenshot_card().save(f_adh, "JPEG", quality=92)
    j2 = ingest_file(f_adh, cfg, sleep_fn=lambda s: None)
    assert j2["id"] != j1["id"] and j2["state"] == "ready"
    d2 = prepare_document(j2["staged_path"], cfg, job_id=j2["id"])

    f_ssc = cfg.inbox_dir / "WhatsApp Image 2026-11-23 at 10.21.35 PM.jpeg"
    _synth_card_on_textured_bg(800, 1000).save(f_ssc, "JPEG", quality=92)
    j3 = ingest_file(f_ssc, cfg, sleep_fn=lambda s: None)
    d3 = prepare_document(j3["staged_path"], cfg, job_id=j3["id"])

    f_sig = cfg.inbox_dir / "WhatsApp Image 2026-11-23 at 10.21.32 PM.jpeg"
    _synth_signature_on_desk().save(f_sig, "JPEG", quality=92)
    j4 = ingest_file(f_sig, cfg, sleep_fn=lambda s: None)
    d4 = prepare_document(j4["staged_path"], cfg, job_id=j4["id"])

    # Backdate intake arrival timestamps to 10 minutes ago (all 4 within 25 seconds of each other)
    # to prove slow OCR (finishing 10 minutes after arrival) NEVER breaks the burst window!
    conn = connect(cfg.db_path)
    try:
        ts = [
            "2026-11-23T10:00:00+00:00",
            "2026-11-23T10:00:08+00:00",
            "2026-11-23T10:00:19+00:00",
            "2026-11-23T10:00:25+00:00",
        ]
        for j, d, t in zip((j1, j2, j3, j4), (d1, d2, d3, d4), ts):
            conn.execute("UPDATE intake_jobs SET created_at = ?, updated_at = ? WHERE id = ?", (t, t, j["id"]))
            conn.execute("UPDATE documents SET created_at = ? WHERE id = ?", (t, d["id"]))
        conn.commit()
    finally:
        conn.close()

    # Now run extraction "10 minutes later" (wall clock >> 120s window!)
    run_extraction(d1["id"], cfg, ocr_fn=lambda im: ("", 0.0))
    run_extraction(
        d2["id"], cfg,
        ocr_fn=lambda im: (
            "Government of India\nAadhaar\nRavi Kumar\nS/O Venkatesh Rao\nDOB: 15/08/2001\n4567 8901 2345\n",
            0.91,
        ),
    )
    run_extraction(d3["id"], cfg, ocr_fn=lambda im: (ssc_text, 0.93))
    run_extraction(d4["id"], cfg, ocr_fn=lambda im: ("", 0.0))

    conn = connect(cfg.db_path)
    try:
        cases = conn.execute("SELECT * FROM cases ORDER BY id").fetchall()
        assert len(cases) == 1, f"expected 1 grouped case, got {len(cases)}"
        c1_id = cases[0]["id"]
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM documents ORDER BY id").fetchall()}
        for did in (d1["id"], d2["id"], d3["id"], d4["id"]):
            assert rows[did]["case_id"] == c1_id
        assert Path(rows[d1["id"]]["inbox_export_path"]).name == "Ready_photo_1.jpg"
        assert Path(rows[d2["id"]]["inbox_export_path"]).name == "Ready_aadhaar_1.jpg"
        assert Path(rows[d3["id"]]["inbox_export_path"]).name == "Ready_10th_memo_1.jpg"
        assert Path(rows[d4["id"]]["inbox_export_path"]).name == "Ready_signature_1.jpg"
    finally:
        conn.close()


def test_fast_classify_exports_to_inbox_before_ocr_and_paddle_uses_native_res(tmp_cfg, monkeypatch):
    """Verify:
    1. Unlabeled WhatsApp signature/aadhaar dropped into Inbox exports Ready_*
       IMMEDIATELY during prepare_document(fast_classify=True) BEFORE run_extraction runs.
    2. Primary PaddleOCR/RapidOCR path receives the untouched native-resolution
       RGB original/cropped image (never resized/upscaled or compressed prepared_path).
    3. SSC fuzzy labels ('CANDMAIUSNAME', 'FATORS NAME', header DOB) and e-Aadhaar
       separate PIN Code / State fallback both extract cleanly.
    """
    import app.services.ocr as ocr_mod
    import app.services.ocr_paddle as paddle_mod
    from app.services.fields import extract_candidates, load_field_rules
    from app.services.ingest import ingest_file

    cfg = _make_cfg(tmp_cfg)
    rules = load_field_rules(cfg)

    # 1. Fast pre-OCR export for unlabeled WhatsApp signature -> Ready_signature_1.jpg immediately!
    raw_sig = cfg.inbox_dir / "WhatsApp Image 2026-11-24 at 07.01.01 PM.jpeg"
    _synth_signature_on_desk().save(raw_sig, "JPEG", quality=92)
    j_sig = ingest_file(raw_sig, cfg, sleep_fn=lambda s: None)
    d_sig = prepare_document(j_sig["staged_path"], cfg, job_id=j_sig["id"], fast_classify=True)
    assert d_sig["doctype"] == "signature"
    assert d_sig["status"] == "ready"
    assert d_sig["byte_size_prepared"] < 51200
    exp_sig = Path(d_sig["inbox_export_path"])
    assert exp_sig.exists() and exp_sig.name == "Ready_signature_1.jpg"

    # 2. Primary PaddleOCR receives crisp native-resolution RGB original (1080x1247, NOT resized)
    native_src = _stage(cfg, "native_aadhaar.jpg", Image.new("RGB", (1080, 1247), (240, 242, 245)))
    seen_dims: list[tuple[tuple[int, int], str]] = []

    def fake_paddle_full(im, lang="en"):
        seen_dims.append((im.size, im.mode))
        return (
            "Government of India\nUnique Identification Authority\n"
            "Ravi Kumar\nDOB: 15/08/1998\nMALE\n1234 5678 9012\n"
            "State: Telangana\nPIN Code: 500010\n"
            "Address:\nC/O Suresh Kumar, 12 MG Road\n",
            0.91,
            [],
            "rapidocr-test",
        )

    monkeypatch.setattr(paddle_mod, "available", lambda: True)
    monkeypatch.setattr(paddle_mod, "run_paddle_ocr_full", fake_paddle_full)
    res = ocr_mod.extract_document_text(native_src, cfg, upscale=True)
    assert res["engine"] == "paddleocr"
    assert seen_dims == [((1080, 1247), "RGB")]

    c_adh = {c["field_key"]: c["value"] for c in extract_candidates(res["pages"], rules)}
    assert c_adh["pres_pincode"] == "500010"
    assert c_adh["pres_state"] == "Telangana"
    assert c_adh["full_name"] == "Ravi Kumar"

    # 3. SSC EN-PP-OCRv5 label variants ('CANDMAIUSNAME', 'FATORS NAME', header DOB)
    ssc_text = (
        "BOARD OF SECONDARY EDUCATION TELANGANA STATE\n"
        "S.S.C. PUBLIC EXAMINATIONS\n"
        "Memorandum of Grades\n"
        "ROLL NUMBER 2029123456\n"
        "CANDMAIUSNAME RAVI KUMAR\n"
        "FATORS NAME VENKATESH RAO\n"
        "MOTHER NAME LAKSHMI DEVI\n"
        "SCHOOL NAME ZPHS HIGH SCHOOL\n"
        "tHITE OE m 15/08/2001 FIFTEEN AUGUST\n"
        "TELUGU HINDI ENGLISH MATHEMATICS SCIENCE SOCIAL GRADE CGPA 9.4\n"
    )
    c_ssc = {c["field_key"]: c["value"] for c in extract_candidates([{"page": 1, "text": ssc_text, "method": "ocr"}], rules)}
    assert c_ssc["full_name"] == "RAVI KUMAR"
    assert c_ssc["father_name"] == "VENKATESH RAO"
    assert c_ssc["dob"] == "2001-08-15"
    assert c_ssc["qualification"] == "10th Standard / SSC"



