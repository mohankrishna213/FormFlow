"""Phase 3 optional PaddleOCR adapter tests (fake engines; no package needed).

PaddleOCR is NOT installed in the test environment and must never be a
hard dependency: every test here works with fake modules/engines or the
honest 'unavailable' path. A successful run is only ever labelled
``paddleocr`` when an engine actually executed.
"""
import sys
import types

import pytest
from PIL import Image

import app.services.ocr_paddle as ocr_paddle

FAKE_RESULT_3X = [{"rec_texts": ["Father's Name: Suresh Kumar",
                                 "App.No 3542873326"],
                   "rec_scores": [0.97, 0.91]}]
FAKE_RESULT_2X = [[[[0, 0], [1, 0], [1, 1], [0, 1]],
                   ("Name: Ravi Kumar", 0.85)]]


@pytest.fixture(autouse=True)
def _clean_engine_cache():
    ocr_paddle._reset_engine_cache()
    yield
    ocr_paddle._reset_engine_cache()


def test_availability_and_provenance_shape():
    assert isinstance(ocr_paddle.available(), bool)
    prov = ocr_paddle.paddle_provenance()
    assert prov["available"] is ocr_paddle.available()
    assert "paddleocr_version" in prov and "paddlepaddle_version" in prov
    if not prov["available"]:
        assert isinstance(prov["error"], str) and prov["error"]


def test_extract_lines_3x_dict_shape():
    texts, scores = ocr_paddle._extract_lines(FAKE_RESULT_3X)
    assert texts == ["Father's Name: Suresh Kumar", "App.No 3542873326"]
    assert scores == [0.97, 0.91]


def test_extract_lines_2x_nested_shape():
    texts, scores = ocr_paddle._extract_lines(FAKE_RESULT_2X)
    assert texts == ["Name: Ravi Kumar"]
    assert scores == [0.85]


FAKE_RESULT_3X_WITH_BOXES = [
    {
        "rec_texts": ["Ravi Kumar", "2. Name", "App.No 3542873326",
                      "Name of Father"],
        "rec_scores": [0.9, 0.95, 0.8, 0.85],
        "rec_polys": [
            [[200, 100], [320, 100], [320, 130], [200, 130]],
            [[40, 100], [190, 100], [190, 130], [40, 130]],
            [[40, 60], [300, 60], [300, 90], [40, 90]],
            [[40, 200], [300, 200], [300, 230], [40, 230]],
        ],
    },
]


def test_extract_lines_3x_regroups_boxes_into_visual_lines():
    # Same-row label/value boxes merge into one line; lines are ordered
    # top-to-bottom and left-to-right regardless of input order.
    texts, scores = ocr_paddle._extract_lines(FAKE_RESULT_3X_WITH_BOXES)
    assert texts == ["App.No 3542873326", "2. Name Ravi Kumar",
                     "Name of Father"]
    assert scores == pytest.approx([0.9, 0.95, 0.8, 0.85])


def test_extract_lines_2x_merges_same_row_boxes():
    result = [
        [[[40, 100], [190, 100], [190, 130], [40, 130]],
         ("2. Name", 0.95)],
        [[[200, 100], [320, 100], [320, 130], [200, 130]],
         ("Ravi Kumar", 0.9)],
    ]
    texts, scores = ocr_paddle._extract_lines(result)
    assert texts == ["2. Name Ravi Kumar"]
    assert scores == pytest.approx([0.95, 0.9])


def test_engine_for_raises_unavailable_when_import_fails(monkeypatch):
    # None in sys.modules makes 'import paddleocr' raise ImportError
    # deterministically, even if the package is installed on the host.
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    with pytest.raises(ocr_paddle.PaddleUnavailableError):
        ocr_paddle._engine_for("en")


