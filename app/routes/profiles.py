"""Dev-Mode Document Profiles & Portal Presets editor (/profiles)."""
from __future__ import annotations

from urllib.parse import urlencode

from flask import Blueprint, redirect, render_template, request, url_for

from .. import validate_csrf
from ..services.prepare import reprepare_all_documents
from ..services.profiles import (
    PROTECTED_DOCTYPE_KEYS,
    activate_preset,
    create_preset,
    delete_document_profile,
    delete_preset,
    load_profile_store,
    slugify_key,
    upsert_document_profile,
)
from . import get_cfg

profiles_bp = Blueprint("profiles_bp", __name__)


def _redirect_profiles(msg: str | None = None, err: str | None = None):
    params = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    base = url_for("profiles_bp.profiles_page")
    return redirect(f"{base}?{urlencode(params)}" if params else base)


@profiles_bp.get("/profiles")
def profiles_page():
    cfg = get_cfg()
    store = load_profile_store(cfg)
    active_key = store["active_preset"]
    active_preset = store["presets"][active_key]
    return render_template(
        "profiles.html",
        store=store,
        active_key=active_key,
        active_preset=active_preset,
        protected_keys=PROTECTED_DOCTYPE_KEYS,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@profiles_bp.post("/profiles/preset/activate")
def preset_activate():
    validate_csrf()
    cfg = get_cfg()
    key = (request.form.get("preset_key") or "").strip().lower()
    try:
        store = activate_preset(cfg, key)
        label = store["presets"][store["active_preset"]]["label"]
        return _redirect_profiles(msg=f"Switched active portal preset to '{label}'.")
    except ValueError as e:
        return _redirect_profiles(err=str(e))


@profiles_bp.post("/profiles/preset/create")
def preset_create():
    validate_csrf()
    cfg = get_cfg()
    label = (request.form.get("label") or "").strip()
    raw_key = (request.form.get("preset_key") or "").strip() or slugify_key(label)
    desc = (request.form.get("description") or "").strip()
    clone_from = (request.form.get("clone_from") or "").strip() or None
    if not label:
        return _redirect_profiles(err="Preset name is required.")
    try:
        store = create_preset(cfg, raw_key, label, description=desc, clone_from=clone_from)
        return _redirect_profiles(msg=f"Created and activated portal preset '{store['presets'][store['active_preset']]['label']}'.")
    except ValueError as e:
        return _redirect_profiles(err=str(e))


@profiles_bp.post("/profiles/preset/delete")
def preset_delete():
    validate_csrf()
    cfg = get_cfg()
    key = (request.form.get("preset_key") or "").strip().lower()
    try:
        delete_preset(cfg, key)
        return _redirect_profiles(msg=f"Deleted portal preset '{key}'.")
    except ValueError as e:
        return _redirect_profiles(err=str(e))


@profiles_bp.post("/profiles/doc/save")
def doc_profile_save():
    validate_csrf()
    cfg = get_cfg()
    label = (request.form.get("label") or "").strip()
    raw_key = (request.form.get("doc_key") or "").strip() or slugify_key(label)
    export_tag = (request.form.get("export_tag") or "").strip() or slugify_key(label) or raw_key
    crop_mode = (request.form.get("crop_mode") or "document").strip().lower()
    output_mode = (request.form.get("output_mode") or "jpg").strip().lower()
    description = (request.form.get("description") or "").strip()
    filename_hints = request.form.get("filename_hints", "")
    ocr_keywords = request.form.get("ocr_keywords", "")

    if not label and not raw_key:
        return _redirect_profiles(err="Display name or key is required.")

    try:
        max_kb = float(request.form.get("max_kb") or 200)
        if max_kb < 1 or max_kb > 25600:
            raise ValueError("Max size must be between 1 KB and 25,600 KB.")
        max_bytes = int(round(max_kb * 1024))
    except ValueError as e:
        return _redirect_profiles(err=f"Invalid max size (KB): {e}")

    try:
        max_dim = int(request.form.get("max_dim") or 2000)
    except ValueError:
        max_dim = 2000

    auto_crop = (request.form.get("auto_crop") or "").strip().lower() in ("1", "true", "yes", "on")
    if crop_mode == "none":
        auto_crop = False
    clean_bg = (request.form.get("clean_background") or "").strip().lower() in ("1", "true", "yes", "on")

    payload = {
        "label": label or raw_key.replace("_", " ").title(),
        "export_tag": export_tag,
        "output_mode": output_mode,
        "max_bytes": max_bytes,
        "max_dim": max_dim,
        "crop_mode": crop_mode,
        "auto_crop": auto_crop,
        "clean_background": clean_bg,
        "description": description,
        "filename_hints": filename_hints,
        "ocr_keywords": ocr_keywords,
    }
    if crop_mode == "photo":
        payload["target_aspect"] = 0.7778
    if raw_key == "unknown":
        payload["passthrough"] = True

    try:
        store = upsert_document_profile(cfg, raw_key, payload)
        saved_key = slugify_key(raw_key)
        saved_lbl = store["presets"][store["active_preset"]]["profiles"].get(saved_key, {}).get("label", raw_key)
        return _redirect_profiles(msg=f"Saved document profile '{saved_lbl}' (< {int(round(max_kb))} KB).")
    except ValueError as e:
        return _redirect_profiles(err=str(e))


@profiles_bp.post("/profiles/doc/delete")
def doc_profile_delete():
    validate_csrf()
    cfg = get_cfg()
    key = (request.form.get("doc_key") or "").strip().lower()
    try:
        delete_document_profile(cfg, key)
        return _redirect_profiles(msg=f"Deleted custom document profile '{key}'.")
    except ValueError as e:
        return _redirect_profiles(err=str(e))


@profiles_bp.post("/profiles/reprepare-all")
def profiles_reprepare_all():
    validate_csrf()
    cfg = get_cfg()
    summary = reprepare_all_documents(cfg)
    msg = (
        f"Re-prepared {summary['updated']} existing document(s) using active preset"
        + (f" ({summary['failed']} could not meet target size)" if summary["failed"] else "")
        + (f", {summary['skipped']} skipped." if summary["skipped"] else ".")
    )
    return _redirect_profiles(msg=msg)
