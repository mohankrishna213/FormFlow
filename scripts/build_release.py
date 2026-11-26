"""Build a self-contained, PII-free FormFlow Windows portable release bundle.

Produces:
  dist/FormFlow-v<version>-win64/       Ready-to-run portable folder
  dist/FormFlow-v<version>-win64.zip    Clean distributable ZIP archive

What is bundled inside:
- Official Python 3.11.9 Windows 64-bit Embeddable runtime (signed by PSF,
  zero Windows Defender / SmartScreen unsigned-exe warnings).
- Lean production packages ONLY (Flask, Waitress, Watchdog, PyMuPDF, Pillow,
  pystray, OpenCV contrib + Haar cascades, RapidOCR + ONNXRuntime CPU).
  Excludes dev bloat (paddlepaddle, pytest, etc.).
- Pre-cached RapidOCR English PP-OCRv5/v4 + PP-OCRv6 detection ONNX models
  so OCR works instantly offline on the target PC from the first file.
- FormFlow app code, profiles.json, ocr_rules.json, Chrome extension/, docs/.
- One-click Setup_FormFlow.bat (creates Desktop shortcuts, registers Start with
  Windows for background watcher + tray icon, launches FormFlow app window),
  FormFlow.vbs (silent launcher), Uninstall_FormFlow.bat, and HOW_TO_INSTALL.txt.

Strictly excludes and audits against any customer PII, DBs, Inbox files,
local secrets, or virtual environments.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from app.version import APP_VERSION, GITHUB_REPO, MANIFEST_ASSET_NAME  # noqa: E402

PYTHON_EMBED_VERSION = "3.11.9"
PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_EMBED_VERSION}/"
    f"python-{PYTHON_EMBED_VERSION}-embed-amd64.zip"
)

# Lean production packages installed into runtime\Lib\site-packages
BUNDLE_PACKAGES = [
    "Flask>=3.0,<4.0",
    "waitress>=3.0",
    "watchdog>=4.0",
    "PyMuPDF>=1.23",
    "Pillow>=10.0",
    "pystray>=0.19.5",
    "zxing-cpp>=2.2",
    "pytesseract>=0.3.10",
    "numpy>=1.26,<2.4",
    "opencv-contrib-python==4.10.0.84",
    "rapidocr==3.10.0",
    "onnxruntime>=1.18",
]

# Pre-cached English OCR ONNX models copied from .venv311 if present (~26 MB)
PRECACHE_ONNX_MODELS = (
    "PP-OCRv6_det_small.onnx",
    "en_PP-OCRv5_rec_mobile.onnx",
    "en_PP-OCRv4_rec_mobile.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
)

# Files/directories strictly forbidden in any release artifact
FORBIDDEN_NAMES = {
    ".api_token",
    ".session_secret",
    "formflow.db",
    "formflow.db-wal",
    "formflow.db-shm",
    "config.local.json",
    ".env",
    ".env.local",
}
FORBIDDEN_DIRS = {"data", ".git", ".venv", ".venv311", "__pycache__", ".pytest_cache", "tests"}

SETUP_BAT = r"""@echo off
setlocal
cd /d "%~dp0"
echo ================================================================
echo   FormFlow - Offline Driving Licence Desk Assistant Setup
echo ================================================================
echo.
if not exist "runtime\python.exe" (
  echo ERROR: runtime\python.exe not found. Please extract the entire ZIP first.
  pause
  exit /b 1
)

echo [1/3] Creating Inbox folder, Desktop shortcuts, and Windows Auto-Start...
"runtime\python.exe" "desktop_app.py" --install
if errorlevel 1 (
  echo ERROR: Setup failed.
  pause
  exit /b 1
)

echo.
echo [2/3] Starting FormFlow background watcher and desktop app...
start "" "runtime\pythonw.exe" "desktop_app.py"