def test_engine_for_modern_signature_and_cache(monkeypatch):
    class FakeOCR:
        instances = []

        def __init__(self, **kw):
            FakeOCR.instances.append(kw)

        def predict(self, arr):
            return FAKE_RESULT_3X

    mod = types.ModuleType("paddleocr")
    mod.PaddleOCR = FakeOCR
    monkeypatch.setitem(sys.modules, "paddleocr", mod)
    engine = ocr_paddle._engine_for("en")
    assert isinstance(engine, FakeOCR)
    assert FakeOCR.instances[0]["use_textline_orientation"] is False
    assert FakeOCR.instances[0]["enable_mkldnn"] is False
    assert ocr_paddle._engine_for("en") is engine  # cached per process

    texts, scores = ocr_paddle._run_engine(
        engine, Image.new("RGB", (8, 8), "white"))
    assert texts == "Father's Name: Suresh Kumar\nApp.No 3542873326"
    assert scores == pytest.approx((0.97 + 0.91) / 2)


def test_engine_for_falls_back_to_2x_signature(monkeypatch):
    class OldStyleOCR:
        def __init__(self, **kw):
            if "use_textline_orientation" in kw:
                raise TypeError(
                    "unexpected keyword argument 'use_textline_orientation'")
            self.kw = kw

        def ocr(self, arr, cls=True):
            return FAKE_RESULT_2X

    mod = types.ModuleType("paddleocr")
    mod.PaddleOCR = OldStyleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", mod)
    engine = ocr_paddle._engine_for("en")
    assert isinstance(engine, OldStyleOCR)
    assert engine.kw["use_angle_cls"] is True
    assert engine.kw["enable_mkldnn"] is False

    texts, scores = ocr_paddle._run_engine(
        engine, Image.new("RGB", (8, 8), "white"))
    assert texts == "Name: Ravi Kumar"
    assert scores == pytest.approx(0.85)


def test_make_paddle_ocr_fn_uses_engine(monkeypatch):
    class FakeOCR:
        def predict(self, arr):
            return FAKE_RESULT_3X

    monkeypatch.setattr(ocr_paddle, "_ENGINE", FakeOCR())
    fn = ocr_paddle.make_paddle_ocr_fn()
    text, conf = fn(Image.new("RGB", (8, 8), "white"))
    assert "Father's Name" in text
    assert 0.0 <= conf <= 1.0


def test_column_aware_grouping_separates_two_column_aadhaar_without_breaking_tables():
    # Clean 2-column e-Aadhaar layout (w=1080): left card x=40..480, right back card x=580..1020
    two_col_entries = [
        ((40, 100, 400, 130), "Government of India", 0.95),
        ((40, 150, 320, 180), "Ravi Kumar", 0.95),
        ((40, 200, 340, 230), "DOB: 15/08/1998", 0.95),
        ((40, 250, 200, 280), "MALE", 0.95),
        ((40, 300, 360, 330), "1234 5678 9012", 0.95),
        ((580, 100, 760, 130), "Address:", 0.95),
        ((580, 150, 960, 180), "C/O Suresh Kumar", 0.95),
        ((580, 200, 980, 230), "12 MG Road, Alwal", 0.95),
        ((580, 250, 940, 280), "Medchal, Telangana", 0.95),
        ((580, 300, 820, 330), "PIN Code: 500010", 0.95),
    ]
    lines = ocr_paddle._group_entries_into_lines(two_col_entries, img_width=1080)
    # Left column and right column must NEVER be merged onto the same line!
    assert "Ravi Kumar" in lines
    assert "MALE" in lines
    assert "Address:" in lines
    assert "C/O Suresh Kumar" in lines
    assert not any("MALE" in ln and "Address" in ln for ln in lines)

    # Single-column table layout (e.g. 10th SSC memo with spanning header/subject lines)
    # must keep left label + right value on the SAME line.
    table_entries = [
        ((100, 50, 980, 80), "BOARD OF SECONDARY EDUCATION TELANGANA STATE", 0.95),
        ((150, 100, 920, 130), "S.S.C. PUBLIC EXAMINATIONS", 0.95),
        ((180, 150, 900, 180), "MEMORANDUM OF GRADES", 0.95),
        ((100, 200, 900, 230), "REGULAR CANDIDATE CERTIFICATE", 0.95),
        ((100, 250, 900, 280), "CURRICULAR SUBJECTS AND GRADES", 0.95),
        ((60, 320, 340, 350), "Candidate's Name", 0.92),
        ((420, 320, 720, 350), "RAVI KUMAR", 0.95),
    ]
    t_lines = ocr_paddle._group_entries_into_lines(table_entries, img_width=1080)
    assert "Candidate's Name RAVI KUMAR" in t_lines
