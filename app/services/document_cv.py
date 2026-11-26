"""Document image cropping, deskewing, and cleaning for customer phone captures.

Offline-first OpenCV + Pillow processing for standalone customer uploads:
- Document cards / sheets (Aadhaar, Driving Licence, Learner's Licence, PAN):
  phone screenshot bar removal, excess white-margin trimming (preserving
  multi-card front+back layouts), bright-smooth card quad detection, and
  4-point color perspective rectification with strict content-safety guards.
- Passphotos / Photo IDs (standalone portrait photos):
  phone bar / border trimming, Haar face-centered 3.5:4.5 passport framing
  (when cascade XML is available and the image is not already well-framed),
  or clean portrait-photo boundary fallback.
- Signatures (standalone signatures on paper):
  phone bar trimming, paper isolation, shadow/illumination normalization,
  padded ink-stroke bounding-box crop (never tightly clipped), and gentle
  background whitening preserving natural anti-aliased pen ink.
- Manual crop & rotation support for the operator web dashboard.

Invariants:
- Original files on disk are NEVER modified; all operations work on in-memory
  PIL / numpy copies.
- Uncertain detections always fall back cleanly to the uncropped image.
- Diagnostics contain geometry, flags, and short reasons only (never PII).
- Lazy cv2/numpy imports so FormFlow degrades gracefully if OpenCV is absent.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

DEFAULT_PASSPORT_ASPECT = 3.5 / 4.5  # 0.7778 (standard Indian 35mm x 45mm)


def _load_cv2():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        return cv2, np
    except Exception:
        return None, None


def order_quad_points(pts, np):
    """Order 4 quadrilateral points as [top-left, top-right, bottom-right, bottom-left]."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


_order_points = order_quad_points


def _ensure_rgb_np(img: Image.Image, np):
    work = img if img.mode == "RGB" else img.convert("RGB")
    return np.array(work)


def trim_dark_screenshot_bars(rgb) -> tuple[Any, dict[str, Any]]:
    """Strip top/bottom black phone-gallery or screenshot UI bars.

    Tolerates small white UI icons/pills (e.g. status bar text or 'Crop and adjust')
    embedded inside the dark bars, while refusing to touch solid dark images.
    """
    cv2, np = _load_cv2()
    if cv2 is None:
        return rgb, {"applied": False, "reason": "opencv-unavailable"}

    h, w = rgb.shape[:2]
    if h < 80 or w < 80:
        return rgb, {"applied": False, "reason": "too-small"}

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # A row is content if <65% of pixels are near-black and upper-quartile is visible.
    is_content = ((gray < 28).mean(axis=1) < 0.65) & (np.percentile(gray, 75, axis=1) > 35)
    if not is_content.any():
        return rgb, {"applied": False, "reason": "all-dark-image"}

    # Bridge tiny dark dividers inside real content (up to 2% height)
    k_close = max(3, int(h * 0.02))
    mask = is_content.astype(np.uint8).reshape(-1, 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k_close, 1), np.uint8))
    # Discard thin UI pills floating inside dark bars (<8% height)
    k_open = max(5, int(h * 0.08))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k_open, 1), np.uint8))

    # Pick the largest vertical contiguous span
    num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
    if num <= 1:
        return rgb, {"applied": False, "reason": "no-content-span"}
    best_i = max(range(1, num), key=lambda i: int(stats[i, cv2.CC_STAT_HEIGHT]))
    y0 = int(stats[best_i, cv2.CC_STAT_TOP])
    span_h = int(stats[best_i, cv2.CC_STAT_HEIGHT])
    y1 = y0 + span_h

    trimmed_top = y0
    trimmed_bot = h - y1
    # Only apply if we genuinely stripped dark bars (>= 2.5% of height on top or bottom)
    # and kept a substantial content region (>= 25% of height).
    if (trimmed_top >= int(h * 0.025) or trimmed_bot >= int(h * 0.025)) and span_h >= int(h * 0.25):
        # Verify the removed bands are genuinely dark on average
        removed_parts = []
        if trimmed_top > 0:
            removed_parts.append(gray[:y0, :])
        if trimmed_bot > 0:
            removed_parts.append(gray[y1:, :])
        removed_dark_frac = float(np.mean([float((p < 35).mean()) for p in removed_parts]))
        if removed_dark_frac >= 0.65:
            return rgb[y0:y1, :], {
                "applied": True,
                "reason": "trimmed-dark-bars",
                "trimmed_top": trimmed_top,
                "trimmed_bottom": trimmed_bot,
            }
    return rgb, {"applied": False, "reason": "no-dark-bars"}


def _trim_white_margins(rgb, pad_frac: float = 0.015, min_comp_area_frac: float = 0.02) -> tuple[Any, dict[str, Any]]:
    """Trim excess pure-white outer margins (e.g. CamScanner / A4 scan pages).

    Preserves multi-card sheets (front+back Aadhaar) by taking the union of all
    significant document blocks while ignoring tiny isolated corner watermarks.
    """
    cv2, np = _load_cv2()
    h, w = rgb.shape[:2]
    img_area = float(h * w)
    border_px = np.concatenate([rgb[0, :], rgb[-1, :], rgb[:, 0], rgb[:, -1]], axis=0)
    border_gray = cv2.cvtColor(border_px.reshape(1, -1, 3), cv2.COLOR_RGB2GRAY).flatten()
    if float((border_gray > 235).mean()) < 0.75:
        return rgb, {"applied": False, "reason": "non-white-border"}

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    non_white = (gray < 238).astype(np.uint8) * 255
    kx = max(5, int(w * 0.03))
    ky = max(5, int(h * 0.015))
    merged = cv2.morphologyEx(non_white, cv2.MORPH_CLOSE, np.ones((ky, kx), np.uint8))
    num, _, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)

    boxes = []
    for i in range(1, num):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area >= img_area * min_comp_area_frac:
            x = int(stats[i, cv2.CC_STAT_LEFT])
            y = int(stats[i, cv2.CC_STAT_TOP])
            bw = int(stats[i, cv2.CC_STAT_WIDTH])
            bh = int(stats[i, cv2.CC_STAT_HEIGHT])
            boxes.append((x, y, x + bw, y + bh, area))

    if not boxes:
        return rgb, {"applied": False, "reason": "no-significant-blocks"}

    x0 = max(0, min(b[0] for b in boxes) - int(w * pad_frac))
    y0 = max(0, min(b[1] for b in boxes) - int(h * pad_frac))
    x1 = min(w, max(b[2] for b in boxes) + int(w * pad_frac))
    y1 = min(h, max(b[3] for b in boxes) + int(h * pad_frac))
    kept_area = ((x1 - x0) * (y1 - y0)) / img_area

    if 0.15 <= kept_area <= 0.94:
        return rgb[y0:y1, x0:x1], {
            "applied": True,
            "reason": "white-margin-trim",
            "blocks": len(boxes),
            "kept_area_ratio": round(kept_area, 3),
        }
    return rgb, {"applied": False, "reason": "already-tight"}


