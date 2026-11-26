"""Document preparation: classify + auto-crop/clean + per-type compress to prepared copies.

- Original (staging) bytes are never modified; every output is a new file.
- Images: EXIF transpose, optional safe auto-crop/rectification/cleaning per
  profile (or explicit operator manual crop), aspect preserved on fallback,
  never upscaled, JPEG-safe modes.
- PDFs: all pages kept, text preserved, optimize-only (no rasterize/flatten).
- Outputs must be strictly below the profile max_bytes or the document is
  marked needs_attention with a reason (never silently over-compressed).
- No invented application numbers.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from ..config import FormFlowConfig
from ..db import connect, create_document, get_document, init_db, update_document, utcnow_iso
from .classify import classify_document, suggest_doctype
from .document_cv import apply_manual_crop, process_for_profile
from .filters import PROCESSED_PREFIX, is_processed_output, resolve_within, sanitize_filename
from .profiles import custom_classifier_rules, get_profile, load_profiles

_STAGED_HASH_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{12}_(?:\d+_)?")

_EXPORT_TAGS = {
    "photo": "photo",
    "signature": "signature",
    "aadhaar": "aadhaar",
    "pan": "pan",
    "driving_licence": "dl",
    "learner_licence": "ll",
    "tr_document": "tr",
    "vehicle_rc": "tr",
    "ssc": "10th_memo",
    "supporting_ll": "doc",
}

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
SUPPORTED_PDF_EXTS = {".pdf"}
SUPPORTED_PREPARE_EXTS = SUPPORTED_IMAGE_EXTS | SUPPORTED_PDF_EXTS


class SizeTargetError(Exception):
    """Raised when the byte target cannot be met within safe bounds."""


def mime_for(ext: str, prepared: bool = False) -> str:
    e = ext.lower()
    if e == ".pdf":
        return "application/pdf"
    if e in (".jpg", ".jpeg"):
        return "image/jpeg"
    if e == ".png":
        return "image/jpeg" if prepared else "image/png"
    return "application/octet-stream"


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_prepared_filename(
    applicant_name: str | None,
    case_id: str | None,
    appl_number: str | None,
    doctype: str,
    ext: str,
    job_id: int | None = None,
) -> str:
    """Safe prepared name. Official number used only when explicitly supplied."""
    clean_name = sanitize_filename((applicant_name or "applicant").strip().replace(" ", "_"))
    clean_name = clean_name.rsplit(".", 1)[0][:60] or "applicant"
    appl = appl_number.strip() if appl_number and appl_number.strip() else ""
    case = case_id.strip() if case_id and case_id.strip() else ""
    anchor = appl or case or (f"job{job_id}" if job_id else "nocase")
    anchor = sanitize_filename(anchor).rsplit(".", 1)[0][:40] or "nocase"
    safe_type = sanitize_filename(doctype or "unknown").rsplit(".", 1)[0][:30] or "unknown"
    e = ext.lower() if ext.lower() in (".jpg", ".jpeg", ".pdf") else ".jpg"
    if e == ".jpeg":
        e = ".jpg"
    return sanitize_filename(f"{clean_name}_{anchor}_{safe_type}{e}")


def _resolve_case_sno(conn, doc, cfg: FormFlowConfig | None = None) -> int:
    """Resolve the customer/case serial number for ``Ready_<type>_<Sno>.jpg``.

    When a document was dropped into Inbox and prepared before OCR assigns its
    final ``case_id``, peek at the active intake burst case (or next sequential
    case serial) so the file immediately exports under the right ``<Sno>``.
    """
    case_pk = doc["case_id"] if doc is not None else None
    if case_pk:
        crow = conn.execute("SELECT case_id FROM cases WHERE id = ?", (case_pk,)).fetchone()
        if crow and crow["case_id"]:
            parts = str(crow["case_id"]).rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return int(parts[1])
        return int(case_pk)

    if doc is not None and doc["intake_job_id"] is not None and cfg is not None:
        try:
            from .match import _recent_burst_case

            arrival_ts = doc["created_at"] if "created_at" in set(doc.keys()) else None
            jrow = conn.execute(
                "SELECT created_at FROM intake_jobs WHERE id = ?",
                (doc["intake_job_id"],),
            ).fetchone()
            if jrow and jrow["created_at"]:
                arrival_ts = jrow["created_at"]
            window = float(getattr(cfg, "inbox_batch_window_sec", 180.0) or 0.0)
            burst = _recent_burst_case(conn, window, reference_ts=arrival_ts)
            if burst is not None and burst["case_id"]:
                parts = str(burst["case_id"]).rsplit("-", 1)
                if len(parts) == 2 and parts[1].isdigit():
                    return int(parts[1])
                return int(burst["id"])
            # No active burst case yet -> predict the next case serial number for today.
            from ..db import generate_case_id

            nxt = generate_case_id(conn)
            parts = nxt.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return int(parts[1])
        except Exception:
            pass
        return 1

    return int(doc["id"]) if doc is not None and doc["id"] else 1


def _fast_classify_image(
    src: Path,
    cfg: FormFlowConfig,
    orig_filename: str,
    ext: str,
    width: int | None,
    height: int | None,
    custom_rules: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str, float, str] | None:
    """Fast (~0.3-1.2s) pre-OCR classification for unlabeled WhatsApp Inbox drops.

    Uses bundled RapidOCR first (Tesseract fallback) + offline QR detection +
    YuNet/Haar face detection so printed documents (Aadhaar, 10th Memo, PAN, LL,
    DL, TR) are NEVER mistaken for signatures when Tesseract is absent, while
    true photos (including photos of physical passport prints) and signatures
    crop and export cleanly into ``Inbox\\Ready_*``.

    Returns ``(pipeline_doctype, profile_category, confidence, reason)`` or None.
    """
    try:
        from .document_cv import classify_visual_image

        with Image.open(src) as im:
            im.load()
            work = ImageOps.exif_transpose(im).convert("RGB")
    except Exception:
        return None

    # 1. Offline QR check first (instant on Aadhaar & PAN cards — never a photo/signature)
    has_qr = False
    try:
        from .qr_decode import detect_has_qr, extract_qr_candidates

        qr_cands, qr_cat = extract_qr_candidates(work)
        if qr_cat:
            return "supporting_ll", qr_cat, 0.95, f"fast-classify:qr:{qr_cat}"
        has_qr = detect_has_qr(work)
    except Exception:
        has_qr = False

    # 2. Fast offline OCR sniff BEFORE visual signature detection.
    #    Prefers bundled RapidOCR (works on machines without Tesseract!) with Tesseract fallback.
    sniff_text = ""
    region_count = 0
    ocr_probe_ran = False
    probe = work
    if max(probe.size) > 1100:
        scale = 1100.0 / max(probe.size)
        probe = probe.resize(
            (max(1, int(probe.width * scale)), max(1, int(probe.height * scale))),
            Image.LANCZOS,
        )

    selected_backend = (getattr(cfg, "ocr_backend", "auto") or "auto").strip().lower()
    if selected_backend in ("auto", "paddleocr") and not getattr(cfg, "force_tesseract", False):
        try:
            from . import ocr_paddle

            if ocr_paddle.available():
                sniff_text, _, regions, _ = ocr_paddle.run_paddle_ocr_full(probe, lang="en")
                region_count = len(regions or [])
                ocr_probe_ran = True
        except Exception:
            pass

    if not ocr_probe_ran:
        try:
            from .ocr import tesseract_ocr_full

            probe_gray = ImageOps.autocontrast(probe.convert("L"), cutoff=1)
            sniff_text, _, regions, _ = tesseract_ocr_full(probe_gray, cfg, lang="eng", psm=6)
            region_count = len(regions or [])
            ocr_probe_ran = True
        except Exception:
            sniff_text = ""

    alnum_chars = sum(1 for ch in sniff_text if ch.isalnum())
    if sniff_text:
        cls2 = classify_document(
            orig_filename, ext, width, height, ocr_hints={"text": sniff_text},
            custom_rules=custom_rules,
        )
        if cls2.is_identity_doc and cls2.category not in ("unknown", "supporting_ll"):
            return cls2.doctype, cls2.category, cls2.confidence, cls2.reason
        if alnum_chars >= 25 or region_count >= 4:
            return "supporting_ll", "supporting_ll", 0.72, "fast-classify:document-text"

    if has_qr:
        return "supporting_ll", "supporting_ll", 0.78, "fast-classify:qr-detected"

    # 3. Low/no printed text -> visual classification (photo / framed-print photo / strict signature / card).
    #    CRITICAL: Never allow visual 'signature' unless OCR probe actually ran and confirmed sparse text!
    low_text_verified = ocr_probe_ran and alnum_chars < 15 and region_count <= 2
    try:
        vis_type, vis_conf, vis_reason = classify_visual_image(
            work.copy(),
            allow_signature=low_text_verified,
            low_text_verified=low_text_verified,
        )
        if vis_type in ("photo", "signature"):
            return vis_type, vis_type, vis_conf, vis_reason
        if vis_type == "supporting_ll":
            return "supporting_ll", "supporting_ll", vis_conf, vis_reason
    except Exception:
        pass
    return None


def _fast_classify_pdf(
    src: Path,
    cfg: FormFlowConfig,
    orig_filename: str,
    custom_rules: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str, float, str] | None:
    """Fast pre-OCR classification for PDFs using embedded text (<10ms) or a quick page-1 probe."""
    pdf_text = ""
    try:
        import pymupdf

        with pymupdf.open(src) as doc:
            parts = [doc[i].get_text() for i in range(min(2, doc.page_count))]
            pdf_text = "\n".join(parts).strip()
            if sum(1 for ch in pdf_text if ch.isalnum()) < 25 and doc.page_count >= 1:
                pix = doc[0].get_pixmap(dpi=130, alpha=False)
                probe = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                if max(probe.size) > 900:
                    sc = 900.0 / max(probe.size)
                    probe = probe.resize(
                        (max(1, int(probe.width * sc)), max(1, int(probe.height * sc))),
                        Image.LANCZOS,
                    )
                try:
                    from . import ocr_paddle

                    if ocr_paddle.available():
                        pdf_text, _, _, _ = ocr_paddle.run_paddle_ocr_full(probe, lang="en")
                except Exception:
                    pass
                if not pdf_text.strip():
                    try:
                        from .ocr import tesseract_ocr_full

                        probe_gray = ImageOps.autocontrast(probe.convert("L"), cutoff=1)
                        pdf_text, _, _, _ = tesseract_ocr_full(probe_gray, cfg, lang="eng", psm=6)
                    except Exception:
                        pass
    except Exception:
        return None

    if not pdf_text.strip():
        return None
    cls2 = classify_document(
        orig_filename, ".pdf", ocr_hints={"text": pdf_text}, custom_rules=custom_rules
    )
    if cls2.is_identity_doc and cls2.category not in ("unknown", "supporting_ll"):
        return cls2.doctype, cls2.category, cls2.confidence, cls2.reason
    return None


def build_inbox_export_filename(
    orig_filename: str,
    doctype: str,
    ext: str,
    label_hint: str | None = None,
    sno: int | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Build a concise ``Ready_<type>_<Sno>.<ext>`` filename for Inbox export."""
    tag_key = (label_hint or doctype or "doc").strip().lower()
    custom_tag = None
    if isinstance(profiles, dict) and tag_key in profiles and isinstance(profiles[tag_key], dict):
        raw_ct = profiles[tag_key].get("export_tag")
        if raw_ct and str(raw_ct).strip():
            custom_tag = sanitize_filename(str(raw_ct).strip().lower())[:24]
    tag = custom_tag or _EXPORT_TAGS.get(
        tag_key,
        sanitize_filename(tag_key)[:24] if tag_key and tag_key != "unknown" else "doc",
    )
    e = ext.lower() if ext.lower() in (".jpg", ".jpeg", ".pdf") else ".jpg"
    if e == ".jpeg":
        e = ".jpg"
    num = max(1, int(sno)) if sno is not None else 1
    return sanitize_filename(f"Ready_{tag}_{num}{e}")


