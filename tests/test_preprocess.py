"""Phase 2 OpenCV preprocessing tests (synthetic data only, no PII).

Covers perspective correction, safe fallbacks, source preservation,
truthful benchmark labels, redaction, and the unavailable-backend path.
"""
import hashlib
import json

import pytest

from app.config import load_config
from app.services.benchmark import (
    CRITICAL_FIELDS,
    benchmark_file,
    compare_variants,
    summarize,
)
from app.services.fields import load_field_rules

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


def make_cfg(tmp_cfg, **kw):
    d = dict(tmp_cfg)
    d.update(kw)
    cfg = load_config(d)
    cfg.ensure_data_dirs()
    return cfg


def rules(tmp_cfg):
    return load_field_rules(make_cfg(tmp_cfg))


def text_doc(h=800, w=600, lines=12):
    """White page with printed text lines on a dark surround."""
    canvas = np.zeros((h + 80, w + 80), np.uint8)
    canvas[40:40 + h, 40:40 + w] = 255
    img = canvas.copy()
    for i in range(lines):
        y = 90 + i * 55
        cv2.putText(img, f"Name: Test Line {i}", (80, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2)
    return img


def test_quad_detection_recovers_warped_page(tmp_cfg):
    from app.services.preprocess import detect_page_quad, opencv_cfg

    doc = text_doc()
    h, w = doc.shape
    src = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    dst = np.float32([[30, 20], [w - 40, 45], [w - 15, h - 25], [15, h - 40]])
    warped = cv2.warpPerspective(doc, cv2.getPerspectiveTransform(src, dst),
                                 (w, h), borderValue=0)
    quad, diag = detect_page_quad(warped, opencv_cfg({"opencv": {}}))
    assert diag["attempted"] is True
    assert quad is not None and diag["found"] is True
    assert diag["confidence"] >= 0.6
    assert quad.shape == (4, 2)
    assert ((quad[:, 0] >= 0) & (quad[:, 0] < w)).all()
    assert ((quad[:, 1] >= 0) & (quad[:, 1] < h)).all()


def test_quad_fallback_on_blank(tmp_cfg):
    from app.services.preprocess import detect_page_quad, opencv_cfg

    blank = np.full((500, 400), 200, np.uint8)
    quad, diag = detect_page_quad(blank, opencv_cfg({"opencv": {}}))
    assert quad is None and diag["found"] is False
    assert diag["reason"]


def test_rectify_and_deskew_guards(tmp_cfg):
    from PIL import Image

    from app.services.preprocess import prepare_variant_image

    # Blank: every geometric step must fall back, output stays valid.
    blank = Image.new("RGB", (500, 400), (200, 200, 200))
    out, diag = prepare_variant_image(blank, "opencv-gray",
                                      {"upscale": False, "opencv": {}})
    assert out.mode == "L"
    assert out.size == (500, 400)
    ops = {s["op"]: s for s in diag["steps"]}
    assert ops["page-detect"]["found"] is False
    assert all(s["applied"] is False for s in diag["steps"]
               if s["op"] in ("perspective",))
    # Small rotation is corrected; huge skew is skipped.
    doc = text_doc()
    h, w = doc.shape
    for deg, expect in ((5.0, True), (40.0, False)):
        mat = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
        rot = cv2.warpAffine(doc, mat, (w, h), borderValue=0)
        from app.services.preprocess import deskew, opencv_cfg

        fixed, d = deskew(rot, opencv_cfg({"opencv": {}}))
        assert d["attempted"] is True
        assert d["applied"] is expect
        if expect:
            _, d2 = deskew(fixed, opencv_cfg({"opencv": {}}))
            assert abs(d2.get("detected_angle", 99)) < 1.0


def test_source_file_never_modified(tmp_cfg):
    from PIL import Image

    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "orig.png"
    Image.new("RGB", (400, 300), (120, 130, 140)).save(src, "PNG")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    ref = {k: None for k in CRITICAL_FIELDS}
    for v in ("baseline", "opencv-gray", "opencv-thresh"):
        benchmark_file(src, ref, cfg, preprocess_variant=v,
                       ocr_fn=lambda img: ("hello", 0.5))
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


def test_thresh_variant_is_binary_and_labels_truthful(tmp_cfg):
    from PIL import Image

    cfg = make_cfg(tmp_cfg)
    grad = Image.new("RGB", (300, 200))
    grad.putdata([(x % 256, x % 256, x % 256)
                  for x in range(300 * 200)])
    src = cfg.data_dir / "grad.png"
    grad.save(src, "PNG")
    ref = {k: None for k in CRITICAL_FIELDS}
    seen = {}

    def spy(img):
        seen["mode"] = img.mode
        seen["nunique"] = len(set(img.tobytes()))
        return "hello", 0.5

    r_gray = benchmark_file(src, ref, cfg, preprocess_variant="opencv-gray",
                            ocr_fn=spy)
    assert r_gray["preprocess"]["variant"] == "opencv-gray"
    assert seen["mode"] == "L" and seen["nunique"] > 2
    r_thr = benchmark_file(src, ref, cfg, preprocess_variant="opencv-thresh",
                           ocr_fn=spy)
    assert r_thr["preprocess"]["variant"] == "opencv-thresh"
    assert seen["nunique"] <= 2  # actually thresholded
    r_base = benchmark_file(src, ref, cfg, preprocess_variant="baseline",
                            ocr_fn=spy)
    assert r_base["preprocess"]["variant"] == "baseline"
    with pytest.raises(ValueError, match="unsupported preprocess_variant"):
        benchmark_file(src, ref, cfg, preprocess_variant="opencv-magic",
                       ocr_fn=spy)


def test_variant_results_stay_redacted(tmp_cfg):
    from PIL import Image

    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "doc.png"
    Image.new("RGB", (400, 300), (255, 255, 255)).save(src, "PNG")
    ref = {"full_name": "Ravi Kumar", "father_name": None,
           "dob": "1995-08-17", "licence_no": None,
           "application_no": None, "address": None}
    for v in ("opencv-gray", "opencv-thresh"):
        r = benchmark_file(src, ref, cfg, preprocess_variant=v,
                           ocr_fn=lambda img: ("Name: Ravi Kumar", 0.9))
        blob = json.dumps(r)
        assert "Ravi Kumar" not in blob

        def _keys(o):
            if isinstance(o, dict):
                for k, vv in o.items():
                    yield k
                    yield from _keys(vv)
            elif isinstance(o, list):
                for vv in o:
                    yield from _keys(vv)

        assert set(_keys(r)).isdisjoint(
            {"value", "text", "expected", "predicted", "raw"})


def test_preprocess_unavailable_counts_as_miss(tmp_cfg):
    import app.services.preprocess as pre

    from PIL import Image

    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "scan.png"
    Image.new("RGB", (200, 150), (255, 255, 255)).save(src, "PNG")
    ref = {"full_name": "Ravi Kumar", "father_name": None,
           "dob": None, "licence_no": None,
           "application_no": None, "address": None}
    orig = pre._require_cv2

    def _boom():
        from app.services.preprocess import PreprocessUnavailableError

        raise PreprocessUnavailableError("no cv2 here")

    pre._require_cv2 = _boom
    try:
        r = benchmark_file(src, ref, cfg, preprocess_variant="opencv-gray")
    finally:
        pre._require_cv2 = orig
    assert r["failed"] is True
    assert r["failure_kind"] == "preprocess_unavailable"
    assert r["fields"]["full_name"]["expected_present"] is True
    assert r["fields"]["full_name"]["predicted_present"] is False
    assert summarize([r])["backend_failure_rate"] == 1.0


def test_text_pdf_path_preserved_for_opencv_variant(tmp_cfg):
    import pymupdf

    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "t.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Name: Ravi Kumar with embedded text here")
    page.insert_text((72, 92), "Date of Birth: 17-08-1995 and more words")
    doc.save(src)
    doc.close()

    def _boom(img):
        raise AssertionError("OCR must not run for text PDFs")

    r = benchmark_file(src, {k: None for k in CRITICAL_FIELDS}, cfg,
                       preprocess_variant="opencv-gray", ocr_fn=_boom)
    assert r["failed"] is False
    assert [p["method"] for p in r["ocr_stage"]["per_page"]] == ["text"]