def _quad_touches_edges(quad, w: int, h: int, margin_frac: float = 0.025) -> int:
    mx = w * margin_frac
    my = h * margin_frac
    return sum((
        bool((quad[:, 0] <= mx).any()),
        bool((quad[:, 0] >= w - mx).any()),
        bool((quad[:, 1] <= my).any()),
        bool((quad[:, 1] >= h - my).any()),
    ))


def _warp_quad_rgb(rgb, quad, pad_frac: float = 0.015):
    cv2, np = _load_cv2()
    (tl, tr, br, bl) = quad
    w_a = float(np.linalg.norm(br - bl))
    w_b = float(np.linalg.norm(tr - tl))
    h_a = float(np.linalg.norm(tr - br))
    h_b = float(np.linalg.norm(tl - bl))
    dst_w = max(1, int(round(max(w_a, w_b))))
    dst_h = max(1, int(round(max(h_a, h_b))))
    # Slightly expand quad from centroid to avoid clipping edge text
    centroid = quad.mean(axis=0)
    exp_quad = centroid + (quad - centroid) * (1.0 + pad_frac * 2.0)
    dst = np.array(
        [[0, 0], [dst_w - 1, 0], [dst_w - 1, dst_h - 1], [0, dst_h - 1]],
        dtype="float32",
    )
    mat = cv2.getPerspectiveTransform(exp_quad.astype("float32"), dst)
    return cv2.warpPerspective(
        rgb, mat, (dst_w, dst_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _evaluate_mask_candidate(mask, lab_L, hsv_S, h: int, w: int, img_area: float) -> dict[str, Any] | None:
    cv2, np = _load_cv2()
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
    large = [c for c in contours if cv2.contourArea(c) >= img_area * 0.14]
    if not large:
        return None

    # Guard: multi-card layout (e.g. front & back stacked in one photo)
    if len(large) >= 2 and cv2.contourArea(large[1]) >= 0.40 * cv2.contourArea(large[0]):
        pts = np.vstack(large[:2])
        x, y, bw, bh = cv2.boundingRect(pts)
        union_ratio = (bw * bh) / img_area
        if 0.25 <= union_ratio <= 0.92:
            return {"multi": True, "bbox": (x, y, bw, bh), "ratio": union_ratio}
        return None

    c = large[0]
    area = float(cv2.contourArea(c))
    ratio = area / img_area
    if not (0.16 <= ratio <= 0.88):
        return None

    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    quad = None
    for eps in (0.02, 0.03, 0.04, 0.05):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = _order_points(approx.reshape(4, 2).astype("float32"), np)
            break
    if quad is None:
        rect = cv2.minAreaRect(hull)
        quad = _order_points(cv2.boxPoints(rect).astype("float32"), np)

    q_area = max(1.0, float(cv2.contourArea(quad)))
    rect_score = min(area, q_area) / max(area, q_area)
    if rect_score < 0.78:
        return None

    touch = _quad_touches_edges(quad, w, h)
    # Never aggressively warp a card/page already cut off on >=3 image borders
    if touch >= 3:
        return None

    # Interior corner angle check (must look like a rectangle/perspective rectangle)
    devs = []
    for i in range(4):
        p0, p1, p2 = quad[i], quad[(i + 1) % 4], quad[(i + 3) % 4]
        v1, v2 = p1 - p0, p2 - p0
        cos_a = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-9))
        ang = math.degrees(math.acos(max(-1.0, min(1.0, cos_a))))
        devs.append(abs(ang - 90.0))
    angle_score = max(0.0, 1.0 - (sum(devs) / 4.0) / 30.0)
    if angle_score < 0.55:
        return None

    # Centrality guard: document sits near frame centre, not in a corner
    cx, cy = float(quad[:, 0].mean()), float(quad[:, 1].mean())
    off = max(abs(cx - w / 2.0) / w, abs(cy - h / 2.0) / h)
    if off > 0.22:
        return None

    # Foreground vs background separation guard:
    # Prevents clipping shadowed portions of a full-frame white A4 paper (like Form 3 LL).
    inside_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(inside_mask, [quad.astype(np.int32)], 255)
    # Exclude a 2% gutter around the quad border when sampling background
    gutter = max(3, int(min(h, w) * 0.02))
    dilated = cv2.dilate(inside_mask, np.ones((gutter, gutter), np.uint8), iterations=1)
    outside = dilated == 0
    inside = inside_mask > 0
    if not outside.any() or not inside.any():
        return None

    L_in = float(lab_L[inside].mean())
    L_out = float(lab_L[outside].mean())
    S_in = float(hsv_S[inside].mean())
    S_out = float(hsv_S[outside].mean())
    contrast = (L_in - L_out) + 0.5 * (S_out - S_in)

    # If the excluded exterior is itself bright low-saturation paper, do NOT clip it.
    if L_out >= 160.0 and S_out < 35.0:
        return None
    if contrast < 38.0:
        return None

    # Guard against high-contrast dark printed text sitting outside the quad on light paper
    outside_bright_paper = (lab_L > 140) & (hsv_S < 40) & outside
    if float(outside_bright_paper.mean()) > 0.25 and contrast < 65.0:
        return None

    score = 0.40 * rect_score + 0.30 * angle_score + 0.15 * min(1.0, ratio / 0.55) + 0.15 * min(1.0, contrast / 100.0)
    return {
        "multi": False,
        "quad": quad,
        "score": score,
        "ratio": ratio,
        "rect_score": rect_score,
        "contrast": contrast,
        "touch": touch,
    }


