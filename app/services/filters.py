"""Filename filtering, extension checks, and path safety (Phase 2)."""
from __future__ import annotations

import re
from pathlib import Path

# Temporary / in-progress download artefacts (Windows + browsers + Office + editors).
TEMP_SUFFIXES = {
    ".tmp", ".temp", ".crdownload", ".part", ".download", ".opdownload",
    ".partial", ".bak", ".swp", ".swo", ".lock",
}
TEMP_EXACT = {"thumbs.db", "desktop.ini", ".ds_store"}
TEMP_PREFIXES = ("~$", ".~", "~")


def is_temp_file(name: str) -> bool:
    n = name.strip()
    if not n:
        return True
    low = n.lower()
    if low in TEMP_EXACT:
        return True
    if low.startswith(TEMP_PREFIXES):
        return True
    if low.endswith("~"):
        return True
    suffix = Path(low).suffix
    if suffix in TEMP_SUFFIXES:
        return True
    # Office lock files like ~$report.docx
    if n.startswith("~$"):
        return True
    return False


def is_supported(name: str, permitted: set[str]) -> bool:
    return Path(name.lower()).suffix in {e.lower() for e in permitted}


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str, limit: int = 120) -> str:
    base = Path(name).name.strip().strip(".")
    if not base:
        base = "file"
    base = _SAFE_CHARS.sub("_", base)
    base = re.sub(r"_+", "_", base).strip("._") or "file"
    if len(base) > limit:
        stem, dot, ext = base.rpartition(".")
        if dot and len(ext) <= 5:
            base = stem[: limit - len(ext) - 1] + "." + ext
        else:
            base = base[:limit]
    return base


def resolve_within(path: Path | str, root: Path | str) -> Path:
    """Resolve and require path to be inside root (path traversal guard)."""
    rp = Path(path)
    rr = Path(root)
    try:
        resolved = rp.resolve()
        rroot = rr.resolve()
    except OSError as e:
        raise ValueError(f"cannot resolve path: {e}") from e
    try:
        resolved.relative_to(rroot)
    except ValueError as e:
        raise ValueError(f"path escapes allowed root: {path}") from e
    return resolved
