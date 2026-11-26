"""Dynamic per-document processing profiles & Portal Presets (v5).

Profiles live in configurable JSON (default ``profiles.json``).
Supports:
- Multiple named Portal Presets (e.g. ``sarathi``, ``exam_scholarship``,
  ``passport_meeseva``, plus user-created custom presets).
- Arbitrary user-defined document types (e.g. ``voter_id``, ``caste_cert``,
  ``medical_cert``) with custom KB limits, crop modes, ``Inbox\\Ready_*``
  export tags, display labels, and optional filename/OCR auto-detection keywords.
- Full backward compatibility with legacy flat v4 ``profiles.json`` files
  and missing/corrupt files (falls back cleanly to safe built-in defaults).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..config import FormFlowConfig

DOCTYPE_KEYS = (
    "photo",
    "signature",
    "aadhaar",
    "ssc",
    "learner_licence",
    "pan",
    "driving_licence",
    "tr_document",
    "supporting_ll",
    "unknown",
)

# Built-in fallback keys that cannot be deleted because the pipeline relies on them.
PROTECTED_DOCTYPE_KEYS = frozenset({"photo", "signature", "supporting_ll", "unknown"})
_RESERVED_TOP_KEYS = frozenset({"_note", "version", "active_preset", "presets"})
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

_VALID_CROP_MODES = ("document", "photo", "signature", "none")
_VALID_OUTPUT_MODES = ("jpg", "jpg_or_pdf", "pdf")

_DOC_200KB_BASE: dict[str, Any] = {
    "max_bytes": 204800,
    "output": "JPEG",
    "output_mode": "jpg",
    "max_dim": 2000,
    "min_dim": 400,
    "min_quality": 55,
    "qualities": [90, 82, 74, 66, 58, 55],
    "scales": [1.0, 0.85, 0.7, 0.55, 0.4],
    "auto_crop": True,
    "crop_mode": "document",
}

_DEFAULTS: dict[str, dict[str, Any]] = {
    "photo": {
        "label": "Passphoto",
        "export_tag": "photo",
        "max_bytes": 51200,
        "output": "JPEG",
        "output_mode": "jpg",
        "max_dim": 1600,
        "min_dim": 300,
        "min_quality": 55,
        "qualities": [92, 85, 78, 70, 62, 55],
        "scales": [1.0, 0.85, 0.7, 0.55, 0.4],
        "auto_crop": True,
        "crop_mode": "photo",
        "target_aspect": 0.7778,
        "description": "Passphoto / Photo ID target <50 KB; auto-crops bars and frames face to 3.5:4.5",
    },
    "signature": {
        "label": "Signature",
        "export_tag": "signature",
        "max_bytes": 51200,
        "output": "JPEG",
        "output_mode": "jpg",
        "max_dim": 1200,
        "min_dim": 200,
        "min_quality": 60,
        "qualities": [92, 85, 78, 70, 65, 60],
        "scales": [1.0, 0.85, 0.7, 0.55, 0.4],
        "auto_crop": True,
        "crop_mode": "signature",
        "clean_background": True,
        "description": "Signature target <50 KB; padded ink crop with paper whitening",
    },
    "aadhaar": {
        "label": "Aadhaar Card",
        "export_tag": "aadhaar",
        "max_bytes": 512000,
        "output": "JPEG",
        "output_mode": "jpg",
        "max_dim": 2200,
        "min_dim": 400,
        "min_quality": 55,
        "qualities": [90, 82, 74, 66, 58, 55],
        "scales": [1.0, 0.85, 0.7, 0.55, 0.4],
        "auto_crop": True,
        "crop_mode": "document",
        "description": "Aadhaar card target <500 KB; auto-crops backgrounds and rectifies card",
    },
    "ssc": {
        **_DOC_200KB_BASE,
        "label": "10th Memo / SSC",
        "export_tag": "10th_memo",
        "output_mode": "jpg",
        "description": "10th SSC Marks/Grade Memo target <200 KB",
    },
    "learner_licence": {
        **_DOC_200KB_BASE,
        "label": "LL Document",
        "export_tag": "ll",
        "output_mode": "jpg_or_pdf",
        "description": "Learner's Licence (LL / Form 3) target <200 KB; accepts JPG or PDF",
    },
    "pan": {
        **_DOC_200KB_BASE,
        "label": "PAN Card",
        "export_tag": "pan",
        "output_mode": "jpg",
        "description": "PAN card target <200 KB; auto-crops card",
    },
    "driving_licence": {
        **_DOC_200KB_BASE,
        "label": "Driving Licence",
        "export_tag": "dl",
        "output_mode": "jpg",
        "description": "Driving Licence (DL / Form 7) target <200 KB; auto-crops card",
    },
    "tr_document": {
        **_DOC_200KB_BASE,
        "label": "TR / Vehicle RC",
        "export_tag": "tr",
        "output_mode": "jpg_or_pdf",
        "description": "Temporary Registration (TR) / Vehicle RC target <200 KB; accepts JPG or PDF",
    },
    "supporting_ll": {
        **_DOC_200KB_BASE,
        "label": "Other Document",
        "export_tag": "doc",
        "output_mode": "jpg_or_pdf",
        "description": "Other supporting documents target <200 KB; accepts JPG or PDF",
    },
    "unknown": {
        "label": "Unclassified",
        "export_tag": "doc",
        "max_bytes": 512000,
        "output_mode": "jpg_or_pdf",
        "passthrough": True,
        "auto_crop": False,
        "crop_mode": "none",
        "description": "Unclassified files are copied only until classified",
    },
}


def slugify_key(raw: str) -> str:
    """Convert arbitrary user text into a safe lowercase underscore profile/preset key."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if cleaned and cleaned[0].isdigit():
        cleaned = f"doc_{cleaned}"
    return cleaned[:40]


