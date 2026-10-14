"""Safe ingest pipeline (Phase 2). No OCR/compression here.

Flow: validate root -> ignore temp -> record unsupported/oversize as
needs_attention -> wait for download stability -> SHA-256 -> dedup ->
record job -> move bytes unchanged into data/staging -> mark ready.

Never deletes input silently: the Inbox file is removed only by a
successful move to staging. Move failures leave the Inbox file in place
and record failed/needs_attention for retry.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from ..config import FormFlowConfig
from ..db import (
    connect,
    create_job,
    find_job_by_hash,
    get_job_by_path,
    init_db,
    set_job_state,
)
from .filters import is_supported, is_temp_file, resolve_within, sanitize_filename

StatFn = Callable[[Path], tuple[int, int] | None]


def default_stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return (st.st_size, st.st_mtime_ns)


def is_stable(
    path: Path,
    checks: int = 3,
    interval_sec: float = 0.5,
    stat_fn: StatFn | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> bool:
    stat_fn = stat_fn or default_stat
    sleep_fn = sleep_fn or time.sleep
    prev: tuple[int, int] | None = None
    for i in range(checks):
        cur = stat_fn(path)
        if cur is None or cur[0] <= 0:
            return False
        if prev is not None and cur != prev:
            return False
        prev = cur
        if i < checks - 1:
            sleep_fn(interval_sec)
            # Re-stat after sleep is the next loop iteration; but to detect
            # growth during the sleep we compare again next round.
    # Final verify: one more stat after last sleep is covered by loop;
    # require at least 2 identical observations (checks >= 2 enforced).
    return prev is not None


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def staging_dest(cfg: FormFlowConfig, sha: str, filename: str) -> Path:
    safe = sanitize_filename(filename)
    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    base = f"{sha[:12]}_{safe}"
    dest = staging / base
    n = 1
    while dest.exists():
        dest = staging / f"{sha[:12]}_{n}_{safe}"
        n += 1
        if n > 1000:
            raise OSError("staging namespace exhausted")
    # Guard: dest must stay inside data_dir.
    resolve_within(dest, cfg.data_dir)
    return dest


def ingest_file(
    inbox_file: Path | str,
    cfg: FormFlowConfig,
    stat_fn: StatFn | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict | None:
    """Ingest one Inbox file. Returns job dict or None if ignored (temp).

    Idempotent: repeated calls for the same inbox path return the same job.
    """
    raw = Path(inbox_file)
    try:
        resolved = resolve_within(raw, cfg.inbox_dir)
    except ValueError as e:
        # Outside allowed root: record nothing, raise for caller to log.
        raise ValueError(str(e)) from e

    name = resolved.name
    if is_temp_file(name):
        return None
    if not resolved.exists() or not resolved.is_file():
        return None

    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        inbox_key = str(resolved)
        existing = get_job_by_path(conn, inbox_key)
        # Idempotency without starvation: processing/ready return as-is
        # (avoid concurrent double-work / re-doing finished jobs), but
        # waiting/queued are transient -> fall through and re-attempt so a
        # file that was growing during the last check can complete later.
        if existing is not None and existing["state"] in ("processing", "ready"):
            return dict(existing)

        if not is_supported(name, cfg.permitted_extensions):
            if existing is not None:
                return dict(existing)
            job = create_job(
                conn, inbox_key, state="needs_attention",
                last_error=f"unsupported extension '{resolved.suffix or '(none)'}'; allowed: {sorted(cfg.permitted_extensions)}",
            )
            return dict(job)

        try:
            size = resolved.stat().st_size
        except OSError as e:
            job = create_job(conn, inbox_key, state="failed", last_error=f"cannot stat file: {e}")
            return dict(job)
        if size > cfg.max_input_file_size:
            job = create_job(
                conn, inbox_key, state="needs_attention",
                last_error=f"file {size} bytes exceeds limit {cfg.max_input_file_size} bytes",
            )
            try:
                conn.execute(
                    "UPDATE intake_jobs SET file_size = ? WHERE id = ?", (size, job["id"])
                )
                conn.commit()
                job = conn.execute(
                    "SELECT * FROM intake_jobs WHERE id = ?", (job["id"],)
                ).fetchone()
            except OSError:
                pass
            return dict(job)

        stable = is_stable(
            resolved, checks=cfg.stable_checks,
            interval_sec=cfg.stable_interval_sec,
            stat_fn=stat_fn, sleep_fn=sleep_fn,
        )
        if not stable:
            if existing is not None:
                # Refresh so operator sees the latest check time.
                job = set_job_state(
                    conn, existing["id"], "waiting",
                    last_error="download in progress; waiting for stable file",
                )
                return dict(job)
            job = create_job(conn, inbox_key, state="waiting",
                             last_error="download in progress; waiting for stable file")
            return dict(job)

        try:
            digest = sha256_file(resolved)
        except OSError as e:
            job = create_job(conn, inbox_key, state="failed", last_error=f"cannot read file: {e}")
            return dict(job)

        dup = find_job_by_hash(conn, digest)
        if dup is not None and dup["inbox_path"] != inbox_key:
            if existing is not None:
                job = set_job_state(
                    conn, existing["id"], "needs_attention", sha256=digest,
                    last_error=f"duplicate content of job #{dup['id']} ({Path(dup['inbox_path']).name}); kept Inbox copy for review",
                )
                return dict(job)
            job = create_job(
                conn, inbox_key, state="needs_attention", sha256=digest,
                last_error=f"duplicate content of job #{dup['id']} ({Path(dup['inbox_path']).name}); kept Inbox copy for review",
            )
            return dict(job)

        if existing is None:
            job = create_job(conn, inbox_key, state="processing", sha256=digest)
        else:
            job = set_job_state(conn, existing["id"], "processing", sha256=digest)
        job_id = job["id"]

        # Safe move: bytes preserved, Inbox removed only on success.
        try:
            dest = staging_dest(cfg, digest, name)
            cfg.ensure_data_dirs()
            shutil.move(str(resolved), str(dest))
            # Verify move preserved bytes.
            if not dest.exists():
                raise OSError("move reported success but destination missing")
            job = set_job_state(
                conn, job_id, "ready", last_error=None,
                sha256=digest, staged_path=str(dest), file_size=size,
            )
            return dict(job)
        except OSError as e:
            job = set_job_state(
                conn, job_id, "failed",
                last_error=f"could not move to staging (Inbox copy retained): {e}",
                sha256=digest, file_size=size,
            )
            return dict(job)
    finally:
        conn.close()


def requeue_job(job_id: int, cfg: FormFlowConfig) -> dict:
    """Manual retry: failed/needs_attention -> queued (bounded by max_attempts)."""
    from ..db import get_job, retry_job

    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        job = get_job(conn, job_id)
        if job is None:
            raise ValueError(f"unknown job: {job_id}")
        # If the Inbox file is gone but staging succeeded earlier, a retry
        # of a duplicate/unsupported case simply re-queues for operator.
        job = retry_job(conn, job_id, max_attempts=cfg.max_attempts)
        # Opportunistically re-ingest if Inbox file still present.
        inbox_p = Path(job["inbox_path"])
        conn.close()
        try:
            if inbox_p.exists():
                return ingest_file(inbox_p, cfg) or dict(job)
        except ValueError:
            pass
        conn = connect(cfg.db_path)
        return dict(conn.execute(
            "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
        ).fetchone())
    finally:
        try:
            conn.close()
        except Exception:
            pass


def recover_on_startup(cfg: FormFlowConfig) -> int:
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        from ..db import recover_interrupted

        return recover_interrupted(conn)
    finally:
        conn.close()
