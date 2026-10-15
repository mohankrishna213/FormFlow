"""Worker + watcher behaviour and queue routes (deterministic, no real OCR)."""
import time
from pathlib import Path

import pytest

from app import create_app
from app.config import load_config
from app.db import connect, create_job, init_db, list_jobs
from app.services.jobs import Worker
from app.services.watcher import InboxWatcher


def make_cfg(tmp_cfg, **kw):
    d = dict(tmp_cfg)
    d.update({"stable_checks": 2, "stable_interval_sec": 0.0,
              "debounce_sec": 0.05, "max_attempts": 5})
    d.update(kw)
    cfg = load_config(d)
    cfg.ensure_data_dirs()
    return cfg


def test_worker_submit_coalesces_duplicates(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    w = Worker(cfg)
    p = cfg.inbox_dir / "a.pdf"
    p.write_bytes(b"data")
    assert w.submit(p) is True
    assert w.pending == 1
    # Second submit while still pending is coalesced.
    assert w.submit(p) is False
    assert w.pending == 1


def test_worker_process_now_end_to_end(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    w = Worker(cfg)
    p = cfg.inbox_dir / "doc.pdf"
    p.write_bytes(b"hello-worker")
    job = w.process_now(p)
    assert job["state"] == "ready"
    assert not p.exists()


def test_watcher_ignores_temp_and_outside_files(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    w = Worker(cfg)
    watcher = InboxWatcher(cfg, w)
    h = watcher.handler
    temp = cfg.inbox_dir / "x.crdownload"
    temp.write_bytes(b"part")
    h._schedule(str(temp))
    outside = cfg.data_dir / "staging" / "y.pdf"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"y")
    h._schedule(str(outside))
    time.sleep(0.2)
    assert w.pending == 0


def test_watcher_debounces_burst_to_one_delivery(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    w = Worker(cfg)
    watcher = InboxWatcher(cfg, w)
    h = watcher.handler
    p = cfg.inbox_dir / "burst.pdf"
    p.write_bytes(b"burst-bytes")
    for _ in range(5):
        h._schedule(str(p))
    time.sleep(0.25)
    assert w.pending == 1


def test_scan_existing_counts_only_real_files(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    (cfg.inbox_dir / "a.pdf").write_bytes(b"a")
    (cfg.inbox_dir / "b.jpg").write_bytes(b"b")
    (cfg.inbox_dir / "c.tmp").write_bytes(b"c")
    w = Worker(cfg)
    watcher = InboxWatcher(cfg, w)
    assert watcher.scan_existing() == 2
    assert w.pending == 2


def test_watcher_start_error_helpful(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    cfg.inbox_dir = cfg.inbox_dir / "DoesNotExist"
    w = Worker(cfg)
    watcher = InboxWatcher(cfg, w)
    with pytest.raises(RuntimeError, match="Inbox not found"):
        watcher.start()


def test_queue_routes_and_retry(tmp_cfg):
    import re

    app = create_app(dict(tmp_cfg))
    client = app.test_client()
    assert client.get("/queue").status_code == 200
    assert client.post("/queue/9999/retry").status_code == 400  # no token
    cfg = app.config["FORMFLOW"]
    conn = connect(cfg.db_path)
    try:
        job = create_job(conn, "C:/Inbox/r.pdf", state="failed")
        jid = job["id"]
    finally:
        conn.close()
    html = client.get("/queue").data.decode()
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    r = client.post(f"/queue/{jid}/retry", data={"csrf_token": token})
    assert r.status_code in (302, 303)
    conn = connect(cfg.db_path)
    try:
        row = conn.execute("SELECT * FROM intake_jobs WHERE id = ?", (jid,)).fetchone()
        assert row["state"] == "queued"
    finally:
        conn.close()
    # Retry on ready job -> 400, not silent.
    conn = connect(cfg.db_path)
    try:
        ok = create_job(conn, "C:/Inbox/ok.pdf", state="ready")
    finally:
        conn.close()
    assert client.post(f"/queue/{ok['id']}/retry", data={"csrf_token": token}).status_code == 400


def test_dashboard_links_queue(tmp_cfg):
    app = create_app(dict(tmp_cfg))
    assert b"/queue" in app.test_client().get("/").data


def test_waiting_job_completes_on_recheck(tmp_cfg):
    """Regression: a file seen while growing must complete on re-ingest,
    not stay stuck in 'waiting' via a stale early-return."""
    from app.services.ingest import ingest_file

    cfg = make_cfg(tmp_cfg)
    p = cfg.inbox_dir / "slow.pdf"
    p.write_bytes(b"partial")
    sizes = [(7, 1), (999, 2)]
    it = iter(sizes)
    j1 = ingest_file(p, cfg, stat_fn=lambda _: next(it), sleep_fn=lambda s: None)
    assert j1["state"] == "waiting"
    assert p.exists()
    p.write_bytes(b"partial-plus-rest-of-bytes")
    j2 = ingest_file(p, cfg, sleep_fn=lambda s: None)
    assert j2["state"] == "ready"
    assert j2["id"] == j1["id"], "same job row progresses waiting -> ready"
    assert not p.exists()
