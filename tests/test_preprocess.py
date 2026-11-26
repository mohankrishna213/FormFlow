"""OpenCV preprocessing tests (synthetic data only, no PII).

Covers perspective correction, safe fallbacks, variant output invariants,
redacted diagnostics, and preview naming.
"""
import pytest

from app.config import load_config
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


def test_thresh_variant_is_binary_and_unknown_variant_rejected():
    from PIL import Image

    from app.services.preprocess import prepare_variant_image

    grad = Image.new("RGB", (300, 200))
    grad.putdata([(x % 256, x % 256, x % 256) for x in range(300 * 200)])

    gray_out, gray_diag = prepare_variant_image(grad, "opencv-gray", {"upscale": False, "opencv": {}})
    assert gray_diag["variant"] == "opencv-gray"
    assert gray_out.mode == "L" and len(set(gray_out.tobytes())) > 2

    thr_out, thr_diag = prepare_variant_image(grad, "opencv-thresh", {"upscale": False, "opencv": {}})
    assert thr_diag["variant"] == "opencv-thresh"
    assert len(set(thr_out.tobytes())) <= 2  # actually thresholded

    with pytest.raises(ValueError, match="unknown OpenCV variant"):
        prepare_variant_image(grad, "opencv-magic", {"upscale": False, "opencv": {}})


def test_preprocess_unavailable_raises_cleanly(monkeypatch):
    from PIL import Image

    import app.services.preprocess as pre

    def _boom():
        raise pre.PreprocessUnavailableError("no cv2 here")

    monkeypatch.setattr(pre, "_require_cv2", _boom)
    assert pre.opencv_provenance()["available"] is False
    with pytest.raises(pre.PreprocessUnavailableError):
        pre.prepare_variant_image(Image.new("RGB", (200, 150), "white"), "opencv-gray", {"opencv": {}})


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


def test_rectify_vetoes_content_dropping_quad(tmp_cfg):
    """A quad that would discard readable text keeps the original."""
    from app.services.preprocess import opencv_cfg, rectify_perspective

    img = filled_rect(600, 800, 60, 60, 480, 680, lines=True)
    h, w = img.shape
    quad = np.array([[w * 0.4, 0], [w - 1, 0], [w - 1, h - 1],
                     [w * 0.4, h - 1]], dtype=np.float32)
    out, diag = rectify_perspective(img, quad, opencv_cfg({"opencv": {}}))
    assert diag["applied"] is False
    assert diag["reason"] == "fallback:drops-edgy-content"
    assert out.shape == img.shape


def test_rectify_accepts_full_frame_quad(tmp_cfg):
    """The trivial full-frame quad is not blocked by the guards."""
    from app.services.preprocess import opencv_cfg, rectify_perspective

    img = filled_rect(600, 800, 60, 60, 480, 680, lines=True)
    h, w = img.shape
    quad = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
                    dtype=np.float32)
    out, diag = rectify_perspective(img, quad, opencv_cfg({"opencv": {}}))
    assert diag["applied"] is True
    # Pixel-coordinate convention: quad spans w-1 px, so allow 1px slack.
    assert abs(out.shape[0] - h) <= 2 and abs(out.shape[1] - w) <= 2
