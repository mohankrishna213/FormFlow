# Configuration & Profiles

All settings are loaded by `app/config.py::load_config()`. You can override any key via environment variables or an optional gitignored `config.local.json` in the repo root.

## Configuration Keys

| Key | Environment Variable | Default | Notes |
|---|---|---|---|
| `inbox_dir` | `FORMFLOW_INBOX_DIR` | `<repo>/Inbox` | Watched folder (non-recursive) |
| `data_dir` | `FORMFLOW_DATA_DIR` | `<repo>/data` | SQLite DB, staging, prepared, OCR sidecars |
| `backup_dir` | `FORMFLOW_BACKUP_DIR` | `<repo>/data/backups` | Local backup folder |
| `db_path` | `FORMFLOW_DB` | `<repo>/data/formflow.db` | SQLite database file |
| `port` | `FORMFLOW_PORT` | `5000` | Always bound to `127.0.0.1` |
| `secret_key` | `FORMFLOW_SECRET_KEY` | Auto-generated in `data/.session_secret` | Flask CSRF/session secret |
| `ocr_backend` | `FORMFLOW_OCR_BACKEND` | `auto` | `auto` (RapidOCR/PaddleOCR primary, Tesseract fallback), `paddleocr`, or `tesseract` |
| `tesseract_cmd` | `FORMFLOW_TESSERACT_CMD` | `tesseract` | Path to `tesseract.exe` for fallback OCR |
| `auto_ready_fill` | `FORMFLOW_AUTO_READY_FILL` | `True` | `True`: high-confidence OCR goes directly to Ready to Fill & Extension; `False`: strict review required |
| `auto_fill_min_confidence` | `FORMFLOW_AUTO_FILL_MIN_CONF` | `0.70` | Minimum OCR confidence for Auto-Ready fill eligibility |
| `ui_mode` | `FORMFLOW_UI_MODE` | `production` | `production` (clean Desk UI with inline Copy, visual document cards & live auto-refresh) or `dev` (full OCR provenance, Queue, Events & Health nav). Switchable anytime from the top bar |
| `inbox_batch_window_sec` | `FORMFLOW_INBOX_BATCH_WINDOW_SEC` | `180.0` | Arrival-time window grouping one customer's files into one case |
| `max_input_file_size` | `FORMFLOW_MAX_INPUT_BYTES` | `26214400` (25 MB) | Larger files go to `needs_attention` |
| `permitted_extensions` | `FORMFLOW_PERMITTED_EXTENSIONS` | `.pdf,.jpg,.jpeg,.png` | Accepted input formats (`.jpeg`/`.png` convert to `.jpg`) |
| `processing_profile_path` | `FORMFLOW_PROFILES` | `<repo>/profiles.json` | Document size targets & crop settings |
| `ocr_rules_path` | `FORMFLOW_OCR_RULES` | `<repo>/ocr_rules.json` | Field labels & OCR extraction tuning |
| `stable_checks` | `FORMFLOW_STABLE_CHECKS` | `3` | Consecutive unchanged size+mtime checks before staging |
| `stable_interval_sec` | `FORMFLOW_STABLE_INTERVAL` | `0.5` | Seconds between stability checks |
| `debounce_sec` | `FORMFLOW_DEBOUNCE_SEC` | `1.0` | Coalesces Windows filesystem event bursts |
| `worker_threads` | `FORMFLOW_WORKER_THREADS` | `1` | Background ingest worker count |
| `max_attempts` | `FORMFLOW_MAX_ATTEMPTS` | `5` | Retry limit per queue job |
| `allow_reset` | `FORMFLOW_ALLOW_RESET` | `False` | Enables `/queue/reset` local wipe button for testing |

## Dynamic Document Profiles & Portal Presets (`profiles.json`)

`profiles.json` (v5) supports **multiple named Portal Presets** and **unlimited custom document types**, all editable live from the **Profiles** page (`/profiles`, visible in **Dev Mode** top navigation):

- **Built-in Portal Presets**:
  - `sarathi` (**Sarathi / Driving Licence**, default) — Photo/Signature `< 50 KB`, Aadhaar `< 500 KB`, all remaining documents `< 200 KB`.
  - `exam_scholarship` (**Exam / Scholarship**) — Photo `< 30 KB`, Signature `< 20 KB`, IDs & certificates `< 300 KB`.
  - `passport_meeseva` (**Passport / MeeSeva**) — Photo/Signature `< 50 KB`, all documents `< 500 KB`.
  - Create, clone, switch, or delete custom presets for any portal.
