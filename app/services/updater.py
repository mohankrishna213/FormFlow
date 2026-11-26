"""Lightweight GitHub Releases delta auto-updater with SHA-256 verification and rollback.

Privacy & safety invariants:
- Strictly read-only HTTPS GET to GitHub Releases; zero customer PII, tokens, or
  telemetry are ever transmitted.
- Verifies SHA-256 checksum of every downloaded patch archive before touching disk.
- Refuses path traversal (``..``) and NEVER overwrites user state or runtime files
  (``data/``, ``Inbox/``, ``config.local.json``, ``profiles.json``, ``runtime/``).
- Backs up every overwritten file before extraction and automatically rolls back if
  isolated boot verification fails.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

from ..version import APP_VERSION, GITHUB_REPO, MANIFEST_ASSET_NAME

# Paths that a delta patch MUST NEVER overwrite (preserves customer data & custom profiles)
PROTECTED_TOP_DIRS = frozenset({"data", "inbox", "runtime", ".venv", ".venv311", ".git"})
PROTECTED_FILES = frozenset({
    "config.local.json",
    "profiles.json",
    ".env",
    ".env.local",
})


class UpdateError(Exception):
    """Raised when an update check, download, checksum, or patch application fails."""


@dataclass(frozen=True)
class UpdateInfo:
    available: bool
    current_version: str
    latest_version: str
    patch_url: str = ""
    patch_sha256: str = ""
    patch_bytes: int = 0
    full_url: str = ""
    notes: str = ""
    pip_packages: tuple[str, ...] = ()
    error: str | None = None


def parse_version(raw: str) -> tuple[int, ...]:
    """Parse semver-like strings ('v1.2.1', '1.2.1') into an integer tuple."""
    cleaned = str(raw or "").strip().lstrip("vV")
    nums = [int(p) for p in re.findall(r"\d+", cleaned)]
    return tuple(nums) if nums else (0,)


def is_newer_version(latest: str, current: str = APP_VERSION) -> bool:
    a = parse_version(latest)
    b = parse_version(current)
    max_len = max(len(a), len(b))
    a_pad = a + (0,) * (max_len - len(a))
    b_pad = b + (0,) * (max_len - len(b))
    return a_pad > b_pad


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_url_for(repo: str = GITHUB_REPO) -> str:
    return f"https://github.com/{repo.strip('/')}/releases/latest/download/{MANIFEST_ASSET_NAME}"


def check_for_update(
    *,
    repo: str = GITHUB_REPO,
    current_version: str = APP_VERSION,
    manifest_url: str | None = None,
    timeout: float = 8.0,
) -> UpdateInfo:
    """Query GitHub Releases ``update-manifest.json`` (read-only HTTPS, zero PII)."""
    url = manifest_url or manifest_url_for(repo)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"FormFlow-Updater/{current_version}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(256 * 1024).decode("utf-8-sig")
        data = json.loads(raw)
        if not isinstance(data, dict) or not data.get("version"):
            return UpdateInfo(
                available=False,
                current_version=current_version,
                latest_version=current_version,
                error="Invalid update manifest format.",
            )
    except urllib.error.HTTPError as e:
        msg = "No published release found yet." if e.code == 404 else f"HTTP {e.code} checking updates."
        return UpdateInfo(
            available=False, current_version=current_version, latest_version=current_version, error=msg
        )
    except Exception as e:
        return UpdateInfo(
            available=False,
            current_version=current_version,
            latest_version=current_version,
            error=f"Offline or unreachable ({type(e).__name__}).",
        )

    latest = str(data.get("version", current_version)).strip().lstrip("vV")
    patch_url = str(data.get("patch_url") or "").strip()
    patch_sha = str(data.get("patch_sha256") or "").strip().lower()
    avail = is_newer_version(latest, current_version) and bool(patch_url and patch_sha)
    pkgs = tuple(str(p).strip() for p in (data.get("pip_packages") or ()) if str(p).strip())
    return UpdateInfo(
        available=avail,
        current_version=current_version,
        latest_version=latest,
        patch_url=patch_url,
        patch_sha256=patch_sha,
        patch_bytes=int(data.get("patch_bytes") or 0),
        full_url=str(data.get("full_url") or "").strip(),
        notes=str(data.get("notes") or "").strip(),
        pip_packages=pkgs,
        error=None,
    )


def download_patch(
    info: UpdateInfo,
    dest_dir: Path,
    *,
    timeout: float = 30.0,
) -> Path:
    """Download the delta patch ZIP and verify its SHA-256 digest before returning."""
    if not info.patch_url or not info.patch_sha256:
        raise UpdateError("Update manifest is missing patch_url or patch_sha256.")
    if not info.patch_url.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
        raise UpdateError("Refusing insecure non-HTTPS update URL.")

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_ver = re.sub(r"[^A-Za-z0-9._\-]", "_", info.latest_version) or "latest"
    out_zip = dest_dir / f"FormFlow-v{safe_ver}-patch.zip"
    tmp_zip = out_zip.with_suffix(".zip.part")

    req = urllib.request.Request(
        info.patch_url,
        headers={"User-Agent": f"FormFlow-Updater/{info.current_version}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp_zip, "wb") as f:
            shutil.copyfileobj(resp, f)
    except Exception as e:
        if tmp_zip.exists():
            tmp_zip.unlink(missing_ok=True)
        raise UpdateError(f"Failed to download update patch: {e}") from e

    actual_sha = sha256_file(tmp_zip).lower()
    if actual_sha != info.patch_sha256.lower():
        tmp_zip.unlink(missing_ok=True)
        raise UpdateError(
            f"Patch SHA-256 mismatch (expected {info.patch_sha256[:12]}..., got {actual_sha[:12]}...)."
        )
    os.replace(tmp_zip, out_zip)
    return out_zip


def _validate_patch_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, Path]]:
    """Validate every archive entry against path traversal and protected user-data rules."""
    validated: list[tuple[zipfile.ZipInfo, Path]] = []
    for zi in zf.infolist():
        if zi.is_dir():
            continue
        raw = zi.filename.replace("\\", "/")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            raise UpdateError(f"Refusing absolute path inside patch: {zi.filename}")
        parts = [p for p in raw.split("/") if p and p != "."]
        if not parts or any(p == ".." for p in parts):
            raise UpdateError(f"Refusing unsafe path traversal inside patch: {zi.filename}")
        top = parts[0].lower()
        if top in PROTECTED_TOP_DIRS:
            raise UpdateError(f"Refusing patch targeting protected directory '{parts[0]}'")
        if len(parts) == 1 and top in PROTECTED_FILES:
            raise UpdateError(f"Refusing patch targeting protected user config '{parts[0]}'")
        if any(p.lower().endswith((".db", ".api_token", ".session_secret")) for p in parts):
            raise UpdateError(f"Refusing forbidden state file in patch: {zi.filename}")
        validated.append((zi, Path(*parts)))
    if not validated:
        raise UpdateError("Patch archive is empty.")
    return validated


def verify_app_boot(app_root: Path, python_exe: Path | None = None) -> tuple[bool, str]:
    """Isolated boot smoke check: confirms FormFlow creates app and serves /healthz cleanly."""
    py = python_exe
    if py is None:
        cand = app_root / "runtime" / "python.exe"
        py = cand if cand.is_file() else Path(sys.executable)

    with tempfile.TemporaryDirectory(prefix="formflow_upd_check_") as td:
        env = dict(os.environ)
        env.update({
            "PYTHONNOUSERSITE": "1",
            "FORMFLOW_DATA_DIR": str(Path(td) / "data"),
            "FORMFLOW_INBOX_DIR": str(Path(td) / "Inbox"),
            "FORMFLOW_DB": str(Path(td) / "data" / "formflow.db"),
        })
        code = (
            "from app import create_app; "
            "a = create_app(); "
            "r = a.test_client().get('/healthz'); "
            "assert r.status_code == 200, f'healthz={r.status_code}'; "
            "print('BOOT_OK')"
        )
        res = subprocess.run(
            [str(py), "-c", code],
            cwd=str(app_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if res.returncode == 0 and "BOOT_OK" in res.stdout:
            return True, "ok"
        detail = (res.stderr or res.stdout or f"exit {res.returncode}").strip()
        return False, detail[:400]


def apply_patch_update(
    app_root: Path,
    patch_zip: Path,
    expected_sha256: str,
    *,
    data_dir: Path | None = None,
    verify_boot: bool = True,
    python_exe: Path | None = None,
) -> dict[str, object]:
    """Verify, back up, apply a delta patch ZIP, and automatically roll back on boot failure."""
    app_root = Path(app_root).resolve()
    patch_zip = Path(patch_zip).resolve()
    if not patch_zip.is_file():
        raise UpdateError(f"Patch file not found: {patch_zip}")

    actual_sha = sha256_file(patch_zip).lower()
    if expected_sha256 and actual_sha != expected_sha256.strip().lower():
        raise UpdateError("Patch SHA-256 verification failed before apply.")

    state_dir = Path(data_dir) if data_dir is not None else (app_root / "data")
    backup_dir = state_dir / ".update_backup" / f"prev_{APP_VERSION}"
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    backed_up: list[Path] = []
    created_new: list[Path] = []

    def _rollback() -> None:
        for rel in created_new:
            target = app_root / rel
            if target.is_file():
                try:
                    target.unlink()
                except OSError:
                    pass
        for rel in backed_up:
            src_bak = backup_dir / rel
            dst = app_root / rel
            if src_bak.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_bak, dst)

    try:
        with zipfile.ZipFile(patch_zip, "r") as zf:
            members = _validate_patch_members(zf)
            for zi, rel in members:
                target = (app_root / rel).resolve()
                # Defense in depth: ensure resolved target stays inside app_root
                target.relative_to(app_root)
                if target.is_file():
                    bak_path = backup_dir / rel
                    bak_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, bak_path)
                    backed_up.append(rel)
                else:
                    created_new.append(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(zi, "r") as src_f, open(target, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
    except Exception as e:
        _rollback()
        if isinstance(e, UpdateError):
            raise
        raise UpdateError(f"Failed applying patch (rolled back cleanly): {e}") from e

    if verify_boot:
        ok, err = verify_app_boot(app_root, python_exe=python_exe)
        if not ok:
            _rollback()
            raise UpdateError(f"Updated app failed startup check and was rolled back: {err}")

    return {
        "updated_files": len(backed_up) + len(created_new),
        "backed_up_files": len(backed_up),
        "new_files": len(created_new),
        "backup_dir": str(backup_dir),
    }
