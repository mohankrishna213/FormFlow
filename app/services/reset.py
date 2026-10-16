"""Testing helper: wipe all pipeline data and start fresh.

Clears every DB table (FK-safe order) plus generated files under
data/staging, data/prepared and data/ocr. The Inbox is left alone so
dropped test files can be re-ingested on the next run.
"""
from __future__ import annotations

import shutil

from ..config import FormFlowConfig
from ..db import connect, init_db

_TABLES_IN_DELETE_ORDER = (
    "conflict_acks",
    "extracted_fields",
    "case_events",
    "documents",
    "intake_jobs",
    "cases",
    "customers",
)

_GENERATED_DIRS = ("staging", "prepared", "ocr")


def reset_all(cfg: FormFlowConfig) -> dict:
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    counts: dict[str, int] = {}
    try:
        for table in _TABLES_IN_DELETE_ORDER:
            row = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()
            counts[table] = row["n"]
            conn.execute(f"DELETE FROM {table}")
        try:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('conflict_acks','extracted_fields','case_events','documents','intake_jobs','cases','customers')"
            )
        except Exception:
            pass  # sqlite_sequence may not exist; harmless either way
        conn.commit()
    finally:
        conn.close()
    files_removed = 0
    for dirname in _GENERATED_DIRS:
        target = cfg.data_dir / dirname
        if not target.is_dir():
            continue
        for child in list(target.iterdir()):
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                files_removed += 1
            except OSError:
                pass
    counts["files_removed"] = files_removed
    return counts
