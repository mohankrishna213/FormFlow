# FormFlow

Local-first document intake, auto-cropping, OCR extraction, and 1-click Chrome extension autofill for Indian driving-licence (Sarathi) applications on Windows.

Everything runs offline on `127.0.0.1:5000` with SQLite — no cloud OCR, no remote DB, and no external uploads.

## How It Works

1. **Drop customer files into `Inbox\`** — photos, signatures, Aadhaar, 10th SSC memo, PAN, LL, DL, or TR images/PDFs (including unlabeled WhatsApp downloads like `WhatsApp Image ....jpeg`).
2. **Instant auto-crop, resize, PDF processing & export** — FormFlow detects each document type, strips phone screenshot bars / backgrounds, compresses images or PDFs to exact portal limits, and drops clean files right back into `Inbox\` as `Ready_<type>_<Sno>.<ext>`:
   - `Ready_photo_1.jpg`, `Ready_signature_1.jpg` → `< 50 KB` (JPG only)
   - `Ready_aadhaar_1.jpg` → `< 500 KB` (JPG only; e-Aadhaar PDFs auto-convert to JPG)
   - `Ready_10th_memo_1.jpg`, `Ready_pan_1.jpg`, `Ready_dl_1.jpg` → `< 200 KB` (JPG only)
   - `Ready_ll_1.jpg / .pdf`, `Ready_tr_1.jpg / .pdf` → `< 200 KB` (**JPG or PDF**, with adaptive multi-page PDF compression; configurable per document for any future doc type in **Dev Mode → Profiles**)
3. **Local OCR & automatic customer grouping** — Files dropped together for one customer group into a single case. RapidOCR (local PaddleOCR PP-OCRv5/v6 on CPU, with Tesseract automatic fallback) reads native-resolution images and normalizes Indian `DD/MM/YYYY` dates to ISO `YYYY-MM-DD`.
4. **Auto-Ready Fill & 1-click Sarathi Autofill** — High-confidence fields (`>= 0.70`, automatically selecting the highest-confidence value when documents disagree) go straight to **Ready to Fill** and the **Chrome extension** (`https://sarathi.parivahan.gov.in/*`) for 1-click text autofill and 1-click photo/signature/document upload. **Review & Validate** and **Manual Crop** remain available whenever you want to adjust anything.

## Quick Start (Windows)

1. Install **Python 3.11** (recommended for fast RapidOCR + OpenCV Haar face detection) and optionally **Tesseract-OCR** (automatic fallback).
2. Double-click `run.bat` (or run from PowerShell):
   ```bat
   py -3.11 -m venv .venv311
   .venv311\Scripts\activate
   pip install -r requirements.txt
   python run.py
   ```
3. Open `http://127.0.0.1:5000` (starts in the clean **Desk** production UI with live auto-refresh and inline 1-click Copy; click **Dev Mode** in the top bar anytime for Queue, raw OCR provenance, and Events).
4. Load `extension/` as an unpacked Chrome extension (`chrome://extensions` → Developer mode → Load unpacked) and paste the local API token from `data/.api_token` into the extension settings.

## Guides

- [Production Shipping & Windows Desktop Setup](docs/shipping.md) — building the self-contained Windows release ZIP, System Tray background watcher, Auto-Start, and Chrome extension pairing
- [Architecture & Pipeline](docs/architecture.md) — end-to-end flow, SQLite schema, and module map
- [Configuration & Profiles](docs/configuration.md) — environment variables, `profiles.json`, `ocr_rules.json`, and Auto-Ready vs strict review mode
- [Chrome Extension & Local API](docs/extension_and_api.md) — extension setup, Sarathi autofill + file upload, API endpoints, and security model

## Running Tests

```bat
.venv311\Scripts\python.exe -m pytest -q
node tests/test_extension_api_client.js
node tests/test_portal_adapter.js
```

## Security & Privacy

- Binds strictly to `127.0.0.1`; all `/api/*` endpoints require Bearer token authentication (`data/.api_token`) and strict origin validation.
- Staged originals (`data/staging/`) are never modified; OCR sidecars (`data/ocr/`) and SQLite (`data/formflow.db`) stay inside gitignored `data/`.
- The Chrome extension never solves CAPTCHAs, never clicks Submit/Payment, and never logs applicant values or tokens.