- **Custom Document Types & Per-Document Output Format (`output_mode`)**:
  - Add or edit any document type (e.g., `voter_id`, `caste_cert`, `income_cert`, `medical_cert`) with its own **Display Name**, **Max KB limit**, **Allowed Output Format** (`JPG only`, `JPG or PDF`, or `PDF only`), **`Inbox\Ready_<tag>_<Sno>.*` export tag**, **Crop Mode** (`document`, `photo`, `signature`, `none`), and optional **Filename Hints / OCR Keywords** for automatic recognition on drop.
  - **Flexible PDF Processing**:
    - **`JPG or PDF` (`jpg_or_pdf`)** — enabled by default for **Learner's Licence (`learner_licence`)** and **TR Document (`tr_document`)**; JPG/PNG inputs output `.jpg`, PDF inputs stay `.pdf`. Large scanned PDFs first try lossless deflate optimization (preserving selectable text/vectors) and automatically fall back to adaptive page compression keeping all pages under the profile's KB limit.
    - **`JPG only` (`jpg`)** — default for `photo`, `signature`, `aadhaar`, `ssc`, `pan`, `driving_licence`. If a PDF is dropped for a JPG-only document (such as an e-Aadhaar PDF), FormFlow automatically renders, crops, and compresses it to `.jpg` under the KB target.
    - **`PDF only` (`pdf`)** — available for any existing or newly added document type; automatically crops and packs JPG/PNG inputs into a compressed single-page `.pdf` under the KB target.
  - Changes apply immediately to newly dropped files; click **Re-prepare existing docs** on `/profiles` anytime to re-process already-staged documents from their untouched originals.

Default `sarathi` preset limits:

| Profile | `Inbox\` Export Name | Output Format | Size Target | Auto-Crop / PDF Behavior |
|---|---|---|---|---|
| `photo` | `Ready_photo_<Sno>.jpg` | `JPG only` | `< 50 KB` | Strip screenshot bars + 3.5:4.5 face-centered passport framing |
| `signature` | `Ready_signature_<Sno>.jpg` | `JPG only` | `< 50 KB` | Strip bars/desk, padded ink-stroke crop, whiten paper background |
| `aadhaar` | `Ready_aadhaar_<Sno>.jpg` | `JPG only` | `< 500 KB` | Strip bars, trim margins, rectify card (auto-converts PDF → JPG) |
| `ssc` | `Ready_10th_memo_<Sno>.jpg` | `JPG only` | `< 200 KB` | Strip bars, trim scan margins, auto-orient (auto-converts PDF → JPG) |
| `learner_licence` | `Ready_ll_<Sno>.jpg / .pdf` | `JPG or PDF` | `< 200 KB` | Images: auto-crop JPEG; PDFs: lossless/adaptive multi-page compression |
| `driving_licence` | `Ready_dl_<Sno>.jpg` | `JPG only` | `< 200 KB` | Strip bars, trim margins, rectify card (auto-converts PDF → JPG) |
| `pan` | `Ready_pan_<Sno>.jpg` | `JPG only` | `< 200 KB` | Strip bars, trim margins, rectify card (auto-converts PDF → JPG) |
| `tr_document` | `Ready_tr_<Sno>.jpg / .pdf` | `JPG or PDF` | `< 200 KB` | Images: auto-crop JPEG; PDFs: lossless/adaptive multi-page compression |

## Auto-Ready vs. Strict Review Mode

- **Auto-Ready Mode (default, `auto_ready_fill = True`)**:
  - High-confidence OCR fields (`>= 0.70`) appear immediately in **Ready to Fill** and the Chrome extension without clicking **Validate & Save**.
  - When multiple documents disagree on a field, the highest-confidence value is selected automatically; manual edits always override OCR.
  - Low-confidence reads (`< 0.70`) remain gated until confirmed in optional **Review & Validate**.
  - Clicking **Validate & Save** confirms all non-empty reviewed fields (including unchanged OCR `address`).
- **Strict Mode (`FORMFLOW_AUTO_READY_FILL=false`)**:
  - Blocks Ready to Fill and `/api/*` fill endpoints with `409 case_not_reviewed` or `409 unresolved_conflicts` until an operator explicitly completes **Review & Validate**.

## Manual Fallback Tools

- **Manual Crop & Reclassify** (`/documents/<id>/crop`): Interactive offline canvas to adjust crop box, rotate 90°, toggle signature background whitening, or change document type. Saving immediately updates `data/prepared/` and `Inbox\Ready_*`.
- **Queue & Retry** (`/queue`): Shows intake job states, document preparation status, and Retry buttons.
