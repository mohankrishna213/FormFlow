# FormFlow (Phase 6 — validated fill-packet API & Ready to fill UI)

Local-first helper for a Windows driving-licence service desk (5–15 applications/day).
Phases 1–4 provided base, watcher, preparation, and OCR; Phase 5 added the
dashboard, one-button review (six extracted fields), and merge/assign controls.
Phase 6 adds the authenticated local fill-packet API and Ready to fill view with
Copy buttons (Chrome extension deferred to Phase 7; see `docs/phase6_fill_packet_api.md`).

## Quick start (Windows)

1. Install Python 3.11+ (3.11/3.12 recommended) and Tesseract-OCR (path configurable; required from Phase 4).
2. Double-click `run.bat`, or:
   ```bat
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python run.py
   ```
3. Open `http://127.0.0.1:5000`. Health: `http://127.0.0.1:5000/healthz`.

The app binds `127.0.0.1` only. It never binds `0.0.0.0` by default.

## Configuration

All paths live in `app/config.py` via `load_config()`. Overrides:

| Key | Env var | Default |
|---|---|---|
| `inbox_dir` | `FORMFLOW_INBOX_DIR` | `<repo>/Inbox` |
| `data_dir` | `FORMFLOW_DATA_DIR` | `<repo>/data` |
| `backup_dir` | `FORMFLOW_BACKUP_DIR` | `<repo>/data/backups` |
| `db_path` | `FORMFLOW_DB` | `<repo>/data/formflow.db` |
| `max_input_file_size` | `FORMFLOW_MAX_INPUT_BYTES` | 26214400 (25 MB) |
| `permitted_extensions` | `FORMFLOW_PERMITTED_EXTENSIONS` | `.pdf,.jpg,.jpeg,.png` |
| `tesseract_cmd` | `FORMFLOW_TESSERACT_CMD` / `TESSERACT_CMD` | `tesseract` |
| `processing_profile_path` | `FORMFLOW_PROFILES` | `<repo>/profiles.json` |
| `port` | `FORMFLOW_PORT` | 5000 |
| `secret_key` | `FORMFLOW_SECRET_KEY` | dev-only (override in production use) |
| `ocr_rules_path` | `FORMFLOW_OCR_RULES` | `<repo>/ocr_rules.json` |
| `tesseract_cmd` | `FORMFLOW_TESSERACT_CMD` / `TESSERACT_CMD` | `tesseract` (auto-fallback to `C:\Program Files\Tesseract-OCR`) |
| `stable_checks` | `FORMFLOW_STABLE_CHECKS` | 3 (size+mtime reads to confirm download finished) |
| `stable_interval_sec` | `FORMFLOW_STABLE_INTERVAL` | 0.5 |
| `debounce_sec` | `FORMFLOW_DEBOUNCE_SEC` | 1.0 (coalesces Windows event bursts) |
| `worker_threads` | `FORMFLOW_WORKER_THREADS` | 1 |
| `max_attempts` | `FORMFLOW_MAX_ATTEMPTS` | 5 (manual retry bound) |

Optional `config.local.json` in repo root (gitignored) accepts the same keys.
`host` is fixed to `127.0.0.1` and can only be changed by explicit code override.

If `Inbox` is missing the app prints a warning with the expected path, and the
watcher refuses to start with a helpful error instead of watching the wrong folder.

## Inbox watcher (Phase 2)

- `python run.py` starts the web UI plus one background worker and a watchdog
  observer on the configured `Inbox` only (non-recursive; never `data/`).
- Temp/in-progress files (`.crdownload`, `.tmp`, `.part`, `~$*.docx`,
  `Thumbs.db`, …) are ignored with no job record. Unsupported extensions and
  oversize files become `needs_attention` jobs; the Inbox copy is kept.
- Files must be size+mtime stable for `stable_checks` reads before hashing.
  Growing downloads stay `waiting` and are re-polled automatically; they are
  never processed early.
- Each stable file is SHA-256 hashed (dedup) and moved byte-for-byte into
  `data/staging/<hash>_<name>`. The Inbox copy is removed only by that
  successful move; failures leave it in place as `failed` for retry.