def export_to_inbox(
    conn,
    doc_id: int,
    cfg: FormFlowConfig,
    prepared_path: Path,
    orig_filename: str,
    doctype: str,
    label_hint: str | None = None,
) -> Path | None:
    """Atomically copy a prepared file back into Inbox as ``Ready_<type>_<Sno>.jpg``.

    - Only exports when ``cfg.inbox_dir`` exists and ``prepared_path`` is a file.
    - Writes to a hidden ``.tmp`` file first and atomically swaps via ``os.replace``
      so neither the watcher nor browser upload pickers ever see a partial file.
    - If this document previously exported a ``Ready_*`` file in Inbox, overwrites
      it in place (or removes the old one cleanly if the type or case serial changed).
    - Never raises on filesystem quirks; returns exported Path or None.
    """
    try:
        inbox_dir = cfg.inbox_dir
        if not inbox_dir.exists() or not inbox_dir.is_dir():
            return None
        if not prepared_path.is_file():
            return None

        doc = get_document(conn, doc_id)
        if doc is None:
            return None
        keys = set(doc.keys())
        prev_raw = doc["inbox_export_path"] if "inbox_export_path" in keys else None
        prev_path: Path | None = None
        if prev_raw:
            try:
                cand = resolve_within(Path(prev_raw), inbox_dir)
                if is_processed_output(cand.name):
                    prev_path = cand
            except ValueError:
                prev_path = None

        sno = _resolve_case_sno(conn, doc, cfg=cfg)
        active_profiles, _ = load_profiles(cfg)
        desired_name = build_inbox_export_filename(
            orig_filename, doctype, prepared_path.suffix, label_hint=label_hint, sno=sno,
            profiles=active_profiles,
        )
        dest = resolve_within(inbox_dir / desired_name, inbox_dir)

        # Avoid clobbering a DIFFERENT document's Ready_ file (e.g. front+back Aadhaar for same case)
        if dest.exists() and (prev_path is None or dest.resolve() != prev_path.resolve()):
            stem, dot, ext = desired_name.rpartition(".")
            n = 2
            while dest.exists() and (prev_path is None or dest.resolve() != prev_path.resolve()):
                cand_name = f"{stem}_{n}.{ext}" if dot else f"{stem}_{n}"
                dest = resolve_within(inbox_dir / cand_name, inbox_dir)
                n += 1
                if n > 1000:
                    return None

        tmp_path = resolve_within(inbox_dir / f".ff_export_{doc_id}_{os.getpid()}.tmp", inbox_dir)
        try:
            shutil.copyfile(prepared_path, tmp_path)
            os.replace(tmp_path, dest)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        # Clean up stale previous Ready_ file if filename changed (e.g. case assigned or re-classified)
        if prev_path is not None and prev_path.exists() and prev_path.resolve() != dest.resolve():
            try:
                prev_path.unlink()
            except OSError:
                pass

        update_document(conn, doc_id, inbox_export_path=str(dest))
        return dest
    except Exception:
        return None


