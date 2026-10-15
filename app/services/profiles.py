"""Load per-document processing profiles (Phase 3).

Profiles live in configurable JSON (default profiles.json). Values are local
configuration, NOT official Sarathi limits. Missing/corrupt files fall back
to safe built-in defaults so the pipeline degrades to needs_attention
instead of crashing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import FormFlowConfig

DOCTYPE_KEYS = ("photo", "signature", "supporting_ll", "unknown")

_DEFAULTS: dict[str, dict[str, Any]] = {
    "photo": {
        "max_bytes": 51200, "output": "JPEG", "max_dim": 1600,
        "min_dim": 300, "min_quality": 55,
        "qualities": [92, 85, 78, 70, 62, 55],
        "scales": [1.0, 0.85, 0.7, 0.55, 0.4],
    },
    "signature": {
        "max_bytes": 51200, "output": "JPEG", "max_dim": 1200,
        "min_dim": 200, "min_quality": 60,
        "qualities": [92, 85, 78, 70, 65, 60],
        "scales": [1.0, 0.85, 0.7, 0.55, 0.4],
    },
    "supporting_ll": {
        "max_bytes": 204800, "output": "JPEG", "max_dim": 2000,
        "min_dim": 400, "min_quality": 55,
        "qualities": [90, 82, 74, 66, 58, 55],
        "scales": [1.0, 0.85, 0.7, 0.55, 0.4],
    },
    "unknown": {"max_bytes": 204800, "passthrough": True},
}


def load_profiles(cfg: FormFlowConfig) -> tuple[dict[str, dict[str, Any]], str]:
    """Return (profiles, version). Never raises for missing/corrupt files."""
    merged = {k: dict(v) for k, v in _DEFAULTS.items()}
    version = "defaults"
    try:
        raw = json.loads(Path(cfg.processing_profile_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return merged, version
    if isinstance(raw, dict):
        version = str(raw.get("version", "1"))
        for key in DOCTYPE_KEYS:
            section = raw.get(key)
            if isinstance(section, dict):
                for field in ("max_bytes", "output", "max_dim", "min_dim",
                              "min_quality", "qualities", "scales", "passthrough",
                              "description"):
                    if field in section and section[field] not in (None, ""):
                        merged[key][field] = section[field]
    # Sanitize numerics so a bad edit cannot cause upscale/overflow loops.
    for key in DOCTYPE_KEYS:
        p = merged[key]
        try:
            p["max_bytes"] = max(1024, int(p.get("max_bytes", 51200)))
        except (TypeError, ValueError):
            p["max_bytes"] = 51200
        for num_key, lo in (("max_dim", 100), ("min_dim", 50), ("min_quality", 10)):
            if num_key in p:
                try:
                    p[num_key] = max(lo, int(p[num_key]))
                except (TypeError, ValueError):
                    pass
        if isinstance(p.get("qualities"), list):
            qs = sorted({q for q in p["qualities"] if isinstance(q, int) and 10 <= q <= 95}, reverse=True)
            if qs:
                p["qualities"] = qs
        if isinstance(p.get("scales"), list):
            ss = sorted({s for s in p["scales"] if isinstance(s, (int, float)) and 0.1 <= s <= 1.0}, reverse=True)
            if ss:
                p["scales"] = ss or [1.0]
    return merged, version


def get_profile(profiles: dict[str, dict[str, Any]], doctype: str) -> tuple[str, dict[str, Any]]:
    """Resolve doctype to (profile_name, profile). Unknown types use 'unknown'."""
    if doctype in profiles:
        return doctype, profiles[doctype]
    return "unknown", profiles["unknown"]
