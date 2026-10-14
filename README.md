# FormFlow (Phase 1 — foundation)

Local-first helper for a Windows driving-licence service desk (5–15 applications/day).
Phase 1 provides a runnable Flask + SQLite base. Watcher, OCR, review UI, and the
Chrome extension arrive in later phases.

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

Optional `config.local.json` in repo root (gitignored) accepts the same keys.
`host` is fixed to `127.0.0.1` and can only be changed by explicit code override.

If `Inbox` is missing the app prints a warning with the expected path.

## Data model (SQLite, source of truth)

`customers`, `cases` (internal `case_id` like `CASE-20261014-001`; `appl_number`
nullable until operator confirms — never invented), `documents`, `extracted_fields`
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