def crop_document_card(
    img: Image.Image,
    cfg: dict[str, Any] | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    """Auto-crop and perspective-rectify an ID card or supporting document photo.

    Returns (pil_rgb_image, diagnostics). Original image is never modified.
    """
    cv2, np = _load_cv2()
    orig_w, orig_h = img.size
    diag: dict[str, Any] = {
        "mode": "document",
        "crop_applied": False,
        "method": "none",
        "reason": "no-change",
        "orig_dims": [orig_w, orig_h],
        "out_dims": [orig_w, orig_h],
    }
    if cv2 is None:
        diag["reason"] = "opencv-unavailable"
        return img, diag
    if min(orig_w, orig_h) < 100:
        diag["reason"] = "too-small"
        return img, diag

    rgb = _ensure_rgb_np(img, np)

    # Step 1: Trim black phone screenshot / gallery UI bars
    rgb_work, bar_diag = trim_dark_screenshot_bars(rgb)
    bars_trimmed = bool(bar_diag.get("applied"))
    h, w = rgb_work.shape[:2]
    img_area = float(h * w)

    # Step 2: Uniform white margin trimming (scans, CamScanner, stacked front+back)
    trimmed_white, white_diag = _trim_white_margins(rgb_work)
    if white_diag.get("applied"):
        out_im = Image.fromarray(trimmed_white)
        diag.update(
            crop_applied=True,
            method="white-margin-trim" + ("+bars" if bars_trimmed else ""),
            reason=white_diag["reason"],
            blocks=white_diag.get("blocks", 1),
            out_dims=[out_im.width, out_im.height],
        )
        return out_im, diag

    # Step 3: Multi-strategy bright-document / card quad detection on textured/dark backgrounds
    lab = cv2.cvtColor(rgb_work, cv2.COLOR_RGB2LAB)
    hsv = cv2.cvtColor(rgb_work, cv2.COLOR_RGB2HSV)
    L = lab[:, :, 0].astype(np.float32)
    S = hsv[:, :, 1].astype(np.float32)

    ksize = max(5, (min(h, w) // 45) | 1)
    whiteness = np.clip(L - 0.55 * S, 0, 255).astype(np.uint8)
    w_smooth = cv2.medianBlur(whiteness, ksize)

    # Local texture penalty separates smooth white card from grainy concrete/fabric floors
    mean_L = cv2.blur(L, (11, 11))
    sq_L = cv2.blur(L * L, (11, 11))
    std_L = np.sqrt(np.maximum(0.0, sq_L - mean_L * mean_L))
    bright_smooth = np.clip(L - 0.45 * S - 1.5 * std_L, 0, 255).astype(np.uint8)
    bs_smooth = cv2.medianBlur(bright_smooth, ksize)

    mk = max(9, (min(h, w) // 18) | 1)
    close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (mk, mk))
    open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(5, mk // 2), max(5, mk // 2)))

    raw_masks = []
    _, m1 = cv2.threshold(w_smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    raw_masks.append(("whiteness-otsu", m1))
    _, m2 = cv2.threshold(bs_smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    raw_masks.append(("bright-smooth-otsu", m2))
    thr_hi = max(135, int(np.percentile(bs_smooth, 58)))
    _, m3 = cv2.threshold(bs_smooth, thr_hi, 255, cv2.THRESH_BINARY)
    raw_masks.append(("bright-smooth-hi", m3))

    best_cand = None
    best_strategy = ""
    for strat_name, raw_m in raw_masks:
        m = cv2.morphologyEx(raw_m, cv2.MORPH_CLOSE, close_k)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, open_k)
        cand = _evaluate_mask_candidate(m, L, S, h, w, img_area)
        if cand is None:
            continue
        if cand.get("multi"):
            x, y, bw, bh = cand["bbox"]
            pad = int(min(h, w) * 0.02)
            x0, y0 = max(0, x - pad), max(0, y - pad)
            x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
            out_im = Image.fromarray(rgb_work[y0:y1, x0:x1])
            diag.update(
                crop_applied=True,
                method=f"multi-card-union:{strat_name}",
                reason="multi-card-preserved",
                out_dims=[out_im.width, out_im.height],
            )
            return out_im, diag
        if best_cand is None or cand["score"] > best_cand["score"]:
            best_cand = cand
            best_strategy = strat_name

    if best_cand is not None:
        warped = _warp_quad_rgb(rgb_work, best_cand["quad"], pad_frac=0.015)
        if min(warped.shape[0], warped.shape[1]) >= 80:
            out_im = Image.fromarray(warped)
            diag.update(
                crop_applied=True,
                method=f"perspective:{best_strategy}" + ("+bars" if bars_trimmed else ""),
                reason="card-rectified",
                area_ratio=round(best_cand["ratio"], 3),
                rect_score=round(best_cand["rect_score"], 3),
                out_dims=[out_im.width, out_im.height],
            )
            return out_im, diag

    if bars_trimmed:
        out_im = Image.fromarray(rgb_work)
        diag.update(
            crop_applied=True,
            method="trim-dark-bars",
            reason="screenshot-bars-removed",
            out_dims=[out_im.width, out_im.height],
        )
        return out_im, diag

    diag["reason"] = "kept-original"
    return img, diag


_YUNET_MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "face_detection_yunet_2023mar.onnx"


def _find_haar_face_xml(cv2) -> str | None:
    """Locate an installed OpenCV frontal-face Haar cascade XML if available."""
    data_mod = getattr(cv2, "data", None)
    hc_dir = getattr(data_mod, "haarcascades", None)
    if not hc_dir:
        return None
    base = Path(hc_dir)
    for fname in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml", "haarcascade_frontalface_alt.xml"):
        p = base / fname
        if p.is_file():
            return str(p)
    return None


def _detect_faces(rgb, *, min_face_frac: float = 0.08, score_thresh: float = 0.62) -> list[tuple[int, int, int, int, float]]:
    """Detect faces using OpenCV YuNet ONNX (primary) with Haar Cascade automatic fallback.

    Returns ``[(x, y, w, h, score), ...]`` in pixel coordinates of ``rgb``.
    """
    cv2, np = _load_cv2()
    if cv2 is None:
        return []
    h, w = rgb.shape[:2]
    if min(h, w) < 40:
        return []

    # 1. Primary: YuNet ONNX (~230 KB, vastly better than Haar on printed/angled/low-contrast photos)
    if _YUNET_MODEL_PATH.is_file() and hasattr(cv2, "FaceDetectorYN"):
        try:
            scale = 1.0
            det_rgb = rgb
            if max(h, w) > 960:
                scale = 960.0 / float(max(h, w))
                det_rgb = cv2.resize(
                    rgb,
                    (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            dh, dw = det_rgb.shape[:2]
            bgr = cv2.cvtColor(det_rgb, cv2.COLOR_RGB2BGR)
            detector = cv2.FaceDetectorYN.create(
                str(_YUNET_MODEL_PATH), "", (dw, dh), float(score_thresh), 0.3, 100
            )
            _, raw = detector.detect(bgr)
            faces: list[tuple[int, int, int, int, float]] = []
            if raw is not None and len(raw) > 0:
                inv = 1.0 / scale
                for row in raw:
                    fx = max(0, int(round(float(row[0]) * inv)))
                    fy = max(0, int(round(float(row[1]) * inv)))
                    fw = max(1, int(round(float(row[2]) * inv)))
                    fh = max(1, int(round(float(row[3]) * inv)))
                    sc = float(row[-1])
                    if fw >= 18 and fh >= 18:
                        faces.append((fx, fy, min(fw, w - fx), min(fh, h - fy), sc))
            if faces:
                return faces
        except Exception:
            pass

    # 2. Fallback: OpenCV Haar Cascade
    xml_path = _find_haar_face_xml(cv2)
    if xml_path and hasattr(cv2, "CascadeClassifier"):
        try:
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            cascade = cv2.CascadeClassifier(xml_path)
            min_face = max(24, int(min(h, w) * min_face_frac))
            raw_h = cascade.detectMultiScale(
                cv2.equalizeHist(gray),
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(min_face, min_face),
            )
            return [(int(x), int(y), int(fw), int(fh), 0.75) for (x, y, fw, fh) in raw_h]
        except Exception:
            pass
    return []


def _find_framed_photo_print(rgb) -> tuple[Any, tuple[int, int, int, int], float] | None:
    """Detect a small physical passport print sitting on a darker cloth/table (photo-of-a-photo).

    Returns ``(cropped_print_rgb, (fx, fy, fw, fh), face_score)`` if a bright/colored
    rectangular portrait print containing a clear face is found; else ``None``.
    """
    cv2, np = _load_cv2()
    if cv2 is None:
        return None
    h, w = rgb.shape[:2]
    img_area = float(h * w)
    if min(h, w) < 120:
        return None

    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    L = lab[:, :, 0].astype(np.float32)
    S = hsv[:, :, 1].astype(np.float32)

    # Candidate masks: bright print on dark surface, OR colorful studio backdrop (blue/cyan/red) on dark/neutral surface
    masks = []
    _, m_bright = cv2.threshold(lab[:, :, 0], 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    masks.append(m_bright)
    # High-saturation studio photo backdrop (e.g. blue passport photo on dark cloth/wall)
    sat_score = np.clip(S * 1.1 + L * 0.45, 0, 255).astype(np.uint8)
    _, m_color = cv2.threshold(sat_score, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    masks.append(m_color)

    k = max(5, (min(h, w) // 45) | 1)
    kernel = np.ones((k, k), np.uint8)
    seen_boxes: list[tuple[int, int, int, int]] = []

    for m in masks:
        clean = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel)
        clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN, np.ones((max(3, k // 2), max(3, k // 2)), np.uint8))
        contours, _ = cv2.findContours(clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
        for c in contours:
            area = float(cv2.contourArea(c))
            ratio = area / img_area
            if not (0.035 <= ratio <= 0.72):
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            if min(bw, bh) < 55:
                continue
            aspect = bw / float(max(1, bh))
            if not (0.45 <= aspect <= 1.35):
                continue
            rect_fill = area / float(max(1, bw * bh))
            if rect_fill < 0.68:
                continue
            box = (x, y, bw, bh)
            if any(abs(x - ox) < 15 and abs(y - oy) < 15 and abs(bw - obw) < 20 for (ox, oy, obw, _) in seen_boxes):
                continue
            seen_boxes.append(box)

            crop = rgb[y:y + bh, x:x + bw]
            # Upscale tiny prints before running face detection so small prints read cleanly
            probe = crop
            up = 1.0
            if min(bw, bh) < 220:
                up = 240.0 / float(min(bw, bh))
                probe = cv2.resize(
                    crop,
                    (max(1, int(round(bw * up))), max(1, int(round(bh * up)))),
                    interpolation=cv2.INTER_CUBIC,
                )
            faces = _detect_faces(probe, min_face_frac=0.18, score_thresh=0.58)
            if not faces:
                continue
            bf = max(faces, key=lambda f: f[2] * f[3])
            fx, fy, fw, fh, fsc = (
                int(round(bf[0] / up)),
                int(round(bf[1] / up)),
                int(round(bf[2] / up)),
                int(round(bf[3] / up)),
                bf[4],
            )
            if (fw / float(bw)) >= 0.22 and (fh / float(bh)) >= 0.18:
                return crop, (fx, fy, fw, fh), fsc
    return None


def crop_passphoto(
    img: Image.Image,
    cfg: dict[str, Any] | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    """Auto-frame a standalone customer passphoto / photo ID.

    1. Trims dark screenshot UI bars and excess pure-white outer scan margins.
    2. Detects the applicant face (YuNet ONNX primary, OpenCV Haar fallback) —
       including small physical passport prints photographed on a dark cloth/table —
       and frames to standard 3.5:4.5 Indian passport proportions when the capture
       has excess background/torso/margins. Leaves already well-framed passport
       photos untouched.
    3. Safely falls back to the trimmed original if no face is detected (never
       destroys aspect ratio on synthetic/abstract inputs).
    """
    cv2, np = _load_cv2()
    orig_w, orig_h = img.size
    target_aspect = float((cfg or {}).get("target_aspect", DEFAULT_PASSPORT_ASPECT))
    diag: dict[str, Any] = {
        "mode": "photo",
        "crop_applied": False,
        "method": "none",
        "reason": "no-change",
        "orig_dims": [orig_w, orig_h],
        "out_dims": [orig_w, orig_h],
    }
    if cv2 is None:
        diag["reason"] = "opencv-unavailable"
        return img, diag
    if min(orig_w, orig_h) < 80:
        diag["reason"] = "too-small"
        return img, diag

    rgb = _ensure_rgb_np(img, np)
    rgb_work, bar_diag = trim_dark_screenshot_bars(rgb)
    bars_trimmed = bool(bar_diag.get("applied"))

    rgb_white, white_diag = _trim_white_margins(rgb_work, pad_frac=0.01, min_comp_area_frac=0.04)
    white_trimmed = bool(white_diag.get("applied"))
    if white_trimmed:
        rgb_work = rgb_white

    h, w = rgb_work.shape[:2]

    # Step 2a: Check if this is a phone photo of a small physical passport print on a table/cloth
    framed_print_used = False
    faces = _detect_faces(rgb_work, min_face_frac=0.10, score_thresh=0.60)
    full_has_prominent_face = any(
        (fw / float(w) >= 0.22 and fh / float(h) >= 0.18) for (_, _, fw, fh, _) in faces
    )
    if not full_has_prominent_face:
        print_hit = _find_framed_photo_print(rgb_work)
        if print_hit is not None:
            print_crop, (pfx, pfy, pfw, pfh), psc = print_hit
            rgb_work = print_crop
            h, w = rgb_work.shape[:2]
            faces = [(pfx, pfy, pfw, pfh, psc)]
            framed_print_used = True

    if len(faces) > 0:
        def _face_score(f):
            fx, fy, fw, fh, sc = f
            area_f = (fw * fh) / float(w * h)
            cx = (fx + fw / 2.0) / w
            cy = (fy + fh / 2.0) / h
            center_pen = abs(cx - 0.5) + abs(cy - 0.45)
            return area_f + 0.15 * sc - 0.25 * center_pen

        fx, fy, fw, fh, _ = max(faces, key=_face_score)
        fx, fy, fw, fh = int(fx), int(fy), int(fw), int(fh)
        cur_aspect = w / float(h)
        face_w_frac = fw / float(w)
        face_h_frac = fh / float(h)
        face_cx_frac = (fx + fw / 2.0) / float(w)

        already_passport = (
            not framed_print_used
            and 0.66 <= cur_aspect <= 0.92
            and 0.30 <= face_w_frac <= 0.72
            and 0.28 <= face_h_frac <= 0.65
            and abs(face_cx_frac - 0.5) <= 0.14
        )
        if framed_print_used and 0.64 <= cur_aspect <= 0.94 and face_w_frac >= 0.26:
            # The isolated physical print itself is already passport-proportioned; trim tiny 2% white rim
            ix, iy = max(1, int(w * 0.02)), max(1, int(h * 0.02))
            trimmed = rgb_work[iy:h - iy, ix:w - ix] if (w - 2 * ix >= 60 and h - 2 * iy >= 75) else rgb_work
            out_im = Image.fromarray(trimmed)
            diag.update(
                crop_applied=True,
                method="framed-print-face-crop",
                reason="physical-print-isolated",
                face_detected=True,
                out_dims=[out_im.width, out_im.height],
            )
            return out_im, diag

        if not already_passport:
            crop_h = min(float(h), fh * 2.55)
            crop_w = crop_h * target_aspect
            if crop_w > w:
                crop_w = float(w)
                crop_h = min(float(h), crop_w / target_aspect)

            fcx = fx + fw / 2.0
            x0 = int(round(fcx - crop_w / 2.0))
            y0 = int(round(fy - fh * 0.58))
            x0 = max(0, min(w - int(round(crop_w)), x0))
            y0 = max(0, min(h - int(round(crop_h)), y0))
            x1 = min(w, x0 + int(round(crop_w)))
            y1 = min(h, y0 + int(round(crop_h)))

            if (x1 - x0) >= 60 and (y1 - y0) >= 75:
                cropped = rgb_work[y0:y1, x0:x1]
                out_im = Image.fromarray(cropped)
                diag.update(
                    crop_applied=True,
                    method="framed-print+face-crop" if framed_print_used else "face-passport-crop",
                    reason="face-framed-3.5x4.5",
                    face_detected=True,
                    out_dims=[out_im.width, out_im.height],
                )
                return out_im, diag
        else:
            diag["face_detected"] = True

    if bars_trimmed or white_trimmed:
        out_im = Image.fromarray(rgb_work)
        parts = []
        if bars_trimmed:
            parts.append("bars")
        if white_trimmed:
            parts.append("white-margins")
        diag.update(
            crop_applied=True,
            method="+".join(parts),
            reason="margins-trimmed",
            out_dims=[out_im.width, out_im.height],
        )
        return out_im, diag

    diag["reason"] = "kept-original"
    return img, diag


def _whiten_signature_background(rgb, norm_gray, np):
    """Lift uneven paper/shadow background to crisp white while preserving ink."""
    # Soft ramp: pixels with normalized brightness >= 218 become pure white (255),
    # pixels <= 170 keep full authentic pen ink color (blue/black), smooth blend in between.
    alpha = np.clip((norm_gray.astype(np.float32) - 170.0) / (218.0 - 170.0), 0.0, 1.0)
    # Darken ink slightly to boost legibility on faded pen scans
    ink_rgb = np.clip(rgb.astype(np.float32) * 0.92, 0.0, 255.0)
    out = ink_rgb * (1.0 - alpha[:, :, None]) + 255.0 * alpha[:, :, None]
    return np.clip(out, 0, 255).astype(np.uint8)


def crop_and_clean_signature(
    img: Image.Image,
    cfg: dict[str, Any] | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    """Auto-crop a standalone customer signature photo and clean paper background.

    1. Strips dark phone screenshot bars.
    2. Isolates white paper if photographed on a darker desk/table.
    3. Normalizes uneven phone lighting/shadows to find pen ink strokes.
    4. Crops around ink strokes with generous safe padding (never tightly clipped)
       and optionally whitens the paper background while preserving anti-aliased ink.
    5. Refuses to crop random noise or blank images.
    """
    cv2, np = _load_cv2()
    orig_w, orig_h = img.size
    clean_bg = bool((cfg or {}).get("clean_background", True))
    diag: dict[str, Any] = {
        "mode": "signature",
        "crop_applied": False,
        "cleaned_bg": False,
        "method": "none",
        "reason": "no-change",
        "orig_dims": [orig_w, orig_h],
        "out_dims": [orig_w, orig_h],
    }
    if cv2 is None:
        diag["reason"] = "opencv-unavailable"
        return img, diag
    if min(orig_w, orig_h) < 40:
        diag["reason"] = "too-small"
        return img, diag

    rgb = _ensure_rgb_np(img, np)
    rgb_work, bar_diag = trim_dark_screenshot_bars(rgb)
    bars_trimmed = bool(bar_diag.get("applied"))
    h, w = rgb_work.shape[:2]

    # Step 2: Check if signature paper sits inside a darker desk/table photo
    paper_cropped = False
    lab = cv2.cvtColor(rgb_work, cv2.COLOR_RGB2LAB)
    hsv = cv2.cvtColor(rgb_work, cv2.COLOR_RGB2HSV)
    L = lab[:, :, 0].astype(np.float32)
    S = hsv[:, :, 1].astype(np.float32)
    border_L = np.concatenate([L[0, :], L[-1, :], L[:, 0], L[:, -1]])
    if float(border_L.mean()) < 135.0 and float(np.percentile(L, 75)) > 165.0:
        whiteness = np.clip(L - 0.55 * S, 0, 255).astype(np.uint8)
        k = max(5, (min(h, w) // 40) | 1)
        smooth = cv2.medianBlur(whiteness, k)
        _, pm = cv2.threshold(smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        mk = max(7, (min(h, w) // 20) | 1)
        pm = cv2.morphologyEx(pm, cv2.MORPH_CLOSE, np.ones((mk, mk), np.uint8))
        pm = cv2.morphologyEx(pm, cv2.MORPH_OPEN, np.ones((max(3, mk // 2), max(3, mk // 2)), np.uint8))
        cand = _evaluate_mask_candidate(pm, L, S, h, w, float(h * w))
        if cand is not None and not cand.get("multi"):
            warped = _warp_quad_rgb(rgb_work, cand["quad"], pad_frac=0.0)
            if min(warped.shape[0], warped.shape[1]) >= 40:
                # Inset slightly (2%) to remove dark paper edge line
                wh, ww = warped.shape[:2]
                ix, iy = max(1, int(ww * 0.02)), max(1, int(wh * 0.02))
                rgb_work = warped[iy:wh - iy, ix:ww - ix]
                paper_cropped = True
                h, w = rgb_work.shape[:2]

    # Step 3: Illumination normalization on grayscale to separate pen ink from paper
    gray = cv2.cvtColor(rgb_work, cv2.COLOR_RGB2GRAY)

    # Guard against high-entropy random noise images (like synthetic noisy_jpeg tests):
    # Real paper has a bright homogeneous dominant mode (median >= 125, IQR < 80)
    # and paper pixels (>= 215 after normalization) dominate (>= 72% of image).
    p25, p50, p75 = (
        float(np.percentile(gray, 25)),
        float(np.percentile(gray, 50)),
        float(np.percentile(gray, 75)),
    )
    iqr = p75 - p25
    bg_sigma = max(15.0, min(h, w) / 14.0)
    bg_est = cv2.GaussianBlur(gray, (0, 0), bg_sigma)
    bg_est = np.maximum(bg_est, 40)
    norm_gray = cv2.divide(gray, bg_est, scale=255)

    paper_frac = float((norm_gray >= 215).mean())
    if p50 < 125.0 or p75 < 150.0 or iqr > 80.0 or paper_frac < 0.72:
        if bars_trimmed or paper_cropped:
            out_im = Image.fromarray(rgb_work)
            diag.update(
                crop_applied=True,
                method="paper-or-bars-only",
                reason="low-paper-dominance",
                out_dims=[out_im.width, out_im.height],
            )
            return out_im, diag
        diag["reason"] = "not-ink-on-paper"
        return img, diag

    # Ink mask: pixels distinctly darker than normalized paper background
    ink = (norm_gray < 195).astype(np.uint8) * 255
    # Zero out outer 2% border strip to ignore paper edge shadows
    bx = max(2, int(w * 0.02))
    by = max(2, int(h * 0.02))
    ink[:by, :] = 0
    ink[h - by:, :] = 0
    ink[:, :bx] = 0
    ink[:, w - bx:] = 0

    # Remove tiny dust specks and ignore dark desk/binding strips touching outer borders
    num, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    min_speck = max(4, int((h * w) * 0.00004))
    hsv_sig = cv2.cvtColor(rgb_work, cv2.COLOR_RGB2HSV)
    interior_boxes = []
    border_boxes = []
    valid_ink_pixels = 0
    edge_band_y = max(by + 4, int(h * 0.11))
    edge_band_x = max(bx + 4, int(w * 0.06))
    for i in range(1, num):
        area = int(stats[i, cv2.CC_STAT_AREA])
        bw_c = int(stats[i, cv2.CC_STAT_WIDTH])
        bh_c = int(stats[i, cv2.CC_STAT_HEIGHT])
        if area >= min_speck or max(bw_c, bh_c) >= 6:
            x = int(stats[i, cv2.CC_STAT_LEFT])
            y = int(stats[i, cv2.CC_STAT_TOP])
            x2, y2 = x + bw_c, y + bh_c
            touches_edge = (
                y <= by + 3 or y2 >= h - by - 3 or x <= bx + 3 or x2 >= w - bx - 3
            )
            in_outer_strip = (
                y2 <= edge_band_y or y >= h - edge_band_y
                or x2 <= edge_band_x or x >= w - edge_band_x
            )
            if touches_edge or (in_outer_strip and float(hsv_sig[y:y2, x:x2, 1].mean()) > 42.0):
                border_boxes.append((x, y, x2, y2, area))
            else:
                interior_boxes.append((x, y, x2, y2, area))
                valid_ink_pixels += area

    valid_boxes = interior_boxes if interior_boxes else border_boxes
    if not interior_boxes:
        valid_ink_pixels = sum(b[4] for b in valid_boxes)

    ink_frac = valid_ink_pixels / float(h * w)
    if not valid_boxes or not (0.0008 <= ink_frac <= 0.42):
        if bars_trimmed or paper_cropped:
            out_im = Image.fromarray(rgb_work)
            diag.update(
                crop_applied=True,
                method="paper-or-bars-only",
                reason="ink-bounds-uncertain",
                out_dims=[out_im.width, out_im.height],
            )
            return out_im, diag
        diag["reason"] = "no-clear-signature-strokes"
        return img, diag

    # Bounding box of ink with generous safe margins (never tightly trimmed!)
    ix0 = min(b[0] for b in valid_boxes)
    iy0 = min(b[1] for b in valid_boxes)
    ix1 = max(b[2] for b in valid_boxes)
    iy1 = max(b[3] for b in valid_boxes)
    ink_w = max(1, ix1 - ix0)
    ink_h = max(1, iy1 - iy0)

    pad_x = max(16, int(round(ink_w * 0.20)))
    pad_y = max(16, int(round(ink_h * 0.28)))
    x0 = max(0, ix0 - pad_x)
    y0 = max(0, iy0 - pad_y)
    x1 = min(w, ix1 + pad_x)
    y1 = min(h, iy1 + pad_y)

    # Ensure sensible minimum output dimensions
    if (x1 - x0) < 60 or (y1 - y0) < 25:
        diag["reason"] = "ink-region-too-tiny"
        return img, diag

    cropped_rgb = rgb_work[y0:y1, x0:x1]
    cropped_norm = norm_gray[y0:y1, x0:x1]

    if clean_bg:
        cropped_rgb = _whiten_signature_background(cropped_rgb, cropped_norm, np)
        diag["cleaned_bg"] = True

    out_im = Image.fromarray(cropped_rgb)
    # Mark crop_applied=True if dimensions changed OR background was cleaned
    changed = (
        bars_trimmed
        or paper_cropped
        or clean_bg
        or (out_im.width < orig_w or out_im.height < orig_h)
    )
    diag.update(
        crop_applied=changed,
        method="signature-crop" + ("+whiten" if clean_bg else ""),
        reason="signature-extracted",
        ink_frac=round(ink_frac, 4),
        out_dims=[out_im.width, out_im.height],
    )
    return out_im, diag


def apply_manual_crop(
    img: Image.Image,
    crop_box: tuple[float, float, float, float] | None = None,
    rotate_deg: int = 0,
    clean_signature_bg: bool = False,
) -> tuple[Image.Image, dict[str, Any]]:
    """Apply explicit operator crop/rotation from the dashboard crop tool.

    ``crop_box`` is normalized ``(x, y, w, h)`` in ``[0.0, 1.0]`` relative to
    the EXIF-oriented, rotated image.
    """
    work = ImageOps.exif_transpose(img).convert("RGB")
    orig_dims = [work.width, work.height]

    rot = int(rotate_deg) % 360
    if rot in (90, 180, 270):
        # PIL rotate is counter-clockwise; negate for standard clockwise degrees
        work = work.rotate(-rot, expand=True)

    applied = rot in (90, 180, 270)
    if crop_box is not None:
        nx, ny, nw, nh = crop_box
        nx = max(0.0, min(1.0, float(nx)))
        ny = max(0.0, min(1.0, float(ny)))
        nw = max(0.01, min(1.0 - nx, float(nw)))
        nh = max(0.01, min(1.0 - ny, float(nh)))
        x0 = int(round(nx * work.width))
        y0 = int(round(ny * work.height))
        x1 = max(x0 + 10, min(work.width, int(round((nx + nw) * work.width))))
        y1 = max(y0 + 10, min(work.height, int(round((ny + nh) * work.height))))
        work = work.crop((x0, y0, x1, y1))
        applied = True

    cleaned_bg = False
    if clean_signature_bg:
        cv2, np = _load_cv2()
        if cv2 is not None:
            rgb = np.array(work)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            bg_sigma = max(15.0, min(rgb.shape[0], rgb.shape[1]) / 14.0)
            bg_est = np.maximum(cv2.GaussianBlur(gray, (0, 0), bg_sigma), 40)
            norm_gray = cv2.divide(gray, bg_est, scale=255)
            work = Image.fromarray(_whiten_signature_background(rgb, norm_gray, np))
            cleaned_bg = True
            applied = True

    return work, {
        "mode": "manual",
        "crop_applied": applied,
        "cleaned_bg": cleaned_bg,
        "rotate_deg": rot,
        "method": "operator-manual-crop",
        "reason": "manual-crop-applied" if applied else "no-change",
        "orig_dims": orig_dims,
        "out_dims": [work.width, work.height],
    }


def _count_horizontal_ink_bands(ink_mask, np) -> tuple[int, float]:
    """Count distinct horizontal ink bands and vertical span fraction (rejects multi-line printed docs)."""
    h, w = ink_mask.shape[:2]
    if h < 20 or w < 20:
        return 0, 0.0
    row_density = (ink_mask > 0).mean(axis=1)
    active = row_density >= 0.008
    bands = 0
    in_band = False
    gap = 0
    min_gap = max(2, int(h * 0.012))
    active_rows = []
    for r, on in enumerate(active):
        if on:
            active_rows.append(r)
            if not in_band:
                bands += 1
                in_band = True
            gap = 0
        elif in_band:
            gap += 1
            if gap >= min_gap:
                in_band = False
    span_frac = ((active_rows[-1] - active_rows[0]) / float(h)) if len(active_rows) >= 2 else 0.0
    return bands, span_frac


def classify_visual_image(
    img: Image.Image,
    *,
    allow_signature: bool = True,
    low_text_verified: bool = False,
) -> tuple[str, float, str]:
    """Offline visual classifier for unlabeled phone uploads (e.g. WhatsApp Image).

    Intended to run ONLY when filename clues and OCR text did not already
    identify an ID card/document. Distinguishes:
    - ``photo``: prominent portrait face (YuNet ONNX primary, Haar fallback),
      including a small physical passport print photographed on a dark surface
      when ``low_text_verified=True``.
    - ``signature``: clean dark pen ink strokes on homogeneous light low-saturation
      paper, strictly guarded against multi-line printed sheets/cards.
    - ``supporting_ll``: high-confidence rectangular card on a contrasting background
      (even when tilt/blur prevented initial OCR).
    - ``unknown``: genuinely ambiguous image requiring operator choice.
    """
    cv2, np = _load_cv2()
    if cv2 is None:
        return "unknown", 0.0, "visual:opencv-unavailable"

    work = ImageOps.exif_transpose(img).convert("RGB")
    if min(work.width, work.height) < 60:
        return "unknown", 0.0, "visual:too-small"

    rgb = np.array(work)
    rgb_work, _ = trim_dark_screenshot_bars(rgb)
    h, w = rgb_work.shape[:2]

    # 1. Standalone passphoto check (YuNet ONNX primary + Haar fallback)
    faces = _detect_faces(rgb_work, min_face_frac=0.14, score_thresh=0.62)
    if faces:
        bf = max(faces, key=lambda f: f[2] * f[3])
        fw, fh = int(bf[2]), int(bf[3])
        w_frac = fw / float(w)
        h_frac = fh / float(h)
        area_frac = (fw * fh) / float(w * h)
        if (w_frac >= 0.16 and h_frac >= 0.13) or area_frac >= 0.038:
            return "photo", 0.88, f"visual:face-detected({w_frac:.2f}w)"

    # 1b. Physical passport print resting on a dark cloth/table (only when OCR confirmed low/no text!)
    if low_text_verified:
        print_hit = _find_framed_photo_print(rgb_work)
        if print_hit is not None:
            return "photo", 0.86, "visual:framed-print-face"

    # 2. Standalone signature check (pen ink strokes on light low-saturation paper)
    if allow_signature:
        sig_work = rgb_work
        sh, sw = sig_work.shape[:2]
        lab = cv2.cvtColor(sig_work, cv2.COLOR_RGB2LAB)
        hsv = cv2.cvtColor(sig_work, cv2.COLOR_RGB2HSV)
        L = lab[:, :, 0].astype(np.float32)
        S = hsv[:, :, 1].astype(np.float32)
        border_L = np.concatenate([L[0, :], L[-1, :], L[:, 0], L[:, -1]])
        if float(border_L.mean()) < 135.0 and float(np.percentile(L, 75)) > 165.0:
            whiteness = np.clip(L - 0.55 * S, 0, 255).astype(np.uint8)
            k = max(5, (min(sh, sw) // 40) | 1)
            smooth = cv2.medianBlur(whiteness, k)
            _, pm = cv2.threshold(smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            mk = max(7, (min(sh, sw) // 20) | 1)
            pm = cv2.morphologyEx(pm, cv2.MORPH_CLOSE, np.ones((mk, mk), np.uint8))
            pm = cv2.morphologyEx(pm, cv2.MORPH_OPEN, np.ones((max(3, mk // 2), max(3, mk // 2)), np.uint8))
            cand = _evaluate_mask_candidate(pm, L, S, sh, sw, float(sh * sw))
            if cand is not None and not cand.get("multi"):
                warped = _warp_quad_rgb(sig_work, cand["quad"], pad_frac=0.0)
                if min(warped.shape[0], warped.shape[1]) >= 40:
                    wh, ww = warped.shape[:2]
                    ix, iy = max(1, int(ww * 0.02)), max(1, int(wh * 0.02))
                    sig_work = warped[iy:wh - iy, ix:ww - ix]

        sg = cv2.cvtColor(sig_work, cv2.COLOR_RGB2GRAY)
        s_hsv = cv2.cvtColor(sig_work, cv2.COLOR_RGB2HSV)
        p25, p50, p75 = (
            float(np.percentile(sg, 25)),
            float(np.percentile(sg, 50)),
            float(np.percentile(sg, 75)),
        )
        med_sat = float(np.median(s_hsv[:, :, 1]))
        bg_sigma = max(15.0, min(sg.shape[0], sg.shape[1]) / 14.0)
        bg_est = np.maximum(cv2.GaussianBlur(sg, (0, 0), bg_sigma), 40)
        norm_g = cv2.divide(sg, bg_est, scale=255)
        paper_frac = float((norm_g >= 215).mean())

        if p50 >= 125.0 and p75 >= 150.0 and (p75 - p25) <= 80.0 and paper_frac >= 0.72 and med_sat < 45.0:
            ink = (norm_g < 195).astype(np.uint8) * 255
            bx = max(2, int(sg.shape[1] * 0.03))
            by = max(2, int(sg.shape[0] * 0.03))
            ink[:by, :] = 0
            ink[sg.shape[0] - by:, :] = 0
            ink[:, :bx] = 0
            ink[:, sg.shape[1] - bx:] = 0
            # Reject dense multi-line printed documents (Aadhaar/10th Memo/Forms)
            n_bands, span_frac = _count_horizontal_ink_bands(ink, np)
            if not (n_bands >= 5 and span_frac >= 0.45):
                num, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
                min_speck = max(4, int(sg.size * 0.00004))
                valid_px = 0
                xs, ys = [], []
                for i in range(1, num):
                    area = int(stats[i, cv2.CC_STAT_AREA])
                    bw_c = int(stats[i, cv2.CC_STAT_WIDTH])
                    bh_c = int(stats[i, cv2.CC_STAT_HEIGHT])
                    if area >= min_speck or max(bw_c, bh_c) >= 6:
                        valid_px += area
                        x0 = int(stats[i, cv2.CC_STAT_LEFT])
                        y0 = int(stats[i, cv2.CC_STAT_TOP])
                        xs.extend((x0, x0 + bw_c))
                        ys.extend((y0, y0 + bh_c))
                ink_frac = valid_px / float(max(1, sg.size))
                if xs and (0.0008 <= ink_frac <= 0.18):
                    span_w = max(xs) - min(xs)
                    span_h = max(ys) - min(ys)
                    if span_w >= 50 and span_h >= 18:
                        return "signature", 0.82, f"visual:ink-on-paper(ink={ink_frac:.4f})"

    # 3. Rectangular physical card on textured/dark background fallback
    _, card_diag = crop_document_card(work)
    if card_diag.get("crop_applied") and str(card_diag.get("method", "")).startswith(
        ("perspective:", "multi-card-union:")
    ):
        return "supporting_ll", 0.75, f"visual:card-detected({card_diag.get('method')})"

    return "unknown", 0.0, "visual:ambiguous"


def process_for_profile(
    img: Image.Image,
    profile_name: str,
    profile: dict[str, Any],
) -> tuple[Image.Image, dict[str, Any]]:
    """Dispatch automatic cropping/cleaning based on profile configuration."""
    if not profile.get("auto_crop", False):
        return img, {
            "mode": profile_name,
            "crop_applied": False,
            "method": "disabled",
            "reason": "auto-crop-disabled",
            "orig_dims": [img.width, img.height],
            "out_dims": [img.width, img.height],
        }

    crop_mode = str(profile.get("crop_mode") or profile_name).lower()
    if crop_mode == "photo":
        return crop_passphoto(img, profile)
    if crop_mode == "signature":
        return crop_and_clean_signature(img, profile)
    if crop_mode in ("document", "card", "aadhaar", "supporting_ll"):
        return crop_document_card(img, profile)

    return img, {
        "mode": crop_mode,
        "crop_applied": False,
        "method": "none",
        "reason": "no-handler",
        "orig_dims": [img.width, img.height],
        "out_dims": [img.width, img.height],
    }