def sync_inbox_export(
    conn,
    doc_id: int,
    cfg: FormFlowConfig,
    label_hint: str | None = None,
) -> Path | None:
    """Refresh a ready document's ``Inbox/Ready_<type>_<Sno>.jpg`` after case/type updates."""
    doc = get_document(conn, doc_id)
    if doc is None or doc["status"] != "ready" or not doc["prepared_path"]:
        return None
    prep = Path(doc["prepared_path"])
    if not prep.is_file():
        return None
    effective_hint = label_hint or doc["profile_name"] or doc["doctype"]
    return export_to_inbox(
        conn, doc_id, cfg, prep, doc["orig_filename"], doc["doctype"],
        label_hint=effective_hint,
    )


def prepared_dest(cfg: FormFlowConfig, filename: str) -> Path:
    out_dir = cfg.data_dir / "prepared"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / sanitize_filename(filename)
    n = 1
    while dest.exists():
        stem, dot, ext = sanitize_filename(filename).rpartition(".")
        dest = out_dir / f"{stem}_{n}.{ext}" if dot else out_dir / f"{stem}_{n}"
        n += 1
        if n > 1000:
            raise OSError("prepared namespace exhausted")
    return resolve_within(dest, cfg.data_dir)


def to_jpeg_safe_mode(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "PA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        alpha = img.getchannel("A")
        bg.paste(img.convert("RGB"), mask=alpha)
        return bg
    if img.mode == "P":
        if "transparency" in img.info:
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img.convert("RGB"), mask=img.convert("RGBA").getchannel("A"))
            return bg
        return img.convert("RGB")
    if img.mode in ("CMYK", "YCbCr", "LAB", "HSV", "I", "F"):
        return img.convert("RGB")
    if img.mode in ("RGB", "L"):
        return img
    return img.convert("RGB")


