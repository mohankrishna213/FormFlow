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
from app.config import load_config
from app.services.benchmark import (
    CRITICAL_FIELDS,
    benchmark_file,
    collect_provenance,
    summarize,
)

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


def make_cfg(tmp_cfg):
    cfg = load_config(dict(tmp_cfg))
    cfg.ensure_data_dirs()
    return cfg


def _png(cfg, name="scan.png"):
    src = cfg.data_dir / name
    Image.new("RGB", (300, 100), (255, 255, 255)).save(src, "PNG")
    return src


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


def test_benchmark_paddleocr_unavailable_is_honest(tmp_cfg, monkeypatch):
    def _raise(lang):
        raise ocr_paddle.PaddleUnavailableError("simulated: not installed")

    monkeypatch.setattr(ocr_paddle, "_engine_for", _raise)
    cfg = make_cfg(tmp_cfg)
    ref = {k: None for k in CRITICAL_FIELDS}
    ref["full_name"] = "Ravi Kumar"
    r = benchmark_file(_png(cfg), ref, cfg, backend="paddleocr")
    assert r["failed"] is True
    assert r["failure_kind"] == "unavailable"
    assert r["backend"] == "paddleocr"
    # Failure retained as a miss, never excluded and never a success.
    assert r["fields"]["full_name"]["expected_present"] is True
    assert r["fields"]["full_name"]["exact_match"] is False
    assert summarize([r])["backend_failure_rate"] == 1.0
    blob = str(r)
    assert "simulated: not installed" in blob  # static message, no values


def test_benchmark_paddleocr_with_injected_engine_scores(tmp_cfg, monkeypatch):
    # Success path stays testable without the real package: an engine
    # that actually executes is simulated via the documented ocr_fn seam.
    monkeypatch.setattr(ocr_paddle, "_ENGINE", None)

    class FakeEngine:
        def predict(self, arr):
            return FAKE_RESULT_3X

    monkeypatch.setattr(ocr_paddle, "_engine_for", lambda lang: FakeEngine())
    cfg = make_cfg(tmp_cfg)
    ref = {"full_name": None, "father_name": "Suresh Kumar",
           "dob": None, "licence_no": None, "application_no": "3542873326",
           "address": None}
    r = benchmark_file(_png(cfg, "scan3.png"), ref, cfg, backend="paddleocr")
    assert r["failed"] is False
    assert r["backend"] == "paddleocr"
    assert r["fields"]["father_name"]["exact_match"] is True
    assert r["fields"]["application_no"]["exact_match"] is True


def test_collect_provenance_paddleocr_backend_label(tmp_cfg):
    prov = collect_provenance(make_cfg(tmp_cfg), backend="paddleocr")
    assert prov["backend"] == "paddleocr"
    assert isinstance(prov["paddleocr"]["available"], bool)
    assert isinstance(prov["tesseract"]["available"], bool)
    assert prov["preprocess_variant"] == "baseline"
