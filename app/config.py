"""Central configuration for FormFlow (Phase 1).

All paths are stored here, never scattered through the code.
Overrides (highest wins):
  1. explicit dict passed to load_config()
  2. FORMFLOW_* environment variables
  3. config.local.json next to repo root (gitignored, optional)
  4. built-in defaults
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PORT = 5000
DEFAULT_HOST = "127.0.0.1"  # local-only; never default to 0.0.0.0
DEFAULT_MAX_INPUT_BYTES = 25 * 1024 * 1024
DEFAULT_PERMITTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


@dataclass
class FormFlowConfig:
    inbox_dir: Path = field(default_factory=lambda: REPO_ROOT / "Inbox")
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")
    backup_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "backups")
    db_path: Path = field(default_factory=lambda: REPO_ROOT / "data" / "formflow.db")
    max_input_file_size: int = DEFAULT_MAX_INPUT_BYTES
    permitted_extensions: set[str] = field(
        default_factory=lambda: set(DEFAULT_PERMITTED_EXTENSIONS)
    )
    tesseract_cmd: str = "tesseract"
    processing_profile_path: Path = field(
        default_factory=lambda: REPO_ROOT / "profiles.json"
    )
    ocr_rules_path: Path = field(
        default_factory=lambda: REPO_ROOT / "ocr_rules.json"
    )
    port: int = DEFAULT_PORT
    host: str = DEFAULT_HOST
    secret_key: str = "dev-only-change-me"
    # Phase 2: watcher / ingest tuning
    stable_checks: int = 3
    stable_interval_sec: float = 0.5
    debounce_sec: float = 1.0
    worker_threads: int = 1
    max_attempts: int = 5
    # Testing helper: show the "delete all data" reset button. Enabled by
    # default while testing; set FORMFLOW_ALLOW_RESET=0 for handover.
    allow_reset: bool = True

    def ensure_data_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "staging").mkdir(parents=True, exist_ok=True)

    def inbox_status(self) -> tuple[bool, str]:
        """Return (exists_and_dir, message). Watcher uses this for helpful errors."""
        p = self.inbox_dir
        if not p.exists():
            return False, f"Inbox not found: {p}. Create it or set FORMFLOW_INBOX_DIR."
        if not p.is_dir():
            return False, f"Inbox path is not a directory: {p}."
        if not os.access(p, os.R_OK):
            return False, f"Inbox is not readable: {p}."
        return True, f"Inbox OK: {p}."


def load_config(overrides: dict | None = None) -> FormFlowConfig:
    cfg = FormFlowConfig()

    # 1. optional local JSON file (gitignored)
    local_json = REPO_ROOT / "config.local.json"
    file_vals: dict = {}
    if local_json.exists():
        try:
            file_vals = json.loads(local_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            file_vals = {}

    def pick(key: str, env_name: str, default):
        if overrides and key in overrides and overrides[key] is not None:
            return overrides[key]
        ev = _env(env_name)
        if ev is not None:
            return ev
        if key in file_vals and file_vals[key] not in (None, ""):
            return file_vals[key]
        return default

    def as_path(v) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (REPO_ROOT / p)

    cfg.inbox_dir = as_path(pick("inbox_dir", "FORMFLOW_INBOX_DIR", cfg.inbox_dir))
    cfg.data_dir = as_path(pick("data_dir", "FORMFLOW_DATA_DIR", cfg.data_dir))
    cfg.backup_dir = as_path(pick("backup_dir", "FORMFLOW_BACKUP_DIR", cfg.backup_dir))
    cfg.db_path = as_path(pick("db_path", "FORMFLOW_DB", cfg.db_path))
    cfg.processing_profile_path = as_path(
        pick("processing_profile_path", "FORMFLOW_PROFILES", cfg.processing_profile_path)
    )
    cfg.ocr_rules_path = as_path(
        pick("ocr_rules_path", "FORMFLOW_OCR_RULES", cfg.ocr_rules_path)
    )
    cfg.tesseract_cmd = str(
        pick("tesseract_cmd", "FORMFLOW_TESSERACT_CMD", pick("tesseract_cmd", "TESSERACT_CMD", cfg.tesseract_cmd))
    )
    try:
        cfg.max_input_file_size = int(
            pick("max_input_file_size", "FORMFLOW_MAX_INPUT_BYTES", cfg.max_input_file_size)
        )
    except (TypeError, ValueError):
        cfg.max_input_file_size = DEFAULT_MAX_INPUT_BYTES

    ext_raw = pick("permitted_extensions", "FORMFLOW_PERMITTED_EXTENSIONS", None)
    if ext_raw is not None:
        if isinstance(ext_raw, str):
            parts = {e.strip().lower() for e in ext_raw.replace(";", ",").split(",")}
        else:
            parts = {str(e).strip().lower() for e in ext_raw}
        parts = {e if e.startswith(".") else f".{e}" for e in parts if e and e != "."}
        if parts:
            cfg.permitted_extensions = parts

    try:
        cfg.port = int(pick("port", "FORMFLOW_PORT", cfg.port))
    except (TypeError, ValueError):
        cfg.port = DEFAULT_PORT

    cfg.secret_key = str(pick("secret_key", "FORMFLOW_SECRET_KEY", cfg.secret_key))

    raw_reset = pick("allow_reset", "FORMFLOW_ALLOW_RESET", cfg.allow_reset)
    if isinstance(raw_reset, bool):
        cfg.allow_reset = raw_reset
    else:
        cfg.allow_reset = str(raw_reset).strip().lower() not in ("0", "false", "no", "off")

    for key, env, default, cast in (
        ("stable_checks", "FORMFLOW_STABLE_CHECKS", cfg.stable_checks, int),
        ("stable_interval_sec", "FORMFLOW_STABLE_INTERVAL", cfg.stable_interval_sec, float),
        ("debounce_sec", "FORMFLOW_DEBOUNCE_SEC", cfg.debounce_sec, float),
        ("worker_threads", "FORMFLOW_WORKER_THREADS", cfg.worker_threads, int),
        ("max_attempts", "FORMFLOW_MAX_ATTEMPTS", cfg.max_attempts, int),
    ):
        try:
            cfg.__dict__[key] = cast(pick(key, env, default))
        except (TypeError, ValueError):
            cfg.__dict__[key] = default
    if cfg.stable_checks < 2:
        cfg.stable_checks = 2
    if cfg.worker_threads < 1:
        cfg.worker_threads = 1

    # Host is intentionally NOT configurable via env to prevent accidental
    # public binding. Override must be explicit code opt-in.
    if overrides and "host" in overrides and overrides["host"] is not None:
        cfg.host = str(overrides["host"])
    else:
        cfg.host = DEFAULT_HOST

    # Safety: db must live under data_dir (or be :memory: for tests)
    if str(cfg.db_path) != ":memory:":
        try:
            cfg.db_path.relative_to(cfg.data_dir)
        except ValueError:
            # Allow explicit out-of-tree db (e.g. temp test dir), but keep
            # the default layout documented. No error needed.
            pass

    return cfg
