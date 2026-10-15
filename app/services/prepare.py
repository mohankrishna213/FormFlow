"""Document preparation: classify + per-type compress to prepared copies (Phase 3).

- Original (staging) bytes are never modified; every output is a new file.
- Images: EXIF transpose, aspect preserved, never upscaled, JPEG-safe modes.
- PDFs: all pages kept, text preserved, optimize-only (no rasterize/flatten).
- Outputs must be strictly below the profile max_bytes or the document is
  marked needs_attention with a reason (never silently over-compressed).
- No automatic cropping. No invented application numbers.
"""
from __future__ import annotations

import hashlib
import io
import shutil
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from ..config import FormFlowConfig
from ..db import connect, create_document, init_db, update_document
from .classify import suggest_doctype
from .filters import resolve_within, sanitize_filename
from .profiles import get_profile, load_profiles

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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def prepare_image(src: Path, dest: Path, profile: dict[str, Any]) -> dict[str, Any]:
    before = src.stat().st_size
    with Image.open(src) as im:
        im.load()
        needs_transpose = (im.getexif().get(274, 1) not in (1, None))
        im = ImageOps.exif_transpose(im)
        orig_w, orig_h = im.size
        work = to_jpeg_safe_mode(im)
        # Work on a detached copy; original file untouched.
        work = work.copy()
    aspect_before = orig_w / orig_h
    max_bytes = int(profile["max_bytes"])
    src_is_jpeg = src.suffix.lower() in (".jpg", ".jpeg")
    if src_is_jpeg and not needs_transpose and before < max_bytes:
        # Already a compliant JPEG: never inflate it by re-encoding.
        data = src.read_bytes()
        dest.write_bytes(data)
        return {
            "width": orig_w, "height": orig_h, "quality": "copy",
            "bytes": len(data), "bytes_before": before,
            "aspect_before": aspect_before, "aspect_after": aspect_before,
            "orig_dims": (orig_w, orig_h),
        }
    qualities = list(profile.get("qualities", [90, 80, 70, 60, 55]))
    scales = list(profile.get("scales", [1.0, 0.85, 0.7, 0.55, 0.4]))
    max_dim = int(profile.get("max_dim", 1600))
    min_dim = int(profile.get("min_dim", 200))
    min_quality = int(profile.get("min_quality", 55))

    base_scale = min(1.0, max_dim / max(orig_w, orig_h))
    tried = []
    for s in scales:
        scale = min(s, base_scale)
        if scale <= 0:
            continue
        w, h = max(1, round(orig_w * scale)), max(1, round(orig_h * scale))
        if min(w, h) < min_dim and scale < base_scale:
            # Would drop below the readable floor: stop shrinking.
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
                    "aspect_before": aspect_before, "aspect_after": w / h,
                    "orig_dims": (orig_w, orig_h),
                }
    raise SizeTargetError(
        f"could not reach {max_bytes} bytes within quality>={min_quality} "
        f"and min_dim>={min_dim} (tried {len(tried)} combos, smallest {min(t[3] for t in tried) if tried else 'n/a'} bytes)"
    )


def prepare_pdf(src: Path, dest: Path, profile: dict[str, Any]) -> dict[str, Any]:
    import pymupdf

    max_bytes = int(profile["max_bytes"])
    before = src.stat().st_size
    with pymupdf.open(src) as doc:
        n_pages = doc.page_count
        if n_pages < 1:
            raise SizeTargetError("PDF has no pages")
        text_flags = [bool(doc[i].get_text().strip()) for i in range(n_pages)]
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        try:
            doc.save(tmp, garbage=4, deflate=True, clean=True)
        except Exception:
            # Fall back to a byte copy candidate below.
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
        if not best or len(best) >= max_bytes:
            smallest = len(best) if best else before
            raise SizeTargetError(
                f"PDF {before} bytes cannot safely reach {max_bytes} bytes "
                f"(optimized smallest {smallest} bytes, {n_pages} pages kept; operator action required)"
            )
        dest.write_bytes(best)
        # Verify pages + text preserved.
        with pymupdf.open(dest) as out:
            assert out.page_count == n_pages, "page count changed"
            for i, had in enumerate(text_flags):
                if had and not out[i].get_text().strip():
                    raise SizeTargetError(f"prepared PDF lost readable text on page {i + 1}")
    return {"bytes": len(best), "bytes_before": before,
            "pages": n_pages, "text_pages": sum(1 for t in text_flags if t)}


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


def prepare_document(
    staged_path: Path | str,
    cfg: FormFlowConfig,
    job_id: int | None = None,
    applicant_name: str | None = None,
    case_id: str | None = None,
    appl_number: str | None = None,
    doctype_hint: str | None = None,
    ocr_hints: dict | None = None,
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
        if doctype_hint in profiles:
            doctype, conf, reason = doctype_hint, 1.0, "explicit operator choice"
        else:
            doctype, conf, reason = suggest_doctype(
                orig_filename, ext, width, height, ocr_hints)
        profile_name, profile = get_profile(profiles, doctype)

        out_ext = ".pdf" if ext == ".pdf" else ".jpg"
        filename = build_prepared_filename(
            applicant_name, case_id, appl_number, doctype, out_ext, job_id)
        dest = prepared_dest(cfg, filename)

        if profile.get("passthrough"):
            info = _passthrough(src, dest, profile)
        elif ext == ".pdf":
            info = prepare_pdf(src, dest, profile)
        else:
            info = prepare_image(src, dest, profile)

        prep_hash = sha256_path(dest)
        status = "ready"
        error = None
        if doctype == "unknown":
            # Copied but still needs operator classification downstream.
            status = "needs_attention"
            error = f"prepared copy made; {reason}"
        return dict(create_document(
            conn, orig_filename=orig_filename, orig_path=str(src),
            prepared_path=str(dest), doctype=doctype, status=status,
            intake_job_id=job_id, case_id=None,
            sha256_orig=orig_hash, sha256_prepared=prep_hash,
            mime=mime_for(ext), byte_size_orig=orig_bytes,
            byte_size_prepared=info["bytes"],
            profile_name=profile_name, profile_version=version,
            error=error,
        ))
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


def retry_document(doc_id: int, cfg: FormFlowConfig) -> dict:
    """Re-run preparation from the preserved original (after profile fix)."""
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        from ..db import get_document

        doc = get_document(conn, doc_id)
        if doc is None:
            raise ValueError(f"unknown document: {doc_id}")
        if doc["status"] not in ("failed", "needs_attention"):
            raise ValueError(f"document {doc_id} is {doc['status']}; only failed/needs_attention can retry")
        orig = doc["orig_path"]
        hint = doc["doctype"] if doc["doctype"] != "unknown" else None
        job_id = doc["intake_job_id"]
        conn.close()
        new_doc = prepare_document(orig, cfg, job_id=job_id, doctype_hint=hint)
        try:
            from .extract import run_extraction

            run_extraction(new_doc["id"], cfg)
            from ..db import get_document as _get

            conn2 = connect(cfg.db_path)
            try:
                return dict(_get(conn2, new_doc["id"]))
            finally:
                conn2.close()
        except Exception:
            return new_doc
    finally:
        try:
            conn.close()
        except Exception:
            pass
