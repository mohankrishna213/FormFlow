# Architecture & Pipeline

FormFlow is a local-only Flask + SQLite web app with a background inbox watcher, OpenCV document preparation, local OCR extraction, and a Manifest V3 Chrome extension companion.

## End-to-End Pipeline

```text
Inbox\ (customer downloads)
  │  Watchdog + stability check (size + mtime unchanged)
  ▼
1. Ingest (app/services/ingest.py)
  ├─ Ignore temp/download files (.crdownload, .tmp, Ready_*)
  ├─ SHA-256 hash & dedup
  └─ Move untouched original -> data/staging/<hash>_<name>
  │
  ▼
2. Fast Preparation & Auto-Crop (app/services/prepare.py + document_cv.py)
  ├─ Fast classify (filename + Haar face + lightweight text/ink check)
  ├─ Auto-crop per profile (strip screenshot bars, rectify cards,
  │   frame passphoto 3.5:4.5, whiten signature paper)
  ├─ Compress .jpg strictly under profile KB limit -> data/prepared/
  └─ Export immediately -> Inbox\Ready_<type>_<Sno>.jpg
  │
  ▼
3. Async Local OCR & Field Extraction (app/services/extract.py + ocr.py + fields.py)
  ├─ Runs on native-resolution original/cropped RGB (not compressed copy)
  ├─ Primary: RapidOCR / PaddleOCR CPU (ocr_paddle.py); fallback: Tesseract
  ├─ Re-crops & re-exports Inbox\Ready_* if OCR reveals exact doc type/rotation
  ├─ Extracts 18+ structured fields + Indian DD/MM/YYYY -> ISO YYYY-MM-DD DOB
  └─ Groups burst downloads (180s arrival window) into one customer case
  │
  ▼
4. Auto-Ready Fill & Extension (app/services/fill_packet.py + extension/)
  ├─ High-confidence OCR (>= 0.70) + operator edits flow straight to Ready to Fill
  ├─ Multi-document mismatches auto-select highest-confidence value
  └─ Chrome extension autofills Sarathi inputs & attaches prepared files in 1 click
```

## SQLite Data Model (`app/db.py`)

| Table | Purpose |
|---|---|
| `customers` | Reusable customer records (`display_name`, `dob`) |
| `customer_fields` | Confirmed customer-scoped fields reusable across cases |
| `cases` | Internal case (`CASE-YYYYMMDD-NNN`), optional official `appl_number`, `status`, `review_version` |
| `documents` | Staged original path, prepared path, `inbox_export_path`, `doctype`, `profile_name`, hashes, OCR status |
| `extracted_fields` | Per-field OCR candidates (`method`, `confidence`, `source_page`) and manual edits (`method='manual'`) |
| `conflict_acks` | Operator conflict-resolution anchors (`upto_id`, normalized settled value) |
| `intake_jobs` | Watcher queue states (`waiting`, `queued`, `processing`, `needs_attention`, `ready`, `failed`) |
| `case_events` | Sanitized audit trail (no applicant PII) |

## Repository Map

```text
app/
  __init__.py           Flask app factory, CSRF protection, template filters
  config.py             Config dataclass, env vars, optional config.local.json
  db.py                 SQLite schema, idempotent migrations, CRUD helpers
  routes/
    __init__.py         Shared open_db() / get_cfg() route helpers
    dashboard.py        /, /cases, /needs-attention
    cases.py            Case detail, Review & Validate, Fill Ready, Manual Crop, Merge/Assign
    profiles.py         Dev-mode Document Profiles & Portal Presets editor (/profiles)
    queue.py            /queue, retry controls, optional dev reset
    api.py              Authenticated /api/* JSON + prepared-file endpoints
    health.py           /healthz
  services/
    watcher.py          Watchdog observer on Inbox/ (non-recursive)
    ingest.py           File stability check, SHA-256 dedup, atomic staging
    jobs.py             Decoupled fast ingest worker + sequential OCR worker
    classify.py         Rule-based classifier for all 8 Indian document types
    document_cv.py      OpenCV auto-crop, perspective rectify, face framing, ink cleaning
    prepare.py          Profile sizing (<50KB/<200KB/<500KB), Inbox\Ready_* export, reprepare
    ocr.py              PDF text + image OCR orchestrator, auto-rotate, selective retry
    ocr_paddle.py       RapidOCR / PaddleOCR local CPU adapter + 2-column line grouping
    preprocess.py       OpenCV gray/threshold retry helpers for Tesseract fallback
    fields.py           Deterministic Indian ID/memo field extraction & address decomposition
    extract.py          End-to-end extraction orchestrator & data/ocr/ sidecar writer
    match.py            Case matching (unique Name+DOB) + arrival-time burst grouping
    catalog.py          Extensible field catalogue (labels, categories, scopes)
    review.py           Highest-confidence ranking, conflict freshness, Validate & Save, merge
    fill_packet.py      Auto-Ready & strict fill-packet builder
    api_auth.py         Local 256-bit Bearer token & strict Origin policy
    filters.py          Path containment & filename sanitization
    profiles.py         profiles.json loader
    reset.py            Local reset helper (opt-in via FORMFLOW_ALLOW_RESET)
  templates/            Server-rendered Jinja2 HTML views
extension/
  manifest.json         Chrome Extension Manifest V3 (sarathi.parivahan.gov.in only)
  api_client.js         Bearer-authenticated localhost API client
  portal_adapter.js     Sarathi DOM inspector, autofill engine, and file-input attachment
  content_script.js     Message bridge between popup and portal_adapter.js
  popup.{html,css,js}   Extension UI (case picker, field preview, 1-click fill & file attach)
```