def validate_slug(key: str) -> str:
    k = (key or "").strip().lower()
    if not _SLUG_RE.match(k) or k in _RESERVED_TOP_KEYS:
        raise ValueError(
            "Key must be 2–40 lowercase letters, digits, or underscores starting with a letter."
        )
    return k


def _clean_keyword_list(val: Any) -> list[str]:
    if isinstance(val, str):
        items = [s.strip().lower() for s in re.split(r"[,;\n]+", val)]
    elif isinstance(val, list):
        items = [str(s).strip().lower() for s in val]
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and len(item) >= 2 and item not in seen:
            seen.add(item)
            out.append(item[:60])
    return out[:30]


def _default_for_key(key: str) -> dict[str, Any]:
    if key in _DEFAULTS:
        return dict(_DEFAULTS[key])
    return {
        **_DOC_200KB_BASE,
        "label": key.replace("_", " ").title(),
        "export_tag": key[:24],
        "description": "",
    }


def sanitize_profile_entry(key: str, raw_entry: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a raw profile entry onto safe defaults and sanitize all numeric/boolean bounds."""
    p = _default_for_key(key)
    if isinstance(raw_entry, dict):
        for field in (
            "label", "export_tag", "max_bytes", "output", "output_mode", "max_dim", "min_dim",
            "min_quality", "qualities", "scales", "passthrough", "description",
            "auto_crop", "crop_mode", "clean_background", "target_aspect",
            "filename_hints", "ocr_keywords",
        ):
            if field in raw_entry and raw_entry[field] not in (None, ""):
                p[field] = raw_entry[field]

    # Friendly label & export_tag
    p["label"] = str(p.get("label") or key.replace("_", " ").title()).strip()[:60]
    raw_tag = str(p.get("export_tag") or key).strip().lower()
    clean_tag = re.sub(r"[^a-z0-9_]+", "_", raw_tag).strip("_")[:24] or "doc"
    p["export_tag"] = clean_tag

    # Numeric bounds
    try:
        p["max_bytes"] = max(1024, min(25 * 1024 * 1024, int(p.get("max_bytes", 204800))))
    except (TypeError, ValueError):
        p["max_bytes"] = 204800
    for num_key, lo, hi in (("max_dim", 100, 6000), ("min_dim", 50, 3000), ("min_quality", 10, 95)):
        if num_key in p:
            try:
                p[num_key] = max(lo, min(hi, int(p[num_key])))
            except (TypeError, ValueError):
                pass
    if "max_dim" in p and "min_dim" in p and p["min_dim"] > p["max_dim"]:
        p["min_dim"] = max(50, p["max_dim"] // 2)

    if "target_aspect" in p:
        try:
            ta = float(p["target_aspect"])
            p["target_aspect"] = ta if 0.3 <= ta <= 3.0 else 0.7778
        except (TypeError, ValueError):
            p["target_aspect"] = 0.7778

    crop_mode = str(p.get("crop_mode") or "document").strip().lower()
    if crop_mode not in _VALID_CROP_MODES:
        crop_mode = "document"
    p["crop_mode"] = crop_mode

    out_mode = str(p.get("output_mode") or "jpg").strip().lower()
    if out_mode not in _VALID_OUTPUT_MODES:
        out_mode = "jpg"
    p["output_mode"] = out_mode

    if "passthrough" in p:
        p["passthrough"] = bool(p["passthrough"])
    if crop_mode == "none" and key != "unknown":
        p["auto_crop"] = False
    elif "auto_crop" in p:
        p["auto_crop"] = bool(p["auto_crop"])

    if "clean_background" in p:
        p["clean_background"] = bool(p["clean_background"])

    if isinstance(p.get("qualities"), list):
        qs = sorted({int(q) for q in p["qualities"] if isinstance(q, (int, float)) and 10 <= int(q) <= 95}, reverse=True)
        if qs:
            p["qualities"] = qs
    if isinstance(p.get("scales"), list):
        ss = sorted({float(s) for s in p["scales"] if isinstance(s, (int, float)) and 0.1 <= float(s) <= 1.0}, reverse=True)
        if ss:
            p["scales"] = ss or [1.0]

    for kw_key in ("filename_hints", "ocr_keywords"):
        if kw_key in p:
            cleaned_kws = _clean_keyword_list(p[kw_key])
            if cleaned_kws:
                p[kw_key] = cleaned_kws
            else:
                p.pop(kw_key, None)

    return p


def _sanitize_profile_map(raw_map: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Build a complete, sanitized profile dictionary preserving built-ins + any custom doc types."""
    out: dict[str, dict[str, Any]] = {}
    # Always ensure built-in keys exist first (preserving stable display order)
    for key in DOCTYPE_KEYS:
        raw_entry = raw_map.get(key) if isinstance(raw_map, dict) else None
        out[key] = sanitize_profile_entry(key, raw_entry if isinstance(raw_entry, dict) else None)
    # Also include ANY custom document types present in the file!
    if isinstance(raw_map, dict):
        for key, val in raw_map.items():
            if key in out or key in _RESERVED_TOP_KEYS or key.startswith("_"):
                continue
            if not isinstance(val, dict):
                continue
            if _SLUG_RE.match(key):
                out[key] = sanitize_profile_entry(key, val)
    return out


def _builtin_presets() -> dict[str, dict[str, Any]]:
    sarathi = _sanitize_profile_map(None)

    exam_raw = {k: dict(v) for k, v in sarathi.items()}
    exam_raw["photo"]["max_bytes"] = 30720
    exam_raw["photo"]["description"] = "Exam passphoto target <30 KB"
    exam_raw["signature"]["max_bytes"] = 20480
    exam_raw["signature"]["description"] = "Exam signature target <20 KB"
    for k in ("aadhaar", "ssc", "learner_licence", "pan", "driving_licence", "tr_document", "supporting_ll"):
        exam_raw[k]["max_bytes"] = 307200

    meeseva_raw = {k: dict(v) for k, v in sarathi.items()}
    for k in ("aadhaar", "ssc", "learner_licence", "pan", "driving_licence", "tr_document", "supporting_ll"):
        meeseva_raw[k]["max_bytes"] = 512000

    return {
        "sarathi": {
            "label": "Sarathi / Driving Licence",
            "description": "Photo & Signature <50 KB, Aadhaar <500 KB, all other documents <200 KB.",
            "profiles": sarathi,
        },
        "exam_scholarship": {
            "label": "Exam / Scholarship",
            "description": "Strict portal targets: Photo <30 KB, Signature <20 KB, IDs & memos <300 KB.",
            "profiles": _sanitize_profile_map(exam_raw),
        },
        "passport_meeseva": {
            "label": "Passport / MeeSeva",
            "description": "General service-desk targets: Photo & Signature <50 KB, all documents <500 KB.",
            "profiles": _sanitize_profile_map(meeseva_raw),
        },
    }


def load_profile_store(cfg: FormFlowConfig) -> dict[str, Any]:
    """Load the full dynamic store ``{version, active_preset, presets}``.

    Handles:
    - Modern v5 ``profiles.json`` with ``presets`` + ``active_preset``.
    - Legacy flat v4 ``profiles.json`` (wrapped seamlessly into ``sarathi`` preset).
    - Missing or corrupt files (falls back cleanly to built-in presets).
    """
    builtins = _builtin_presets()
    try:
        raw = json.loads(Path(cfg.processing_profile_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "version": "defaults",
            "active_preset": "sarathi",
            "presets": builtins,
        }

    if not isinstance(raw, dict):
        return {
            "version": "defaults",
            "active_preset": "sarathi",
            "presets": builtins,
        }

    version = str(raw.get("version", "1"))
    raw_presets = raw.get("presets")

    if isinstance(raw_presets, dict) and raw_presets:
        presets: dict[str, dict[str, Any]] = {}
        for pkey, pval in raw_presets.items():
            if not isinstance(pval, dict) or not _SLUG_RE.match(str(pkey)):
                continue
            presets[pkey] = {
                "label": str(pval.get("label") or pkey.replace("_", " ").title()).strip()[:80],
                "description": str(pval.get("description") or "").strip()[:240],
                "profiles": _sanitize_profile_map(pval.get("profiles")),
            }
        if not presets:
            presets = builtins
        active = str(raw.get("active_preset") or "").strip().lower()
        if active not in presets:
            active = "sarathi" if "sarathi" in presets else next(iter(presets))
        return {
            "version": version,
            "active_preset": active,
            "presets": presets,
        }

    # Legacy flat v4 format: wrap top-level document profiles as the active preset.
    flat_profiles = _sanitize_profile_map(raw)
    presets = dict(builtins)
    presets["sarathi"] = {
        "label": "Sarathi / Driving Licence",
        "description": "Loaded from flat profiles.json configuration.",
        "profiles": flat_profiles,
    }
    return {
        "version": version,
        "active_preset": "sarathi",
        "presets": presets,
    }


def save_profile_store(cfg: FormFlowConfig, store: dict[str, Any]) -> dict[str, Any]:
    """Atomically write the dynamic profile store to ``cfg.processing_profile_path``."""
    presets = store.get("presets")
    if not isinstance(presets, dict) or not presets:
        raise ValueError("At least one preset is required.")
    active = str(store.get("active_preset") or "")
    if active not in presets:
        active = next(iter(presets))

    clean_presets: dict[str, dict[str, Any]] = {}
    for pkey, pval in presets.items():
        k = validate_slug(pkey)
        clean_presets[k] = {
            "label": str(pval.get("label") or k.replace("_", " ").title()).strip()[:80],
            "description": str(pval.get("description") or "").strip()[:240],
            "profiles": _sanitize_profile_map(pval.get("profiles")),
        }

    version = str(store.get("version") or "5")
    if version == "defaults":
        version = "5"

    payload = {
        "_note": (
            "Dynamic FormFlow document profiles & portal presets. Editable from the "
            "Dev Mode Profiles page (/profiles) or directly as JSON."
        ),
        "version": version,
        "active_preset": active,
        "presets": clean_presets,
    }

    target = Path(cfg.processing_profile_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return {
        "version": version,
        "active_preset": active,
        "presets": clean_presets,
    }


def activate_preset(cfg: FormFlowConfig, preset_key: str) -> dict[str, Any]:
    store = load_profile_store(cfg)
    key = (preset_key or "").strip().lower()
    if key not in store["presets"]:
        raise ValueError(f"Unknown preset: '{preset_key}'.")
    store["active_preset"] = key
    return save_profile_store(cfg, store)


def create_preset(
    cfg: FormFlowConfig,
    preset_key: str,
    label: str,
    description: str = "",
    clone_from: str | None = None,
) -> dict[str, Any]:
    store = load_profile_store(cfg)
    key = validate_slug(preset_key or slugify_key(label))
    if key in store["presets"]:
        raise ValueError(f"Preset '{key}' already exists.")
    src_key = (clone_from or store["active_preset"]).strip().lower()
    src_profiles = (
        store["presets"][src_key]["profiles"]
        if src_key in store["presets"]
        else _sanitize_profile_map(None)
    )
    store["presets"][key] = {
        "label": (label or key.replace("_", " ").title()).strip()[:80],
        "description": (description or "").strip()[:240],
        "profiles": {k: dict(v) for k, v in src_profiles.items()},
    }
    store["active_preset"] = key
    return save_profile_store(cfg, store)


def delete_preset(cfg: FormFlowConfig, preset_key: str) -> dict[str, Any]:
    store = load_profile_store(cfg)
    key = (preset_key or "").strip().lower()
    if key not in store["presets"]:
        raise ValueError(f"Unknown preset: '{preset_key}'.")
    if len(store["presets"]) <= 1:
        raise ValueError("Cannot delete the last remaining preset.")
    del store["presets"][key]
    if store["active_preset"] == key:
        store["active_preset"] = "sarathi" if "sarathi" in store["presets"] else next(iter(store["presets"]))
    return save_profile_store(cfg, store)


def upsert_document_profile(
    cfg: FormFlowConfig,
    doc_key: str,
    data: dict[str, Any],
    preset_key: str | None = None,
) -> dict[str, Any]:
    store = load_profile_store(cfg)
    pkey = (preset_key or store["active_preset"]).strip().lower()
    if pkey not in store["presets"]:
        raise ValueError(f"Unknown preset: '{pkey}'.")
    key = validate_slug(doc_key or slugify_key(str(data.get("label") or "")))
    existing = dict(store["presets"][pkey]["profiles"].get(key) or {})
    merged = {**existing, **data}
    # Allow clearing optional keyword lists explicitly when passed in edit form
    if "filename_hints" in data:
        merged["filename_hints"] = _clean_keyword_list(data["filename_hints"])
    if "ocr_keywords" in data:
        merged["ocr_keywords"] = _clean_keyword_list(data["ocr_keywords"])
    store["presets"][pkey]["profiles"][key] = sanitize_profile_entry(key, merged)
    return save_profile_store(cfg, store)


def delete_document_profile(
    cfg: FormFlowConfig,
    doc_key: str,
    preset_key: str | None = None,
) -> dict[str, Any]:
    store = load_profile_store(cfg)
    pkey = (preset_key or store["active_preset"]).strip().lower()
    if pkey not in store["presets"]:
        raise ValueError(f"Unknown preset: '{pkey}'.")
    key = (doc_key or "").strip().lower()
    if key in PROTECTED_DOCTYPE_KEYS:
        raise ValueError(f"Built-in fallback profile '{key}' cannot be deleted.")
    profiles_map = store["presets"][pkey]["profiles"]
    if key not in profiles_map:
        raise ValueError(f"Unknown document profile: '{key}'.")
    del profiles_map[key]
    return save_profile_store(cfg, store)


def load_profiles(cfg: FormFlowConfig) -> tuple[dict[str, dict[str, Any]], str]:
    """Return ``(active_profiles, version)``. Never raises for missing/corrupt files."""
    store = load_profile_store(cfg)
    active = store["active_preset"]
    return store["presets"][active]["profiles"], store["version"]


def get_profile(profiles: dict[str, dict[str, Any]], doctype: str) -> tuple[str, dict[str, Any]]:
    """Resolve doctype to ``(profile_name, profile)``. Unknown types use ``'unknown'``."""
    if doctype in profiles:
        return doctype, profiles[doctype]
    return "unknown", profiles["unknown"]


def custom_classifier_rules(profiles: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Extract dynamic filename/OCR keyword hints from profiles for ``classify_document``."""
    rules: dict[str, dict[str, Any]] = {}
    for key, p in profiles.items():
        if key == "unknown" or not isinstance(p, dict):
            continue
        fh = _clean_keyword_list(p.get("filename_hints"))
        ok = _clean_keyword_list(p.get("ocr_keywords"))
        if fh or ok or key not in DOCTYPE_KEYS:
            rules[key] = {
                "filename_hints": fh,
                "ocr_keywords": ok,
                "crop_mode": str(p.get("crop_mode") or "document").lower(),
            }
    return rules