- Open `/queue` for job states, errors, and manual Retry (bounded by
  `max_attempts`). Restart requeues interrupted (`processing`/`waiting`) jobs;
  finished and operator-held states are preserved.

## Document preparation (Phase 3)

- After ingest stages a file, the worker classifies it (filename clues, file
  properties; OCR hints hook for Phase 4) and writes a **prepared copy** to
  `data/prepared/`. The staged original is never modified.
- Profiles in `profiles.json` (editable): photo and signature target <50 KB,
  LL supporting document <200 KB. These are **local configuration, not
  guaranteed official Sarathi limits** — check the exact service/form and edit.
- Images: EXIF orientation applied, aspect ratio preserved, never upscaled,
  JPEG-safe color conversion; quality/dimension steps per profile. Output must
  be strictly below the limit or the document is marked `needs_attention`
  (never silently over-compressed). No automatic cropping.
- PDFs: optimized only (`garbage` + `deflate`); all pages and readable text
  preserved; oversize PDFs go to `needs_attention` rather than being rasterized.
- Prepared names: `Name_CASE-xxx_doctype.jpg` before the official number, or
  `Name_TG123_doctype.jpg` once the operator supplies it (never invented).
  Original filename kept as metadata; every row stores hashes, MIME, sizes,
  profile name/version, and the failure reason shown on `/queue` with Retry.

## Local OCR + extraction (Phase 4)

- Text PDFs are read with PyMuPDF and never touch OCR; scanned pages are
  rendered at configurable DPI and OCR'd page by page; images get conservative
  cleanup (orientation, grayscale, contrast) in memory only. No cloud, no LLM.
- Missing Tesseract is a recoverable `unavailable` state with install
  instructions (PATH or `FORMFLOW_TESSERACT_CMD`), never a crash. Text PDFs
  keep working without it.
- **Implemented (deterministic):** `full_name` (labeled, validated 2–5 words),
  `father_name` (labeled; matches both the "Father's Name" family and the
  "Son/Wife/Daughter of" family — the printed label itself is not stored),
  `dob` (labeled; ISO `YYYY-MM-DD`, month-name and `YYYY-MM-DD` forms; raw kept
  when `DD/MM` order is ambiguous), `address` (labeled block incl. numbered
  `6. Present Address` forms, or PIN heuristic), `licence_no` (labeled or
  standalone `No <digits>` line; display + review anchor only, never matching),
  `application_no` (labeled digits-only candidate; never matching).
