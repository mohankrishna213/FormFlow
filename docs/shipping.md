# Production Shipping & Windows Desktop Setup

FormFlow ships as a **self-contained Windows desktop application** with an always-on background Inbox watcher, Windows System Tray icon, standalone native app window (no browser URL bar or tabs), and zero Python installation required on the target PC.

## Building a Clean Release Bundle

On your development machine (with `.venv311` active), run:

```bat
.venv311\Scripts\python.exe scripts\build_release.py
```

This builds `dist\FormFlow-v1.2.1-win64.zip` containing:
- Official signed **Python 3.11.9 Windows 64-bit embeddable runtime** (`runtime\`) with lean production packages only (`Flask`, `waitress`, `watchdog`, `PyMuPDF`, `Pillow`, `pystray`, `zxing-cpp`, `opencv-contrib-python`, `rapidocr`, `onnxruntime`).
- Pre-cached **RapidOCR English PP-OCRv5/v4 + PP-OCRv6 detection ONNX models** and the **YuNet ONNX face detector** (`app\models\face_detection_yunet_2023mar.onnx`) so OCR, passphoto framing, and offline Aadhaar/PAN QR decoding work immediately offline on the target PC.
- **PII & Hygiene Audit**: automatically fails the build if any `data/`, `.db`, `.api_token`, `.session_secret`, or files in `Inbox/` exist in the release folder.
- **Isolated Runtime Smoke Test**: launches the bundled `runtime\python.exe` in a clean environment to verify Flask, Waitress, RapidOCR, YuNet, `zxing-cpp` QR, OpenCV Haar cascades, and `/healthz` before creating the ZIP.

## Installing on a New Windows 10/11 PC

1. Copy `FormFlow-v1.2.1-win64.zip` to the target PC and extract it to a permanent folder such as `C:\FormFlow`.
2. Double-click **`Setup_FormFlow.bat`**:
   - Creates the watched `Inbox\` folder inside `C:\FormFlow`.
   - Places **`FormFlow`** (app icon) and **`FormFlow Inbox`** shortcuts on the Desktop.
   - Registers per-user **Start with Windows** (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run\FormFlow` — zero Admin rights needed) so the background Inbox watcher and local `127.0.0.1:5000` Waitress server start silently on every reboot.
   - Opens the standalone **FormFlow** desktop app window.
3. Pair the Chrome extension (one-time):
   - Open Chrome → `chrome://extensions` → enable **Developer mode** → click **Load unpacked** → select `C:\FormFlow\extension`.
   - Right-click the **FormFlow** icon in the bottom-right Windows System Tray → click **Copy Chrome Extension Token**.
   - Click the FormFlow extension icon in Chrome → **Settings** → paste the token → **Save**.

## Background Watcher & System Tray Controls

Closing the FormFlow app window leaves the background watcher and Chrome Extension service running quietly in the Windows System Tray (bottom-right taskbar):
- **Double-click tray icon** (or click **Open FormFlow**): reopens the native desktop app window.
- **Open Inbox Folder**: opens `Inbox\` directly in Explorer.
- **Copy Chrome Extension Token**: copies `data\.api_token` directly to the clipboard without displaying or logging it.
- **Start with Windows**: toggle auto-start on/off anytime.
- **Quit FormFlow**: cleanly stops the watcher, worker, and local server.

## Automatic CI/CD Releases (GitHub Actions)

FormFlow includes two GitHub Actions workflows under `.github/workflows/`:
- **[`ci.yml`](../.github/workflows/ci.yml)**: Runs the full Python pytest suite, Chrome extension tests, and a dry-run PII/patch audit on every push and PR to `main`.
- **[`release.yml`](../.github/workflows/release.yml)**: Triggers automatically whenever you push a version tag (`v*.*.*`) or manually run it from the **Actions** tab:
  ```bat
  git tag v1.2.2
  git push origin v1.2.2
  ```
  It verifies all tests, pre-warms RapidOCR ONNX models, builds & smoke-tests the release on `windows-latest`, and publishes **three assets** to GitHub Releases:
  1. `FormFlow-v<ver>-win64.zip` (~218 MB) — one-time self-contained install for new PCs.
  2. `FormFlow-v<ver>-patch.zip` (~300 KB) — lightweight code-only delta patch for existing installations.
  3. `update-manifest.json` — version metadata and SHA-256 checksums.

## One-Click Delta Updates (No 218 MB Re-Download)

Once installed, existing Windows machines update in **~3 seconds** without re-downloading the Python/OCR runtime:
- **From the System Tray**: Right-click the FormFlow tray icon → **Check for Updates** (FormFlow also checks silently on startup and notifies you if a newer release is published).
- **Or double-click `Update_FormFlow.bat`** inside the FormFlow folder.
- **Safety Guarantees**:
  - Strictly read-only HTTPS download from GitHub Releases (zero customer data or tokens ever leave the PC).
  - Verifies the patch **SHA-256 checksum** before modifying any file.
  - Never overwrites `data\`, `Inbox\`, `config.local.json`, `profiles.json`, or `runtime\`.
  - Backs up overwritten files and **automatically rolls back** if the updated app fails an isolated `/healthz` boot check.

## Uninstalling Auto-Start

Run `Uninstall_FormFlow.bat` (or right-click the tray icon → uncheck **Start with Windows** and click **Quit FormFlow**). Customer `Inbox\` and `data\` folders are never deleted automatically.
