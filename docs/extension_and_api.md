# Chrome Extension & Local API

The FormFlow Chrome extension (`extension/`, Manifest V3) connects to the local Flask server (`http://127.0.0.1:5000`) to autofill forms and attach prepared documents on **Sarathi** (`https://sarathi.parivahan.gov.in/*`).

## Setup

1. Start FormFlow locally (`run.bat` or `python run.py`).
2. Copy the generated Bearer token from `data/.api_token` (256-bit hex, created automatically on first run).
3. In Chrome, open `chrome://extensions`, enable **Developer mode**, and click **Load unpacked** → select `C:\FormFlow\extension`.
4. Click the FormFlow toolbar icon → **Settings** → paste the token and click **Save & Test**.

## Using the Extension on Sarathi

1. Navigate to a Sarathi form (e.g. New Learner's Licence, Driving Licence Entry, Application Status, or Upload Documents).
2. Open the FormFlow popup and select the customer case (auto-ready cases show a green **Auto-Ready** badge; reviewed cases show **Reviewed**).
3. Click **Autofill** to fill all matched visible fields (with automatic `YYYY-MM-DD` → `DD-MM-YYYY` date formatting, name/address splitting, and read-back verification).
4. On document upload pages, click **Attach** next to any prepared file (`Passphoto`, `Signature`, `Aadhaar`, `10th Memo`, `PAN`, `LL`, `DL`, `TR`) to populate the matching `<input type="file">` control via `DataTransfer`.
5. Complete CAPTCHA, OTP, declarations, preview, and Submit manually.

## Local API Reference

All endpoints require `Authorization: Bearer <token>` (constant-time validation via `hmac.compare_digest`).

| Endpoint | Purpose |
|---|---|
| `GET /api/cases` | Minimal list of recent cases (`case_id`, `status`, `reviewed`, `auto_ready`, `review_version`, `reviewed_at` — zero applicant PII) |
| `GET /api/cases/<case_id>/fill-packet?workflow=auto` | Fill packet for `auto` (default in UI), `dl_application`, or `app_status`. Returns eligible values and marks missing/low-confidence fields unavailable without fabricating data |
| `GET /api/cases/<case_id>/documents` | Upload-ready prepared documents for the case (plus recent unassigned `photo`/`signature` assets) with `Inbox\Ready_*` filenames and byte sizes |
| `GET /api/cases/<case_id>/documents/<doc_id>/prepared` | Streams prepared `.jpg`/`.pdf` bytes with strict case-isolation (`403` for documents belonging to another case) and `Cache-Control: no-store` |
| `POST /api/cases/<case_id>/events` | Records minimal audit events (`copy_attempt`, `fill_attempt`, `restore_attempt`, `autofill_attempt`, `file_attach_attempt`). Enforces strict JSON schema, positive integer `review_version` (`409 stale_review_version` on mismatch), and supported `field_key` values |

## Security & Safety Invariants

- **Localhost only**: Flask binds to `127.0.0.1`. External web origins, unexpected localhost ports, `Origin: null`, and malformed origins are rejected (`403 origin_not_allowed`). Only the configured localhost origin, valid `chrome-extension://<32-char-id>` origins, and non-browser CLI requests (no `Origin` header) are permitted.
- **No silent automation**: The content script runs strictly on `https://sarathi.parivahan.gov.in/*`, never touches CAPTCHAs (`#entcaptxt`, captcha inputs), submit/cancel/payment buttons, or hidden/disabled/read-only fields.
- **No PII leakage**: Error payloads, `/api/cases`, `case_events`, and browser console logs never include applicant values or Bearer tokens.