echo.
echo [3/3] Chrome Extension Setup (one-time, takes 20 seconds):
echo   1. Open Chrome and go to:  chrome://extensions
echo   2. Turn ON "Developer mode" (top-right switch)
echo   3. Click "Load unpacked" and select this folder:
echo        %~dp0extension
echo   4. Right-click the FormFlow icon in your bottom-right System Tray
echo      and click "Copy Chrome Extension Token".
echo   5. Click the FormFlow Chrome extension icon -^> Settings -^> Paste token -^> Save.
echo.
echo Done! FormFlow will now start automatically in the background whenever Windows boots.
echo Drop customer documents into "FormFlow Inbox" on your Desktop anytime.
echo.
pause
"""

FORMFLOW_VBS = r'''Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = baseDir & "\runtime\pythonw.exe"
script = baseDir & "\desktop_app.py"
shell.CurrentDirectory = baseDir
shell.Run Chr(34) & pyw & Chr(34) & " " & Chr(34) & script & Chr(34), 0, False
'''

UNINSTALL_BAT = r"""@echo off
setlocal
cd /d "%~dp0"
echo Removing FormFlow Windows Auto-Start and Desktop shortcuts...
"runtime\python.exe" "desktop_app.py" --uninstall
echo.
echo Note: Your Inbox\ and data\ folders were NOT deleted.
echo Quit FormFlow from the bottom-right System Tray before deleting this folder.
pause
"""

UPDATE_BAT = r"""@echo off
setlocal
cd /d "%~dp0"
echo Stopping running FormFlow background watcher for safe update...
taskkill /F /IM pythonw.exe >nul 2>nul
timeout /t 1 /nobreak >nul
"runtime\python.exe" "desktop_app.py" --update
echo Restarting FormFlow background watcher...
start "" "runtime\pythonw.exe" "desktop_app.py" --background
echo.
pause
"""

HOW_TO_INSTALL_TXT = f"""FormFlow v{APP_VERSION} — Quick Install Guide (Windows 10 / 11)
================================================================

No Python installation is required. Everything runs 100% locally on this PC.

STEP 1 — Extract to a permanent folder
  Extract this ZIP to a permanent folder such as:
      C:\\FormFlow
  (Avoid running from inside a temporary ZIP preview or Downloads folder.)

STEP 2 — Double-click Setup_FormFlow.bat
  This automatically:
  - Creates your watched Inbox\\ folder
  - Adds "FormFlow" and "FormFlow Inbox" shortcuts on your Desktop
  - Enables "Start with Windows" so the Inbox watcher and Chrome extension
    service always run silently in the background (System Tray icon at
    bottom-right of taskbar — zero black console windows)
  - Opens the FormFlow desktop app window

STEP 3 — Pair the Chrome Extension (one-time)
  1. Open Google Chrome -> go to chrome://extensions
  2. Enable "Developer mode" (top right)
  3. Click "Load unpacked" and choose the "extension" folder inside C:\\FormFlow
  4. Right-click the FormFlow icon in the Windows System Tray (bottom-right)
     and click "Copy Chrome Extension Token"
  5. Open the FormFlow extension popup -> Settings -> Paste token -> Save

DAILY WORKFLOW
  - Save customer photos, signatures, Aadhaar, 10th memo, PAN, LL, DL, or TR
    files into "FormFlow Inbox" (even raw WhatsApp downloads!).
  - FormFlow automatically crops, compresses (including PDFs for LL & TR),
    drops clean Ready_<type>_<Sno>.* files back into Inbox\\, runs local OCR,
    and prepares fields automatically for 1-click autofill on Sarathi.
  - Closing the FormFlow app window leaves the background watcher running in
    the System Tray. Double-click the Desktop "FormFlow" icon or Tray icon
    anytime to reopen the window.

UNINSTALL / DISABLE AUTO-START
  - Right-click the System Tray icon -> uncheck "Start with Windows" or Quit,
    or run Uninstall_FormFlow.bat (keeps your Inbox\\ and data\\ safe).