def _encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    save_kwargs: dict[str, Any] = {"format": "JPEG", "quality": quality,
                                   "optimize": True, "progressive": False}
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def _resolve_output_ext(src_ext: str, profile: dict[str, Any]) -> str:
    """Determine prepared output extension ('.jpg' or '.pdf') for a source file & profile."""
    e = src_ext.lower()
    if profile.get("passthrough"):
        return ".pdf" if e == ".pdf" else ".jpg"
    mode = str(profile.get("output_mode") or "jpg").strip().lower()
    if mode == "pdf":
        return ".pdf"
    if mode == "jpg":
        return ".jpg"
    return ".pdf" if e == ".pdf" else ".jpg"


def _apply_cv_to_pil(
    work: Image.Image,
    profile: dict[str, Any],
    profile_name: str,
    *,
    manual_crop: tuple[float, float, float, float] | None = None,
    rotate_deg: int = 0,
    clean_signature_bg: bool | None = None,
    auto_crop: bool | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    orig_w, orig_h = work.size
    cv_diag: dict[str, Any] = {
        "mode": profile_name,
        "crop_applied": False,
        "method": "none",
        "reason": "skipped",
        "orig_dims": [orig_w, orig_h],
        "out_dims": [orig_w, orig_h],
    }
    if manual_crop is not None or (int(rotate_deg) % 360) != 0 or clean_signature_bg is not None:
        work, cv_diag = apply_manual_crop(
            work,
            crop_box=manual_crop,
            rotate_deg=rotate_deg,
            clean_signature_bg=bool(clean_signature_bg),
        )
    else:
        effective_profile = dict(profile)
        if auto_crop is not None:
            effective_profile["auto_crop"] = bool(auto_crop)
        if effective_profile.get("auto_crop", False):
            work, cv_diag = process_for_profile(work, profile_name, effective_profile)
            work = to_jpeg_safe_mode(work)
    return work, cv_diag


def _compress_pil_to_jpeg(
    work: Image.Image,
    dest: Path,
    profile: dict[str, Any],
    *,
    before: int,
    orig_dims: tuple[int, int],
    cv_diag: dict[str, Any],
) -> dict[str, Any]:
    work_w, work_h = work.size
    aspect_before = work_w / max(1, work_h)
    max_bytes = int(profile["max_bytes"])
    qualities = list(profile.get("qualities", [90, 80, 70, 60, 55]))
    scales = list(profile.get("scales", [1.0, 0.85, 0.7, 0.55, 0.4]))
    max_dim = int(profile.get("max_dim", 1600))
    min_dim = int(profile.get("min_dim", 200))
    min_quality = int(profile.get("min_quality", 55))

    base_scale = min(1.0, max_dim / max(work_w, work_h))
    tried = []
    for s in scales:
        scale = min(s, base_scale)
        if scale <= 0:
            continue
        w, h = max(1, round(work_w * scale)), max(1, round(work_h * scale))
        if min(w, h) < min_dim and scale < base_scale:
            break
        frame = work if scale == 1.0 else work.resize((w, h), Image.LANCZOS)
        for q in qualities:
            if q < min_quality:
                continue
            data = _encode_jpeg(frame, q)
            tried.append((w, h, q, len(data)))
            if len(data) < max_bytes:
                dest.write_bytes(data)
                return {
                    "width": w, "height": h, "quality": q,
                    "bytes": len(data), "bytes_before": before,
                    "aspect_before": aspect_before, "aspect_after": w / max(1, h),
                    "orig_dims": orig_dims,
                    "cv": cv_diag,
                }
    raise SizeTargetError(
        f"could not reach {max_bytes} bytes within quality>={min_quality} "
        f"and min_dim>={min_dim} (tried {len(tried)} combos, smallest {min(t[3] for t in tried) if tried else 'n/a'} bytes)"
    )


def prepare_image(
    src: Path,
    dest: Path,
    profile: dict[str, Any],
    profile_name: str = "unknown",
    manual_crop: tuple[float, float, float, float] | None = None,
    rotate_deg: int = 0,
    clean_signature_bg: bool | None = None,
    auto_crop: bool | None = None,
) -> dict[str, Any]:
    before = src.stat().st_size
    with Image.open(src) as im:
        im.load()
        needs_transpose = (im.getexif().get(274, 1) not in (1, None))
        im = ImageOps.exif_transpose(im)
        orig_w, orig_h = im.size
        work = to_jpeg_safe_mode(im).copy()

    work, cv_diag = _apply_cv_to_pil(
        work, profile, profile_name,
        manual_crop=manual_crop,
        rotate_deg=rotate_deg,
        clean_signature_bg=clean_signature_bg,
        auto_crop=auto_crop,
    )
    crop_applied = bool(cv_diag.get("crop_applied", False))
    max_bytes = int(profile["max_bytes"])
    src_is_jpeg = src.suffix.lower() in (".jpg", ".jpeg")
    if src_is_jpeg and not needs_transpose and not crop_applied and before < max_bytes:
        data = src.read_bytes()
        dest.write_bytes(data)
        return {
            "width": orig_w, "height": orig_h, "quality": "copy",
            "bytes": len(data), "bytes_before": before,
            "aspect_before": orig_w / orig_h, "aspect_after": orig_w / orig_h,
            "orig_dims": (orig_w, orig_h),
            "cv": cv_diag,
        }
    return _compress_pil_to_jpeg(
        work, dest, profile,
        before=before, orig_dims=(orig_w, orig_h), cv_diag=cv_diag,
    )


def prepare_pdf_as_jpeg(
    src: Path,
    dest: Path,
    profile: dict[str, Any],
    *,
    profile_name: str = "supporting_ll",
    auto_crop: bool | None = None,
) -> dict[str, Any]:
    """Render a PDF into a clean JPEG for JPG-only profiles (e.g. Aadhaar PDF -> Ready_aadhaar.jpg)."""
    import pymupdf

    before = src.stat().st_size
    with pymupdf.open(src) as doc:
        n_pages = doc.page_count
        if n_pages < 1:
            raise SizeTargetError("PDF has no pages")
        crop_mode = str(profile.get("crop_mode") or "document").lower()
        pages_to_render = 2 if (n_pages == 2 and crop_mode == "document") else 1
        rendered: list[Image.Image] = []
        for idx in range(pages_to_render):
            pix = doc[idx].get_pixmap(dpi=220, alpha=False)
            rendered.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))

    if len(rendered) == 2:
        gap = 16
        out_w = max(rendered[0].width, rendered[1].width)
        out_h = rendered[0].height + gap + rendered[1].height
        work = Image.new("RGB", (out_w, out_h), (255, 255, 255))
        work.paste(rendered[0], ((out_w - rendered[0].width) // 2, 0))
        work.paste(rendered[1], ((out_w - rendered[1].width) // 2, rendered[0].height + gap))
    else:
        work = rendered[0]

    orig_w, orig_h = work.size
    work, cv_diag = _apply_cv_to_pil(work, profile, profile_name, auto_crop=auto_crop)
    if cv_diag.get("method") == "none":
        cv_diag["method"] = "pdf_to_jpg"
        cv_diag["reason"] = "converted_from_pdf"
    else:
        cv_diag["method"] = f"pdf_to_jpg+{cv_diag['method']}"
    return _compress_pil_to_jpeg(
        work, dest, profile,
        before=before, orig_dims=(orig_w, orig_h), cv_diag=cv_diag,
    )


def prepare_image_as_pdf(
    src: Path,
    dest: Path,
    profile: dict[str, Any],
    *,
    profile_name: str = "supporting_ll",
    manual_crop: tuple[float, float, float, float] | None = None,
    rotate_deg: int = 0,
    clean_signature_bg: bool | None = None,
    auto_crop: bool | None = None,
) -> dict[str, Any]:
    """Crop/clean an image and pack it into a compressed single-page PDF under ``max_bytes``."""
    import pymupdf

    before = src.stat().st_size
    with Image.open(src) as im:
        im.load()
        im = ImageOps.exif_transpose(im)
        orig_w, orig_h = im.size
        work = to_jpeg_safe_mode(im).copy()

    work, cv_diag = _apply_cv_to_pil(
        work, profile, profile_name,
        manual_crop=manual_crop,
        rotate_deg=rotate_deg,
        clean_signature_bg=clean_signature_bg,
        auto_crop=auto_crop,
    )
    work_w, work_h = work.size
    max_bytes = int(profile["max_bytes"])
    qualities = list(profile.get("qualities", [90, 82, 74, 66, 58, 55]))
    scales = list(profile.get("scales", [1.0, 0.85, 0.7, 0.55, 0.4]))
    max_dim = int(profile.get("max_dim", 2000))
    min_dim = int(profile.get("min_dim", 300))
    min_quality = int(profile.get("min_quality", 50))
    base_scale = min(1.0, max_dim / max(work_w, work_h))

    smallest = None
    for s in scales:
        scale = min(s, base_scale)
        if scale <= 0:
            continue
        w, h = max(1, round(work_w * scale)), max(1, round(work_h * scale))
        if min(w, h) < min_dim and scale < base_scale:
            break
        frame = work if scale == 1.0 else work.resize((w, h), Image.LANCZOS)
        page_w = 595.0
        page_h = page_w * (h / max(1, w))
        for q in qualities:
            if q < min_quality:
                continue
            jpg_bytes = _encode_jpeg(frame, q)
            out_doc = pymupdf.open()
            try:
                page = out_doc.new_page(width=page_w, height=page_h)
                page.insert_image(page.rect, stream=jpg_bytes)
                pdf_bytes = out_doc.tobytes(garbage=4, deflate=True)
            finally:
                out_doc.close()
            if smallest is None or len(pdf_bytes) < smallest:
                smallest = len(pdf_bytes)
            if len(pdf_bytes) < max_bytes:
                dest.write_bytes(pdf_bytes)
                cv_diag["method"] = f"img_to_pdf+{cv_diag.get('method', 'compressed')}"
                return {
                    "width": w, "height": h, "quality": q,
                    "bytes": len(pdf_bytes), "bytes_before": before,
                    "orig_dims": (orig_w, orig_h),
                    "pages": 1, "cv": cv_diag,
                }
    raise SizeTargetError(
        f"could not pack image into PDF under {max_bytes} bytes "
        f"(smallest {smallest or before} bytes)"
    )


def prepare_pdf(src: Path, dest: Path, profile: dict[str, Any]) -> dict[str, Any]:
    """Optimize a PDF under ``max_bytes``: lossless deflate first, then adaptive scan compression."""
    import pymupdf

    max_bytes = int(profile["max_bytes"])
    before = src.stat().st_size
    with pymupdf.open(src) as doc:
        n_pages = doc.page_count
        if n_pages < 1:
            raise SizeTargetError("PDF has no pages")
        text_flags = [bool(doc[i].get_text().strip()) for i in range(n_pages)]

        # Pass 1: Lossless optimization (preserves selectable text & crisp vectors).
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            doc.save(tmp, garbage=4, deflate=True, clean=True)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            tmp = None
        candidates: list[bytes] = []
        if tmp is not None and tmp.exists():
            candidates.append(tmp.read_bytes())
            tmp.unlink()
        if before < max_bytes:
            candidates.append(src.read_bytes())
        candidates.sort(key=len)
        best = candidates[0] if candidates else b""

        if best and len(best) < max_bytes:
            dest.write_bytes(best)
            with pymupdf.open(dest) as out:
                assert out.page_count == n_pages, "page count changed"
                for i, had in enumerate(text_flags):
                    if had and not out[i].get_text().strip():
                        raise SizeTargetError(f"prepared PDF lost readable text on page {i + 1}")
            return {
                "bytes": len(best), "bytes_before": before,
                "pages": n_pages, "text_pages": sum(1 for t in text_flags if t),
                "cv": {"method": "pdf_lossless", "reason": "optimized", "crop_applied": False},
            }

        # Pass 2: Adaptive page compression for large scanned PDFs (all pages preserved).
        smallest = len(best) if best else before
        compress_steps = (
            (150, 78), (130, 68), (115, 60), (100, 55), (85, 50), (72, 45),
        )
        for dpi, q in compress_steps:
            out_doc = pymupdf.open()
            try:
                for i in range(n_pages):
                    src_page = doc[i]
                    pix = src_page.get_pixmap(dpi=dpi, alpha=False)
                    im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    jpg_bytes = _encode_jpeg(im, q)
                    new_page = out_doc.new_page(width=src_page.rect.width, height=src_page.rect.height)
                    new_page.insert_image(new_page.rect, stream=jpg_bytes)
                pdf_bytes = out_doc.tobytes(garbage=4, deflate=True)
            finally:
                out_doc.close()
            if len(pdf_bytes) < smallest:
                smallest = len(pdf_bytes)
            if len(pdf_bytes) < max_bytes:
                dest.write_bytes(pdf_bytes)
                return {
                    "bytes": len(pdf_bytes), "bytes_before": before,
                    "pages": n_pages, "text_pages": 0,
                    "quality": q,
                    "cv": {
                        "method": f"pdf_compress:{dpi}dpi_q{q}",
                        "reason": "compressed_scans",
                        "crop_applied": False,
                    },
                }

        raise SizeTargetError(
            f"PDF {before} bytes cannot safely reach {max_bytes} bytes "
            f"(optimized smallest {smallest} bytes, {n_pages} pages kept; operator action required)"
        )


def _passthrough(src: Path, dest: Path, profile: dict[str, Any]) -> dict[str, Any]:
    max_bytes = int(profile["max_bytes"])
    data = src.read_bytes()
    if len(data) >= max_bytes:
        raise SizeTargetError(
            f"unclassified file {len(data)} bytes meets no shrinking rule and exceeds "
            f"{max_bytes} bytes; operator classification required"
        )
    dest.write_bytes(data)
    return {"bytes": len(data), "bytes_before": len(data), "passthrough": True}


def _run_profile_prepare(
    src: Path,
    dest: Path,
    ext: str,
    profile: dict,
    profile_name: str,
    *,
    manual_crop: tuple[float, float, float, float] | None = None,
    rotate_deg: int = 0,
    clean_signature_bg: bool | None = None,
    auto_crop: bool | None = None,
) -> dict:
    """Dispatch preparation across passthrough, PDF compression, PDF->JPG, JPG->PDF, and image CV."""
    if profile.get("passthrough"):
        return _passthrough(src, dest, profile)
    out_ext = _resolve_output_ext(ext, profile)
    if ext == ".pdf":
        if out_ext == ".jpg":
            return prepare_pdf_as_jpeg(
                src, dest, profile, profile_name=profile_name, auto_crop=auto_crop
            )
        return prepare_pdf(src, dest, profile)
    if out_ext == ".pdf":
        return prepare_image_as_pdf(
            src, dest, profile,
            profile_name=profile_name,
            manual_crop=manual_crop,
            rotate_deg=rotate_deg,
            clean_signature_bg=clean_signature_bg,
            auto_crop=auto_crop,
        )
    return prepare_image(
        src, dest, profile,
        profile_name=profile_name,
        manual_crop=manual_crop,
        rotate_deg=rotate_deg,
        clean_signature_bg=clean_signature_bg,
        auto_crop=auto_crop,
    )


def prepare_document(
    staged_path: Path | str,
    cfg: FormFlowConfig,
    job_id: int | None = None,
    applicant_name: str | None = None,
    case_id: str | None = None,
    appl_number: str | None = None,
    doctype_hint: str | None = None,
    ocr_hints: dict | None = None,
    fast_classify: bool = False,
) -> dict:
    """Classify + prepare one staged file. Returns the document row as dict.

    Never modifies the source. Never invents an application number.
    """
    src = Path(staged_path)
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        if not src.exists() or not src.is_file():
            raise ValueError(f"source not found: {src}")
        try:
            resolve_within(src, cfg.data_dir)
        except ValueError as e:
            raise ValueError(f"source outside app data: {e}") from e
        ext = src.suffix.lower()
        orig_bytes = src.stat().st_size
        orig_hash = sha256_path(src)
        # Original filename metadata: prefer the staged name's embedded
        # inbox name, else the staged filename itself.
        orig_filename = src.name

        if ext not in SUPPORTED_PREPARE_EXTS:
            return dict(create_document(
                conn, orig_filename=orig_filename, orig_path=str(src),
                doctype="unknown", status="needs_attention",
                intake_job_id=job_id, case_id=None,
                sha256_orig=orig_hash, mime=mime_for(ext),
                byte_size_orig=orig_bytes,
                profile_name="none", profile_version="none",
                error=f"unsupported format '{ext or '(none)'}' for preparation; JPG/PNG/PDF only",
            ))

        width = height = None
        if ext in SUPPORTED_IMAGE_EXTS:
            try:
                with Image.open(src) as im:
                    im.load()
                    oriented = ImageOps.exif_transpose(im)
                    width, height = oriented.size
            except Exception:
                width = height = None

        profiles, version = load_profiles(cfg)
        dyn_rules = custom_classifier_rules(profiles)
        label_hint: str | None = None
        if doctype_hint in profiles:
            doctype, conf, reason = doctype_hint, 1.0, "explicit operator choice"
            profile_lookup = doctype_hint
            label_hint = doctype_hint
        else:
            cls_res = classify_document(
                orig_filename, ext, width, height, ocr_hints, custom_rules=dyn_rules
            )
            doctype, conf, reason = cls_res.doctype, cls_res.confidence, cls_res.reason
            profile_lookup = cls_res.category if cls_res.category in profiles else doctype
            if cls_res.category not in ("unknown", "supporting_ll"):
                label_hint = cls_res.category
            if fast_classify and doctype == "unknown" and ext in SUPPORTED_IMAGE_EXTS:
                fast = _fast_classify_image(
                    src, cfg, orig_filename, ext, width, height, custom_rules=dyn_rules
                )
                if fast is not None:
                    f_doctype, f_cat, f_conf, f_reason = fast
                    doctype, conf, reason = f_doctype, f_conf, f_reason
                    profile_lookup = f_cat if f_cat in profiles else f_doctype
                    if f_cat not in ("unknown", "supporting_ll"):
                        label_hint = f_cat
            elif ext == ".pdf" and cls_res.category in ("unknown", "supporting_ll"):
                fast_pdf = _fast_classify_pdf(src, cfg, orig_filename, custom_rules=dyn_rules)
                if fast_pdf is not None:
                    f_doctype, f_cat, f_conf, f_reason = fast_pdf
                    doctype, conf, reason = f_doctype, f_conf, f_reason
                    profile_lookup = f_cat if f_cat in profiles else f_doctype
                    if f_cat not in ("unknown", "supporting_ll"):
                        label_hint = f_cat
        profile_name, profile = get_profile(profiles, profile_lookup)

        out_ext = _resolve_output_ext(ext, profile)
        filename = build_prepared_filename(
            applicant_name, case_id, appl_number, doctype, out_ext, job_id)
        dest = prepared_dest(cfg, filename)

        info = _run_profile_prepare(src, dest, ext, profile, profile_name)

        prep_hash = sha256_path(dest)
        status = "ready"
        error = None
        if doctype == "unknown":
            # Copied but still needs operator classification downstream.
            status = "needs_attention"
            error = f"prepared copy made; {reason}"
        prep_meta = _build_prep_meta(info, profile_name)
        created = create_document(
            conn, orig_filename=orig_filename, orig_path=str(src),
            prepared_path=str(dest), doctype=doctype, status=status,
            intake_job_id=job_id, case_id=None,
            sha256_orig=orig_hash, sha256_prepared=prep_hash,
            mime=mime_for(out_ext, prepared=not profile.get("passthrough")),
            byte_size_orig=orig_bytes,
            byte_size_prepared=info["bytes"],
            profile_name=profile_name, profile_version=version,
            error=error,
            prep_meta=prep_meta,
        )
        if status == "ready":
            export_to_inbox(
                conn, created["id"], cfg, dest, orig_filename, doctype,
                label_hint=label_hint,
            )
            created = get_document(conn, created["id"]) or created
        return dict(created)
    except SizeTargetError as e:
        profiles, version = load_profiles(cfg)
        przys = doctype_hint if (doctype_hint in profiles) else "unknown"
        try:
            ohash = sha256_path(src)
        except OSError:
            ohash = None
        try:
            obytes = src.stat().st_size
        except OSError:
            obytes = None
        return dict(create_document(
            conn, orig_filename=src.name, orig_path=str(src),
            doctype=przys if isinstance(przys, str) else "unknown",
            status="needs_attention", intake_job_id=job_id, case_id=None,
            sha256_orig=ohash, mime=mime_for(src.suffix.lower()),
            byte_size_orig=obytes,
            profile_name=przys if isinstance(przys, str) else "unknown",
            profile_version=version, error=str(e),
        ))
    finally:
        conn.close()


def _build_prep_meta(info: dict[str, Any], profile_name: str) -> str:
    """Serialize redacted preparation metadata (no PII) for UI & provenance."""
    cv_info = info.get("cv") if isinstance(info.get("cv"), dict) else {}
    payload: dict[str, Any] = {
        "profile": profile_name,
        "crop_applied": bool(cv_info.get("crop_applied", False)),
        "method": cv_info.get("method", "passthrough" if info.get("passthrough") else "none"),
        "reason": cv_info.get("reason", "passthrough" if info.get("passthrough") else "compressed"),
        "cleaned_bg": bool(cv_info.get("cleaned_bg", False)),
        "orig_dims": list(info["orig_dims"]) if info.get("orig_dims") else cv_info.get("orig_dims"),
        "prepared_dims": [info["width"], info["height"]] if info.get("width") and info.get("height") else None,
        "quality": info.get("quality"),
        "bytes_before": info.get("bytes_before"),
        "bytes_after": info.get("bytes"),
    }
    return json.dumps(payload, separators=(",", ":"))


def parse_prep_meta(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def reprepare_document(
    doc_id: int,
    cfg: FormFlowConfig,
    doctype_hint: str | None = None,
    manual_crop: tuple[float, float, float, float] | None = None,
    rotate_deg: int = 0,
    clean_signature_bg: bool | None = None,
    auto_crop: bool | None = None,
    label_hint: str | None = None,
) -> dict:
    """Re-prepare an existing document in-place from its untouched staged original.

    Powers post-OCR auto-repreparation (when an unlabeled phone capture is
    recognized as an Aadhaar/DL/LL card) as well as operator classification,
    auto-crop retry, and interactive manual cropping from the dashboard.
    Never modifies ``orig_path`` or creates duplicate document rows.
    """
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        doc = get_document(conn, doc_id)
        if doc is None:
            raise ValueError(f"unknown document: {doc_id}")
        src = Path(doc["orig_path"] or "")
        if not src.exists() or not src.is_file():
            raise ValueError(f"staged original not found for document {doc_id}")
        resolve_within(src, cfg.data_dir)

        ext = src.suffix.lower()
        if ext not in SUPPORTED_PREPARE_EXTS:
            raise ValueError(f"unsupported format '{ext or '(none)'}' for preparation")

        profiles, version = load_profiles(cfg)
        chosen_type = (doctype_hint or doc["doctype"] or "unknown").strip().lower()
        if chosen_type not in profiles:
            chosen_type = "unknown"
        profile_name, profile = get_profile(profiles, chosen_type)

        case_code = None
        appl_number = None
        applicant_name = None
        if doc["case_id"]:
            crow = conn.execute(
                "SELECT c.case_id, c.appl_number, cust.display_name "
                "FROM cases c LEFT JOIN customers cust ON cust.id = c.customer_id "
                "WHERE c.id = ?",
                (doc["case_id"],),
            ).fetchone()
            if crow:
                case_code = crow["case_id"]
                appl_number = crow["appl_number"]
                applicant_name = crow["display_name"]

        out_ext = _resolve_output_ext(ext, profile)
        filename = build_prepared_filename(
            applicant_name, case_code, appl_number, chosen_type, out_ext, doc["intake_job_id"]
        )
        dest = prepared_dest(cfg, filename)

        try:
            info = _run_profile_prepare(
                src, dest, ext, profile, profile_name,
                manual_crop=manual_crop,
                rotate_deg=rotate_deg,
                clean_signature_bg=clean_signature_bg,
                auto_crop=auto_crop,
            )
        except SizeTargetError as e:
            return dict(update_document(
                conn, doc_id,
                doctype=chosen_type,
                profile_name=profile_name,
                profile_version=version,
                status="needs_attention",
                error=str(e),
            ))

        prep_hash = sha256_path(dest)
        status = "needs_attention" if chosen_type == "unknown" else "ready"
        error = "prepared copy made; operator classification required" if chosen_type == "unknown" else None
        prep_meta = _build_prep_meta(info, profile_name)
        updated = update_document(
            conn, doc_id,
            doctype=chosen_type,
            prepared_path=str(dest),
            sha256_prepared=prep_hash,
            mime=mime_for(out_ext, prepared=not profile.get("passthrough")),
            byte_size_prepared=info["bytes"],
            profile_name=profile_name,
            profile_version=version,
            status=status,
            error=error,
            prep_meta=prep_meta,
        )
        if status == "ready":
            export_to_inbox(
                conn, doc_id, cfg, dest, doc["orig_filename"], chosen_type,
                label_hint=label_hint or chosen_type,
            )
            updated = get_document(conn, doc_id) or updated
        if doc["case_id"]:
            cv_method = (info.get("cv") or {}).get("method", "compressed")
            conn.execute(
                "INSERT INTO case_events (case_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (
                    doc["case_id"],
                    "document_prepared",
                    f"doc #{doc_id} prepared as {chosen_type} ({cv_method}, {info['bytes']} B)",
                    utcnow_iso(),
                ),
            )
            conn.commit()
        return dict(updated)
    finally:
        conn.close()


def retry_document(doc_id: int, cfg: FormFlowConfig) -> dict:
    """Re-run preparation in-place from the preserved original (no duplicate rows)."""
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        doc = get_document(conn, doc_id)
        if doc is None:
            raise ValueError(f"unknown document: {doc_id}")
        if doc["status"] not in ("failed", "needs_attention"):
            raise ValueError(f"document {doc_id} is {doc['status']}; only failed/needs_attention can retry")
        hint = doc["profile_name"] if (doc["profile_name"] and doc["profile_name"] != "unknown") else (
            doc["doctype"] if doc["doctype"] != "unknown" else None
        )
    finally:
        conn.close()

    updated = reprepare_document(doc_id, cfg, doctype_hint=hint)
    try:
        from .extract import run_extraction

        run_extraction(updated["id"], cfg)
        conn2 = connect(cfg.db_path)
        try:
            return dict(get_document(conn2, updated["id"]))
        finally:
            conn2.close()
    except Exception:
        return updated


def reprepare_all_documents(cfg: FormFlowConfig) -> dict[str, int]:
    """Re-prepare all existing staged documents against the active profile preset.

    Originals in ``data/staging/`` are untouched; prepared copies and ``Inbox\\Ready_*``
    exports are updated cleanly. Returns ``{"updated": N, "failed": M, "skipped": K}``.
    """
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT id, orig_path, doctype, profile_name, status FROM documents ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    profiles, _ = load_profiles(cfg)
    updated = 0
    failed = 0
    skipped = 0
    for r in rows:
        src = Path(r["orig_path"] or "")
        if not src.is_file() or src.suffix.lower() not in SUPPORTED_PREPARE_EXTS:
            skipped += 1
            continue
        chosen = (r["profile_name"] or r["doctype"] or "unknown").strip().lower()
        if chosen == "unknown":
            skipped += 1
            continue
        if chosen not in profiles:
            chosen = "supporting_ll"
        try:
            res = reprepare_document(r["id"], cfg, doctype_hint=chosen, label_hint=chosen)
            if res.get("status") == "ready":
                updated += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"updated": updated, "failed": failed, "skipped": skipped}

