"""Ingest pipeline: stability, dedup, errors, recovery (synthetic files only)."""
from pathlib import Path

import pytest

from app.config import load_config
from app.db import connect, create_job, get_job_by_path, init_db, list_jobs
from app.services.ingest import (
    ingest_file,
    is_stable,
    recover_on_startup,
    requeue_job,
)


def make_cfg(tmp_cfg):
    d = dict(tmp_cfg)
    d.update({"stable_checks": 2, "stable_interval_sec": 0.0, "max_attempts": 3})
    cfg = load_config(d)
    cfg.ensure_data_dirs()
    return cfg


def write_inbox(cfg, name, data: bytes) -> Path:
    p = cfg.inbox_dir / name
    p.write_bytes(data)
    return p


def test_is_stable_constant():
    p = Path("x.pdf")
    seq = [(100, 1), (100, 1)]
    it = iter(seq)
    assert is_stable(p, checks=2, interval_sec=0, stat_fn=lambda _: next(it), sleep_fn=lambda s: None)


def test_is_stable_growing_is_unstable():
    seq = [(100, 1), (200, 2)]
    it = iter(seq)
    assert not is_stable(Path("x"), checks=2, interval_sec=0,
                         stat_fn=lambda _: next(it), sleep_fn=lambda s: None)


def test_is_stable_zero_byte_is_unstable():
    assert not is_stable(Path("x"), checks=2, interval_sec=0,
                         stat_fn=lambda _: (0, 1), sleep_fn=lambda s: None)


def test_drop_creates_one_ready_job_and_preserves_bytes(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = write_inbox(cfg, "scan.pdf", b"%PDF-synthetic-" * 100)
    import hashlib
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    job = ingest_file(src, cfg, sleep_fn=lambda s: None)
    assert job is not None and job["state"] == "ready"
    assert job["sha256"] == before
    assert not src.exists(), "Inbox file must be moved"
    staged = Path(job["staged_path"])
    assert staged.exists()
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == before
    conn = connect(cfg.db_path)
    try:
        assert len(list_jobs(conn)) == 1
    finally:
        conn.close()


def test_repeated_events_do_not_duplicate(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = write_inbox(cfg, "photo.jpg", b"img-bytes-12345")
    j1 = ingest_file(src, cfg, sleep_fn=lambda s: None)
    assert j1["state"] == "ready"
    # Second event for same path after move: file gone -> None, still 1 job.
    j2 = ingest_file(src, cfg, sleep_fn=lambda s: None)
    assert j2 is None
    conn = connect(cfg.db_path)
    try:
        assert len(list_jobs(conn)) == 1
    finally:
        conn.close()


def test_incomplete_download_not_processed_early(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = write_inbox(cfg, "big.pdf", b"partial-bytes")
    sizes = [(14, 1), (999, 2)]  # growth between checks
    it = iter(sizes)
    job = ingest_file(src, cfg, stat_fn=lambda _: next(it), sleep_fn=lambda s: None)
    assert job is not None and job["state"] == "waiting"
    assert src.exists(), "unstable file must stay in Inbox"


def test_temp_files_ignored(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = write_inbox(cfg, "file.crdownload", b"chunk")
    assert ingest_file(src, cfg) is None
    # Temp files create no record and no DB side-effects beyond init.
    if cfg.db_path.exists():
        conn = connect(cfg.db_path)
        try:
            assert list_jobs(conn) == []
        finally:
            conn.close()
    assert src.exists()


def test_unsupported_extension_needs_attention(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = write_inbox(cfg, "evil.exe", b"MZ...")
    job = ingest_file(src, cfg, sleep_fn=lambda s: None)
    assert job["state"] == "needs_attention"
    assert "unsupported" in job["last_error"]
    assert src.exists()


def test_oversize_needs_attention(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    cfg.max_input_file_size = 10
    src = write_inbox(cfg, "big.pdf", b"x" * 100)
    job = ingest_file(src, cfg, sleep_fn=lambda s: None)
    assert job["state"] == "needs_attention"
    assert "exceeds" in job["last_error"]
    assert src.exists()


def test_duplicate_hash_goes_to_needs_attention(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    a = write_inbox(cfg, "a.pdf", b"same-content")
    j1 = ingest_file(a, cfg, sleep_fn=lambda s: None)
    assert j1["state"] == "ready"
    b = write_inbox(cfg, "b.pdf", b"same-content")
    j2 = ingest_file(b, cfg, sleep_fn=lambda s: None)
    assert j2["state"] == "needs_attention"
    assert "duplicate" in j2["last_error"]
    assert b.exists(), "duplicate Inbox copy retained for operator"


def test_move_failure_keeps_inbox_recoverable(tmp_cfg, monkeypatch):
    cfg = make_cfg(tmp_cfg)
    src = write_inbox(cfg, "ok.pdf", b"data-123")
    import shutil
    monkeypatch.setattr(shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("disk locked")))
    job = ingest_file(src, cfg, sleep_fn=lambda s: None)
    assert job["state"] == "failed"
    assert "Inbox copy retained" in job["last_error"]
    assert src.exists()


def test_restart_recovery_requeues_interrupted(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        create_job(conn, "C:/Inbox/a.pdf", state="processing")
        create_job(conn, "C:/Inbox/b.pdf", state="waiting")
        create_job(conn, "C:/Inbox/c.pdf", state="ready")
    finally:
        conn.close()
    n = recover_on_startup(cfg)
    assert n == 2
    conn = connect(cfg.db_path)
    try:
        states = {r["inbox_path"]: r["state"] for r in list_jobs(conn)}
    finally:
        conn.close()
    assert states["C:/Inbox/a.pdf"] == "queued"
    assert states["C:/Inbox/b.pdf"] == "queued"
    assert states["C:/Inbox/c.pdf"] == "ready"


def test_retry_bounded(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        job = create_job(conn, "C:/Inbox/f.pdf", state="failed")
        for _ in range(3):
            conn.execute("UPDATE intake_jobs SET attempts = attempts + 1 WHERE id = ?", (job["id"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError, match="max attempts"):
        requeue_job(job["id"], cfg)


def test_path_traversal_rejected(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    outside = tmp_cfg["data_dir"].parent / "evil.pdf"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        ingest_file(outside, cfg)