"""


def _copy_clean_tree(src: Path, dst: Path) -> None:
    def _ignore(directory: str, entries: list[str]) -> set[str]:
        ignored = set()
        for name in entries:
            if name in ("__pycache__", ".pytest_cache", ".DS_Store", "Thumbs.db"):
                ignored.add(name)
            elif name.endswith((".pyc", ".pyo")):
                ignored.add(name)
        return ignored

    shutil.copytree(src, dst, ignore=_ignore)


def _download_embeddable_python(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / f"python-{PYTHON_EMBED_VERSION}-embed-amd64.zip"
    if zip_path.is_file() and zip_path.stat().st_size > 5_000_000:
        return zip_path
    print(f"Downloading Python {PYTHON_EMBED_VERSION} Windows embeddable package...")
    urllib.request.urlretrieve(PYTHON_EMBED_URL, zip_path)
    return zip_path


def _find_builder_python311(repo_root: Path) -> Path:
    venv_py = repo_root / ".venv311" / "Scripts" / "python.exe"
    if venv_py.is_file():
        return venv_py
    if sys.version_info[:2] == (3, 11):
        return Path(sys.executable)
    local_py = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python311" / "python.exe"
    if local_py.is_file():
        return local_py
    raise RuntimeError("Python 3.11 (.venv311 or active 3.11) is required to assemble the cp311 portable bundle.")


def _find_cached_rapidocr_models(repo_root: Path, py311: Path) -> Path | None:
    candidates = [
        repo_root / ".venv311" / "Lib" / "site-packages" / "rapidocr" / "models",
        py311.parent / "Lib" / "site-packages" / "rapidocr" / "models",
        py311.parent.parent / "Lib" / "site-packages" / "rapidocr" / "models",
    ]
    for cand in candidates:
        if cand.is_dir() and any(cand.glob("*.onnx")):
            return cand
    try:
        res = subprocess.run(
            [str(py311), "-c", "import rapidocr, pathlib; print(pathlib.Path(rapidocr.__file__).parent / 'models')"],
            capture_output=True, text=True, check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            p = Path(res.stdout.strip())
            if p.is_dir() and any(p.glob("*.onnx")):
                return p
    except Exception:
        pass
    return None


def _setup_embedded_runtime(release_dir: Path, cache_dir: Path, repo_root: Path) -> None:
    runtime_dir = release_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    embed_zip = _download_embeddable_python(cache_dir)
    with zipfile.ZipFile(embed_zip, "r") as zf:
        zf.extractall(runtime_dir)

    # Configure python311._pth so embedded runtime resolves app root + site-packages cleanly
    pth_file = runtime_dir / "python311._pth"
    pth_file.write_text(
        "python311.zip\n.\n..\nLib\\site-packages\nimport site\n",
        encoding="utf-8",
    )

    # Install lean production wheels directly into runtime/Lib/site-packages
    site_packages = runtime_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    py311 = _find_builder_python311(repo_root)
    wheel_cache = cache_dir / "wheels_cp311"
    wheel_cache.mkdir(parents=True, exist_ok=True)

    print("Installing lean production dependencies into bundled runtime...")
    cmd = [
        str(py311),
        "-m",
        "pip",
        "install",
        "--target",
        str(site_packages),
        "--cache-dir",
        str(wheel_cache),
        "--upgrade",
        "--no-warn-script-location",
        *BUNDLE_PACKAGES,
    ]
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"pip install into portable runtime failed with exit code {res.returncode}")

    # Copy pre-cached RapidOCR ONNX models if present so first OCR run is instant offline
    src_models = _find_cached_rapidocr_models(repo_root, py311)
    dst_models = site_packages / "rapidocr" / "models"
    if src_models is not None and dst_models.is_dir():
        copied = 0
        for name in PRECACHE_ONNX_MODELS:
            s = src_models / name
            d = dst_models / name
            if s.is_file() and not d.is_file():
                shutil.copy2(s, d)
                copied += 1
        print(f"Pre-bundled {copied} RapidOCR ONNX model files.")

    # Copy msvcp140.dll if available alongside system Python 3.11 / Windows for ONNXRuntime portability
    for dll_name in ("msvcp140.dll", "msvcp140_1.dll"):
        if (runtime_dir / dll_name).is_file():
            continue
        for src_dir in (
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32",
            py311.parent,
        ):
            cand = src_dir / dll_name
            if cand.is_file():
                shutil.copy2(cand, runtime_dir / dll_name)
                break

    # Remove bytecode caches inside site-packages to keep zip clean & smaller
    for pycache in site_packages.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    # Remove any test directories shipped inside third-party packages only if huge? Keep safe, skip deleting package code.


def verify_release_dir(release_dir: Path) -> list[str]:
    """Audit the release directory to guarantee ZERO customer PII, DBs, or secrets leaked."""
    issues: list[str] = []
    inbox_dir = release_dir / "Inbox"
    if not inbox_dir.is_dir():
        issues.append("Inbox/ directory missing in release")
    elif any(inbox_dir.iterdir()):
        issues.append("Inbox/ must be completely empty in release")

    for p in release_dir.rglob("*"):
        rel = p.relative_to(release_dir)
        top = rel.parts[0] if rel.parts else ""
        if top == "runtime":
            # Only check forbidden secret/DB files inside runtime
            if p.name.lower() in FORBIDDEN_NAMES or p.suffix.lower() == ".db":
                issues.append(f"forbidden file inside runtime: {rel}")
            continue
        if p.is_dir() and p.name in FORBIDDEN_DIRS:
            issues.append(f"forbidden directory in release: {rel}")
        if p.is_file():
            if p.name.lower() in FORBIDDEN_NAMES or p.suffix.lower() in (".db", ".log", ".pyc"):
                issues.append(f"forbidden file in release: {rel}")

    for required in (
        "desktop_app.py",
        "run.py",
        "profiles.json",
        "ocr_rules.json",
        "Setup_FormFlow.bat",
        "FormFlow.vbs",
        "Uninstall_FormFlow.bat",
        "HOW_TO_INSTALL.txt",
        "app/__init__.py",
        "app/static/favicon.ico",
        "app/models/face_detection_yunet_2023mar.onnx",
        "extension/manifest.json",
    ):
        if not (release_dir / required).is_file():
            issues.append(f"missing required release file: {required}")
    return issues


def smoke_test_bundled_runtime(release_dir: Path) -> None:
    """Verify the self-contained runtime boots FormFlow cleanly in an isolated environment."""
    py = release_dir / "runtime" / "python.exe"
    if not py.is_file():
        raise RuntimeError(f"bundled python missing: {py}")

    import tempfile

    with tempfile.TemporaryDirectory(prefix="formflow_smoke_") as td:
        env = {
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            "TEMP": td,
            "TMP": td,
            "PATH": str(release_dir / "runtime"),
            "PYTHONNOUSERSITE": "1",
            "FORMFLOW_DATA_DIR": str(Path(td) / "data"),
            "FORMFLOW_INBOX_DIR": str(Path(td) / "Inbox"),
            "FORMFLOW_DB": str(Path(td) / "data" / "formflow.db"),
        }
        code = (
            "import flask, waitress, watchdog, pymupdf, PIL, pystray, cv2, rapidocr, onnxruntime, zxingcpp; "
            "import desktop_app; from app import create_app; from app.services import ocr_paddle, document_cv, qr_decode; "
            "app = create_app(); cfg = app.config['FORMFLOW']; "
            "assert cfg.allow_reset is False, 'allow_reset must be False in prod'; "
            "assert cfg.ui_mode == 'production', 'ui_mode must default to production'; "
            "assert ocr_paddle.available(), 'RapidOCR must be available'; "
            "assert document_cv._YUNET_MODEL_PATH.is_file(), 'YuNet ONNX model must be bundled'; "
            "assert document_cv._find_haar_face_xml(cv2), 'Haar face XML must be bundled'; "
            "r = app.test_client().get('/healthz'); assert r.status_code == 200; "
            "print('SMOKE TEST OK: RapidOCR + YuNet + zxing-cpp QR + OpenCV Haar + Waitress + pystray + Flask verified')"
        )
        res = subprocess.run(
            [str(py), "-c", code],
            cwd=str(release_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Bundled runtime smoke test failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
            )
        print(res.stdout.strip())


def build_release(
    *,
    repo_root: Path = REPO_ROOT,
    out_dir: Path | None = None,
    version: str = APP_VERSION,
    include_runtime: bool = True,
    make_zip: bool = True,
) -> tuple[Path, Path | None]:
    dist_dir = out_dir or (repo_root / "dist")
    cache_dir = repo_root / "build_cache"
    release_name = f"FormFlow-v{version}-win64"
    release_dir = dist_dir / release_name
    zip_path = dist_dir / f"{release_name}.zip"

    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy clean allowlisted application files
    for folder in ("app", "extension", "docs"):
        _copy_clean_tree(repo_root / folder, release_dir / folder)
    for fname in (
        "desktop_app.py",
        "run.py",
        "profiles.json",
        "ocr_rules.json",
        "requirements.txt",
        "README.md",
    ):
        shutil.copy2(repo_root / fname, release_dir / fname)

    # 2. Empty Inbox/ folder + root Windows setup/launcher files
    (release_dir / "Inbox").mkdir(parents=True, exist_ok=True)
    (release_dir / "Setup_FormFlow.bat").write_text(SETUP_BAT, encoding="utf-8", newline="\r\n")
    (release_dir / "FormFlow.vbs").write_text(FORMFLOW_VBS, encoding="utf-8", newline="\r\n")
    (release_dir / "Uninstall_FormFlow.bat").write_text(UNINSTALL_BAT, encoding="utf-8", newline="\r\n")
    (release_dir / "Update_FormFlow.bat").write_text(UPDATE_BAT, encoding="utf-8", newline="\r\n")
    (release_dir / "HOW_TO_INSTALL.txt").write_text(HOW_TO_INSTALL_TXT, encoding="utf-8", newline="\r\n")

    # 3. Embed official Python 3.11 runtime + lean production deps + ONNX models
    if include_runtime:
        _setup_embedded_runtime(release_dir, cache_dir, repo_root)

    # 4. Strict PII & hygiene audit
    issues = verify_release_dir(release_dir)
    if issues:
        raise RuntimeError("Release audit failed:\n  - " + "\n  - ".join(issues))
    print("Release PII & hygiene audit: PASSED (0 customer files, 0 secrets, 0 DBs).")

    # 5. Isolated runtime smoke test
    if include_runtime:
        smoke_test_bundled_runtime(release_dir)

    # 6. Create distributable ZIP archive
    created_zip: Path | None = None
    if make_zip:
        if zip_path.exists():
            zip_path.unlink()
        print(f"Creating archive {zip_path}...")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            # Ensure empty Inbox/ directory is preserved inside the ZIP
            zf.writestr(f"{release_name}/Inbox/", "")
            for file_path in sorted(release_dir.rglob("*")):
                if file_path.is_file():
                    arcname = f"{release_name}/{file_path.relative_to(release_dir).as_posix()}"
                    zf.write(file_path, arcname)
        h = hashlib.sha256()
        with open(zip_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        size_mb = zip_path.stat().st_size / (1024 * 1024)
        print(f"Release ZIP ready: {zip_path} ({size_mb:.1f} MB)")
        print(f"SHA-256: {h.hexdigest()}")
        created_zip = zip_path

    return release_dir, created_zip


def _sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_patch_and_manifest(
    release_dir: Path,
    dist_dir: Path,
    *,
    version: str = APP_VERSION,
    repo: str = GITHUB_REPO,
    full_zip: Path | None = None,
) -> tuple[Path, Path]:
    """Create the lightweight (~300 KB) delta patch ZIP + update-manifest.json.

    Excludes ``runtime/``, ``Inbox/``, ``data/``, ``profiles.json``, and local configs
    so in-place updates never clobber customer data or user-customized profiles.
    """
    patch_name = f"FormFlow-v{version}-patch.zip"
    patch_zip = dist_dir / patch_name
    if patch_zip.exists():
        patch_zip.unlink()

    patch_folders = ("app", "extension", "docs")
    patch_files = (
        "desktop_app.py",
        "run.py",
        "ocr_rules.json",
        "requirements.txt",
        "README.md",
        "Setup_FormFlow.bat",
        "FormFlow.vbs",
        "Uninstall_FormFlow.bat",
        "Update_FormFlow.bat",
        "HOW_TO_INSTALL.txt",
    )
    with zipfile.ZipFile(patch_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for folder in patch_folders:
            base = release_dir / folder
            if base.is_dir():
                for fp in sorted(base.rglob("*")):
                    if fp.is_file():
                        zf.write(fp, fp.relative_to(release_dir).as_posix())
        for fname in patch_files:
            fp = release_dir / fname
            if fp.is_file():
                zf.write(fp, fname)

    patch_sha = _sha256_path(patch_zip)
    patch_bytes = patch_zip.stat().st_size
    tag = f"v{version.lstrip('vV')}"
    base_dl = f"https://github.com/{repo.strip('/')}/releases/download/{tag}"

    full_name = full_zip.name if (full_zip and full_zip.is_file()) else f"FormFlow-v{version}-win64.zip"
    full_sha = _sha256_path(full_zip) if (full_zip and full_zip.is_file()) else ""
    manifest = {
        "version": version.lstrip("vV"),
        "released_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": repo,
        "patch_filename": patch_name,
        "patch_url": f"{base_dl}/{patch_name}",
        "patch_sha256": patch_sha,
        "patch_bytes": patch_bytes,
        "full_filename": full_name,
        "full_url": f"{base_dl}/{full_name}",
        "full_sha256": full_sha,
        "pip_packages": list(BUNDLE_PACKAGES),
    }
    manifest_path = dist_dir / MANIFEST_ASSET_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Delta patch ZIP ready: {patch_zip} ({patch_bytes / 1024:.1f} KB, sha256={patch_sha[:12]}...)")
    print(f"Update manifest ready: {manifest_path}")
    return patch_zip, manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build FormFlow self-contained Windows release ZIP + delta patch")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--repo", default=GITHUB_REPO)
    parser.add_argument("--no-runtime", action="store_true", help="Skip embedding Python runtime (fast dry-run)")
    parser.add_argument("--no-zip", action="store_true", help="Build folder only without full zipping")
    parser.add_argument("--patch-only", action="store_true", help="Build lightweight delta patch + update-manifest.json only")
    args = parser.parse_args(argv)
    release_dir, full_zip = build_release(
        version=args.version,
        include_runtime=(not args.no_runtime) and (not args.patch_only),
        make_zip=(not args.no_zip) and (not args.patch_only),
    )
    build_patch_and_manifest(
        release_dir,
        release_dir.parent,
        version=args.version,
        repo=args.repo,
        full_zip=full_zip,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