def test_compare_variants_same_doc_reference(tmp_cfg):
    from PIL import Image

    cfg = make_cfg(tmp_cfg)
    src = cfg.data_dir / "doc2.png"
    Image.new("RGB", (400, 300), (255, 255, 255)).save(src, "PNG")
    ref = {k: None for k in CRITICAL_FIELDS}
    bundle = compare_variants(
        src, ref, cfg,
        variants=("baseline", "opencv-gray", "opencv-thresh"),
        ocr_fn=lambda img: ("hello", 0.5))
    assert len(bundle["results"]) == 3
    ids = [r["case_id"] for r in bundle["results"]]
    assert len(set(ids)) == 3  # distinguishable without filenames
    assert set(bundle["comparison"]["by_variant"]) == {
        "baseline", "opencv-gray", "opencv-thresh"}
    blob = json.dumps(bundle)
    assert "doc2.png" not in blob


def test_write_preprocess_images_names_and_no_overwrite(tmp_path):
    from PIL import Image

    from app.services.preprocess import write_preprocess_images

    out = tmp_path / "out"
    imgs = {"opencv-gray": Image.new("L", (50, 40), 128),
            "opencv-thresh": Image.new("L", (50, 40), 255)}
    made1 = write_preprocess_images(out, "abc123", imgs)
    made2 = write_preprocess_images(out, "abc123", imgs)
    assert len(made1) == 2 and len(made2) == 2
    assert {m.name for m in made1}.isdisjoint({m.name for m in made2})
    for m in made1 + made2:
        assert m.suffix == ".png" and "abc123" in m.name


