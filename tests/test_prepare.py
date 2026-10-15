"""Phase 3: byte limits, originals, EXIF, aspect, PDFs, naming (synthetic only)."""
import hashlib
import json
import random
from pathlib import Path

from PIL import Image

from app.config import load_config
from app.db import connect, init_db
from app.services.ingest import ingest_file
from app.services.prepare import (
    build_prepared_filename,
    prepare_document,
    retry_document,
)


def make_cfg(tmp_cfg, **kw):
    d = dict(tmp_cfg)
    d.update({"stable_checks": 2, "stable_interval_sec": 0.0, "max_attempts": 3})
    d.update(kw)
    cfg = load_config(d)
    cfg.ensure_data_dirs()
    return cfg


def noisy_jpeg(path: Path, w: int, h: int, seed: int = 42) -> Path:
    rng = random.Random(seed)
    data = rng.randbytes(w * h * 3)
    img = Image.frombytes("RGB", (w, h), data)
    img.save(path, "JPEG", quality=95)
    return path


def stage(cfg, name: str, writer) -> Path:
    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    p = staging / name
    writer(p)
    return p


def test_photo_meets_limit_and_preserves_original(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage(cfg, "in_photo.jpg", lambda p: noisy_jpeg(p, 900, 900))
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    doc = prepare_document(src, cfg, applicant_name="Ravi Kumar",
                           case_id="CASE-20261014-001", doctype_hint="photo")
    assert doc["status"] == "ready"
    assert doc["profile_name"] == "photo"
    assert doc["profile_version"] == "2"
    prep = Path(doc["prepared_path"])
    assert prep.exists()
    assert prep.stat().st_size < 51200  # strictly below
    assert doc["byte_size_prepared"] < 51200
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert src.stat().st_size == doc["byte_size_orig"]
    assert doc["sha256_orig"] == before
    assert doc["sha256_prepared"] == hashlib.sha256(prep.read_bytes()).hexdigest()
    assert "Ravi_Kumar_CASE-20261014-001_photo" in prep.name
    assert "originals" not in str(prep)
    with Image.open(prep) as out:
        assert abs((out.size[0] / out.size[1]) - 1.0) < 0.05  # aspect preserved


def test_signature_uses_own_profile(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage(cfg, "in_sig.png", lambda p: noisy_jpeg(p, 900, 300))
    doc = prepare_document(src, cfg, applicant_name="Asha", doctype_hint="signature")
    assert doc["status"] == "ready"
    assert doc["profile_name"] == "signature"
    assert Path(doc["prepared_path"]).stat().st_size < 51200


def test_exif_orientation_applied(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage(cfg, "exif.jpg", lambda p: None)
    img = Image.new("RGB", (120, 60), (200, 30, 30))
    exif = img.getexif()
    exif[274] = 6  # rotate 90 CW on display
    img.save(src, "JPEG", quality=90, exif=exif)
    doc = prepare_document(src, cfg, doctype_hint="photo")
    assert doc["status"] == "ready"
    with Image.open(doc["prepared_path"]) as out:
        assert out.size == (60, 120)


def test_rgba_png_converted_safely(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage(cfg, "alpha.png", lambda p: None)
    Image.new("RGBA", (400, 400), (10, 120, 200, 128)).save(src, "PNG")
    doc = prepare_document(src, cfg, doctype_hint="photo")
    assert doc["status"] == "ready"
    with Image.open(doc["prepared_path"]) as out:
        assert out.mode in ("RGB", "L")
        assert out.format == "JPEG"


def test_never_upscales(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage(cfg, "tiny.jpg", lambda p: None)
    Image.new("RGB", (50, 50), (9, 9, 9)).save(src, "JPEG", quality=90)
    doc = prepare_document(src, cfg, doctype_hint="photo")
    assert doc["status"] == "ready"
    with Image.open(doc["prepared_path"]) as out:
        assert out.size == (50, 50)


def test_multipage_pdf_preserved(tmp_cfg):
    pymupdf = __import__("pymupdf")
    cfg = make_cfg(tmp_cfg)
    src = stage(cfg, "ll.pdf", lambda p: None)
    doc_new = pymupdf.open()
    for i in range(3):
        page = doc_new.new_page()
        page.insert_text((72, 72), f"Synthetic LL page {i + 1} with readable text")
    doc_new.save(src)
    doc_new.close()
    doc = prepare_document(src, cfg, applicant_name="Ravi",
                           case_id="CASE-20261014-001", doctype_hint="supporting_ll")
    assert doc["status"] == "ready"
    assert doc["profile_name"] == "supporting_ll"
    assert Path(doc["prepared_path"]).stat().st_size < 204800
    with pymupdf.open(doc["prepared_path"]) as out:
        assert out.page_count == 3
        for i in range(3):
            assert f"page {i + 1}" in out[i].get_text()


def test_unreachable_target_marks_needs_attention(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    prof_file = cfg.data_dir / "profiles.tiny.json"
    prof_file.write_text(json.dumps({
        "version": "t", "photo": {"max_bytes": 1500, "min_quality": 90,
                                  "qualities": [95, 90], "scales": [1.0],
                                  "max_dim": 2000, "min_dim": 100},
    }), encoding="utf-8")
    cfg.processing_profile_path = prof_file
    src = stage(cfg, "big.jpg", lambda p: noisy_jpeg(p, 800, 800))
    doc = prepare_document(src, cfg, doctype_hint="photo")
    assert doc["status"] == "needs_attention"
    assert doc["prepared_path"] is None
    assert "1500" in (doc["error"] or "")
    assert src.exists(), "original retained"


def test_oversize_pdf_marks_needs_attention(tmp_cfg):
    pymupdf = __import__("pymupdf")
    cfg = make_cfg(tmp_cfg)
    prof_file = cfg.data_dir / "profiles.tinypdf.json"
    prof_file.write_text(json.dumps({"version": "t", "supporting_ll": {"max_bytes": 1000}}),
                         encoding="utf-8")
    cfg.processing_profile_path = prof_file
    src = stage(cfg, "big.pdf", lambda p: None)
    d = pymupdf.open()
    page = d.new_page(width=600, height=600)
    rng = random.Random(7)
    pix_img = Image.frombytes("RGB", (500, 500), rng.randbytes(500 * 500 * 3))
    import io as _io
    buf = _io.BytesIO()
    pix_img.save(buf, "JPEG", quality=95)
    page.insert_image(page.rect, stream=buf.getvalue())
    d.save(src)
    d.close()
    doc = prepare_document(src, cfg, doctype_hint="supporting_ll")
    assert doc["status"] == "needs_attention"
    assert "pages kept" in (doc["error"] or "")


def test_unsupported_format_rejected(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage(cfg, "note.txt", lambda p: p.write_bytes(b"hello"))
    doc = prepare_document(src, cfg)
    assert doc["status"] == "needs_attention"
    assert "unsupported" in (doc["error"] or "")
    assert doc["prepared_path"] is None


def test_path_sanitization(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage(cfg, "in.jpg", lambda p: noisy_jpeg(p, 200, 200))
    doc = prepare_document(src, cfg, applicant_name="../../evil",
                           case_id="CASE/x", doctype_hint="photo")
    dest = Path(doc["prepared_path"])
    assert dest.resolve().is_relative_to(cfg.data_dir.resolve())
    assert ".." not in dest.name and "/" not in dest.name


def test_naming_rules():
    assert build_prepared_filename("Ravi", "CASE-20261014-001", None, "photo", ".jpg", 1) == \
        "Ravi_CASE-20261014-001_photo.jpg"
    assert "TG123" in build_prepared_filename("Ravi", "CASE-20261014-001", "TG123", "photo", ".jpg", 1)
    name = build_prepared_filename(None, None, "  ", "photo", ".jpg", 9)
    assert "job9" in name and "TG" not in name


def test_unknown_classification_copies_but_flags_operator(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage(cfg, "IMG_0001.jpg", lambda p: None)
    Image.new("RGB", (200, 200), (5, 5, 5)).save(src, "JPEG", quality=85)
    doc = prepare_document(src, cfg)
    assert doc["doctype"] == "unknown"
    assert doc["status"] == "needs_attention"
    assert Path(doc["prepared_path"]).exists()


def test_retry_document_after_fix(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    prof_file = cfg.data_dir / "profiles.retry.json"
    prof_file.write_text(json.dumps({"version": "r", "photo": {"max_bytes": 1500}}),
                         encoding="utf-8")
    cfg.processing_profile_path = prof_file
    src = stage(cfg, "retry.jpg", lambda p: noisy_jpeg(p, 600, 600))
    doc = prepare_document(src, cfg, doctype_hint="photo")
    assert doc["status"] == "needs_attention"
    prof_file.write_text(json.dumps({"version": "r2"}), encoding="utf-8")
    doc2 = retry_document(doc["id"], cfg)
    assert doc2["status"] == "ready"
    assert Path(doc2["prepared_path"]).stat().st_size < 51200


def test_pipeline_ingest_then_prepare_links_job(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    inbox_file = cfg.inbox_dir / "Ravi_photo.jpg"
    noisy_jpeg(inbox_file, 700, 700)
    job = ingest_file(inbox_file, cfg, sleep_fn=lambda s: None)
    assert job["state"] == "ready"
    doc = prepare_document(job["staged_path"], cfg, job_id=job["id"],
                           applicant_name="Ravi", doctype_hint="photo")
    assert doc["intake_job_id"] == job["id"]
    conn = connect(cfg.db_path)
    try:
        rows = conn.execute("SELECT * FROM documents").fetchall()
        assert len(rows) == 1
    finally:
        conn.close()
    init_db(cfg.db_path)  # restart-safe: rows survive reconnect
    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 1
    finally:
        conn.close()
