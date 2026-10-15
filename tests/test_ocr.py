"""OCR text source: embedded text first, Tesseract fallback (synthetic only)."""
import hashlib
from pathlib import Path

import pytest

from app.config import load_config
from app.services.ocr import (
    OcrError,
    OcrUnavailableError,
    extract_document_text,
    preprocess_image,
    tesseract_exe,
)


def make_cfg(tmp_cfg, **kw):
    d = dict(tmp_cfg)
    d.update(kw)
    cfg = load_config(d)
    cfg.ensure_data_dirs()
    return cfg


def text_pdf(path: Path, lines: list[str]) -> Path:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for ln in lines:
        page.insert_text((72, y), ln)
        y += 20
    doc.save(path)
    doc.close()
    return path


def blank_pdf(path: Path, n: int = 1) -> Path:
    import pymupdf

    doc = pymupdf.open()
    for _ in range(n):
        doc.new_page()
    doc.save(path)
    doc.close()
    return path


def test_text_pdf_bypasses_ocr(tmp_cfg, monkeypatch):
    import pytesseract

    def _boom(*a, **k):
        raise AssertionError("OCR must not run for text PDFs")

    monkeypatch.setattr(pytesseract, "image_to_string", _boom)
    monkeypatch.setattr(pytesseract, "image_to_data", _boom)
    cfg = make_cfg(tmp_cfg)
    src = text_pdf(cfg.data_dir / "t.pdf", ["Name: Ravi Kumar", "DOB: 12-05-1998"])
    result = extract_document_text(src, cfg)
    assert result["source"] == "text"
    assert "Ravi Kumar" in result["pages"][0]["text"]


def test_scanned_pdf_uses_ocr_per_page(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = blank_pdf(cfg.data_dir / "scan.pdf", 2)
    seen = []

    def fake_ocr(img):
        seen.append(img.size)
        return "Name: Asha Rao", 0.9

    result = extract_document_text(src, cfg, ocr_fn=fake_ocr)
    assert result["source"] == "ocr"
    assert len(result["pages"]) == 2
    assert all(p["method"] == "ocr" for p in result["pages"])
    assert len(seen) == 2


def test_mixed_pdf(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    import pymupdf

    src = cfg.data_dir / "mixed.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Name: Ravi Kumar with plenty of embedded text here")
    doc.new_page()
    doc.save(src)
    doc.close()
    result = extract_document_text(
        src, cfg, ocr_fn=lambda img: ("DOB: 01-01-2000", 0.8))
    assert result["source"] == "mixed"
    assert [p["method"] for p in result["pages"]] == ["text", "ocr"]


def test_image_ocr_mocked(tmp_cfg):
    from PIL import Image

    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "img.png"
    Image.new("RGB", (300, 100), (255, 255, 255)).save(src, "PNG")
    result = extract_document_text(src, cfg, ocr_fn=lambda img: ("Hello", 0.7))
    assert result["source"] == "ocr"
    assert result["pages"][0]["page"] == 1


def test_missing_tesseract_is_setup_error(tmp_cfg, monkeypatch):
    import app.services.ocr as ocr_mod

    monkeypatch.setattr(ocr_mod, "_COMMON_WINDOWS_PATHS", ())
    cfg = make_cfg(tmp_cfg, tesseract_cmd="tesseract-definitely-missing-xyz")
    with pytest.raises(OcrUnavailableError, match="FORMFLOW_TESSERACT_CMD"):
        tesseract_exe(cfg)
    from PIL import Image

    src = cfg.data_dir / "img.png"
    Image.new("RGB", (100, 100)).save(src, "PNG")
    with pytest.raises(OcrUnavailableError):
        extract_document_text(src, cfg)


def test_preprocess_keeps_original_untouched(tmp_cfg):
    from PIL import Image

    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "orig.png"
    Image.new("RGB", (120, 60), (10, 20, 30)).save(src, "PNG")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    with Image.open(src) as im:
        im.load()
        preprocess_image(im.copy(), binarize=True)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


def test_unsupported_extension(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "note.txt"
    src.write_bytes(b"hello")
    with pytest.raises(OcrError, match="unsupported"):
        extract_document_text(src, cfg)