def test_opencv_block_leaves_baseline_rules_intact(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    r = rules(tmp_cfg)
    assert "opencv" not in r  # loader ignores it; baseline unaffected
    from app.services.preprocess import file_opencv_block, opencv_cfg

    merged = opencv_cfg({"opencv": file_opencv_block(cfg)})
    assert merged["canny_low"] == 50
    assert merged["adaptive_block_size"] % 2 == 1


def filled_rect(canvas_w, canvas_h, x, y, w, h, fill=255, bg=0, lines=False):
    img = np.full((canvas_h, canvas_w), bg, np.uint8)
    img[y:y + h, x:x + w] = fill
    if lines:
        for i in range(8):
            cv2.putText(img, f"Name: Test {i}", (x + 20, y + 60 + i * 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2)
    return img


def test_rejects_distracting_background_frame(tmp_cfg):
    """Two competing page-sized rectangles: ambiguous, keep original."""
    from app.services.preprocess import detect_page_quad, opencv_cfg

    img = np.zeros((700, 1000), np.uint8)
    img[100:600, 190:490] = 255  # page-like rect, left of centre
    for i in range(8):
        cv2.putText(img, f"Name: Test {i}", (210, 160 + i * 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2)
    img[100:600, 510:810] = 255  # distracting poster-like rect, right
    quad, diag = detect_page_quad(img, opencv_cfg({"opencv": {}}))
    assert quad is None and diag["found"] is False
    assert diag["reason"] == "ambiguous-multiple-quads"


def test_tolerates_shadow_streak(tmp_cfg):
    """A shadow band across the middle must not lose the true page."""
    from app.services.preprocess import detect_page_quad, opencv_cfg

    img = filled_rect(680, 960, 40, 40, 600, 880, lines=True)
    shadow = np.zeros_like(img)
    cv2.fillPoly(shadow, [np.array([[0, 500], [680, 380], [680, 500], [0, 620]])],
                 90)
    mask = shadow > 0
    shaded = img.astype(float)
    shaded[mask] = shaded[mask] * 0.45
    quad, diag = detect_page_quad(shaded.astype(np.uint8),
                                  opencv_cfg({"opencv": {}}))
    assert quad is not None and diag["found"] is True


def test_rejects_near_border_document(tmp_cfg):
    """Page filling the frame is the frame: no correction possible."""
    from app.services.preprocess import detect_page_quad, opencv_cfg

    img = filled_rect(600, 800, 5, 5, 590, 790, lines=True)
    quad, diag = detect_page_quad(img, opencv_cfg({"opencv": {}}))
    assert quad is None and diag["found"] is False
    assert diag["reason"] == "veto:frame-touch"


def test_rejects_occluded_page_corner(tmp_cfg):
    """A covered corner breaks the quadrilateral: keep original pixels."""
    from PIL import Image

    from app.services.preprocess import prepare_variant_image

    img = filled_rect(600, 800, 60, 60, 480, 680, lines=True)
    img[60:360, 60:300] = 0  # occluding block over one corner
    from app.services.preprocess import detect_page_quad, opencv_cfg

    quad, diag = detect_page_quad(img, opencv_cfg({"opencv": {}}))
    assert quad is None and diag["found"] is False
    out, vdiag = prepare_variant_image(Image.fromarray(img),
                                       "opencv-gray",
                                       {"upscale": False, "opencv": {}})
    assert out.size == (600, 800)  # nothing cropped or rewarped
    assert all(s.get("applied", True) is False
               for s in vdiag["steps"] if s["op"] == "perspective")


def test_rejects_off_center_quad(tmp_cfg):
    """A large rectangle at the margin is background, not the page."""
    from app.services.preprocess import detect_page_quad, opencv_cfg

    img = filled_rect(800, 700, 10, 10, 400, 500)
    quad, diag = detect_page_quad(img, opencv_cfg({"opencv": {}}))
    assert quad is None and diag["found"] is False
    assert diag["reason"] == "veto:off-center"