- Filename classification tolerates suffixed keywords (`license1`); short
  hints stay exact-only (`sign` won't fire on `assign`). Phone photos are
  upscaled 2x with PSM 6 (both in `ocr_rules.json`). Small/degraded print
  still misreads — surfaced, never guessed; flat, shadow-free captures help
  more than any setting.
- **Heuristics (marked as such):** ambiguous dates stay raw at conf 0.4 and
  never match; unlabeled dates are ignored, not assigned.
- Matching auto-attaches ONLY on unique normalized name + ISO dob; name-only,
  photos/signatures, and conflicts go provisional (`needs_attention`) for
  manual assignment/merge. Raw page text lands in gitignored
  `data/ocr/doc_<id>.json` for review/debugging, never in logs.

## Operator review (Phase 5)

- `/` dashboard (recent cases + next action), `/cases` (search, status filter,
  pagination), `/cases/<id>` (fields with provenance, original/prepared
  preview + download, events), `/needs-attention` (actionable aggregate).
- Review form covers all six extracted fields with labels: Full name,
  Father's / related person's name, Date of birth, Address, Driving licence
  number, Application number. Candidate source, document/page, and confidence
  are shown (low-confidence values are flagged); missing stays blank.
- The form has no per-field checkboxes — one **Validate & Save** button.
  Required name/DOB validated, ambiguous dates rejected with guidance,
  nothing is ever silently filled. Reviewed = operator acknowledgement, not
  authenticity proof; later edits or merges invalidate it (version bump).
- Conflicted fields (same key, materially different extracted values) list
  every candidate with its source; an unchanged conflicted value blocks
  saving. The operator resolves by picking a listed value (radio) or typing a
  correction — including confirming the current best candidate or keeping the
  saved value ("Keep it blank" when it is deliberately empty) — and the
  recorded choice governs later saves without repeat blocking. Contradictory
  evidence that arrives afterwards (attach/merge) re-opens the conflict until
  the operator confirms the value again; agreeing evidence does not.
- `application_no` is the OCR candidate key; the operator-confirmed official
  number is stored in the existing `cases.appl_number` column (no duplicate
  storage). A saved official value always prefills the form and is never
  erased by an empty OCR result. A single candidate pre-fills as an editable
  draft; saving it records it officially. Application number and licence
  number are kept separate everywhere.
- `father_name` keeps its internal key for both the "Father's Name" and
  "Son/Wife/Daughter of" printed label families; candidates do not retain the
  printed label, so the UI uses the neutral label and asks the operator to
  check the document (no relationship is inferred).
- Merge shows a confirmation page first, moves documents/fields with history
  preserved, and records events. All state-changing forms carry CSRF tokens;
## Fill-packet API and Ready to fill view (Phase 6 & 6.1)
- Ready to fill view at `/cases/<case_id>/fill-ready` accessible via "Ready to Fill"
  button on the Case Detail page for reviewed cases. Shows confirmed values
  matching the workflow allowlist with per-field Copy buttons. Missing or
  unconfirmed values cannot bypass review and are marked unavailable.
- Authenticated local API for future Chrome extension (Phase 7):
  - `GET /api/cases`: minimal case listing.
  - `GET /api/cases/<case_id>/fill-packet`: returns validated packet with operator-confirmed
    values only (strictly manual `extracted_fields` rows and `cases.appl_number`; no raw
    OCR candidates, image paths, or unconfirmed fields). Requires `status == 'reviewed'`;
    blocks on unresolved or fresh conflicts (`409 Conflict`).
  - `POST /api/cases/<case_id>/events`: records extension/operator events to `case_events`.
    Enforces strict payload schema, positive integer review versions (rejecting booleans),
    and supported internal field keys (`copy_attempt` requires valid field key).
- API security: Cryptographically secure 256-bit token stored in `data/.api_token`
  (gitignored; preserved across restarts; 0o600 permissions on POSIX). Bearer token
  authentication required on all `/api/*` endpoints with constant-time comparison
  (`hmac.compare_digest`). Bound strictly to `127.0.0.1` with Origin validation
  (rejects external origins, unexpected localhost ports, Origin: null, and malformed origins;
  permits omitted Origin for local CLI/PowerShell tools).
- Review freshness guarantee: New document evidence attaching to a reviewed case
  always invalidates the reviewed state (`status = 'needs_attention'`), requiring
  re-review before a fill packet can be issued.
- Details in `docs/phase6_fill_packet_api.md`.

## Data model (SQLite, source of truth)

`customers`, `cases` (internal `case_id` like `CASE-20261014-001`; `appl_number`
nullable until operator confirms — never invented; the OCR candidate key
`application_no` stays in `extracted_fields` as evidence and is never stored
twice), `documents`, `extracted_fields`
(value + source + confidence, no per-field verified flags), `intake_jobs`
(`waiting|queued|processing|needs_attention|ready|failed`), `case_events` (audit trail).

DB init: `python -c "from app.db import init_db; from app.config import load_config; init_db(load_config().db_path)"`.

## Tests

```bat
pip install -r requirements.txt
python -m pytest -v
```

## Project layout

```text
app/            Flask factory, config, db, routes, templates
tests/          pytest suite (synthetic data only)
Inbox/          watched folder (gitignored; created on first run)
data/           formflow.db, staging/, backups/, logs/ (gitignored)
profiles.json   per-document size targets (configurable)
run.py / run.bat  local-only launchers
```

## Security notes

- Local-only; no cloud OCR/LLM/remote DB.
- `data/`, `Inbox/`, `*.db`, tokens, `config.local.json` are gitignored.
- Never commit real customer documents. Use synthetic fixtures in `tests/`.
