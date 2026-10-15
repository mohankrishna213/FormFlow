"""Classification is a suggestion only; low confidence asks the operator."""
from app.services.classify import suggest_doctype


def test_photo_filename():
    doctype, conf, reason = suggest_doctype("Ravi_passport_photo.jpg", ".jpg")
    assert doctype == "photo"
    assert conf >= 0.6


def test_signature_filename():
    doctype, conf, _ = suggest_doctype("applicant_signature.png", ".png")
    assert doctype == "signature"
    assert conf >= 0.6


def test_supporting_pdf_filename():
    doctype, conf, _ = suggest_doctype("aadhaar_card.pdf", ".pdf")
    assert doctype == "supporting_ll"
    assert conf >= 0.6


def test_random_name_unknown_and_asks_operator():
    doctype, conf, reason = suggest_doctype("IMG_20261014_093211.jpg", ".jpg")
    assert doctype == "unknown"
    assert conf < 0.5
    assert "operator" in reason


def test_dimensions_alone_never_decisive():
    doctype, conf, _ = suggest_doctype("scan0001.png", ".png", width=1000, height=300)
    assert doctype == "unknown"


def test_ocr_hints_hook():
    doctype, conf, _ = suggest_doctype(
        "scan0001.pdf", ".pdf", ocr_hints={"page1": "Aadhaar number 1234"})
    assert doctype == "supporting_ll"


def test_suffixed_keywords_match():
    doctype, conf, _ = suggest_doctype("learning-license1.jpeg", ".jpeg")
    assert doctype == "supporting_ll"
    assert conf >= 0.6


def test_short_hints_stay_exact():
    assert suggest_doctype("assignment.jpg", ".jpg")[0] == "unknown"
    assert suggest_doctype("panel-scan.jpg", ".jpg")[0] == "unknown"
