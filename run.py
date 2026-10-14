"""Local-only runner. Binds 127.0.0.1; refuses public binding."""
from app import create_app

app = create_app()


def start_background():
    """Start worker + Inbox watcher (Phase 2). Returns (worker, watcher)."""
    from app.services.jobs import Worker
    from app.services.watcher import InboxWatcher

    cfg = app.config["FORMFLOW"]
    worker = Worker(cfg).start()
    watcher = InboxWatcher(cfg, worker)
    watcher.start()
    watcher.scan_existing()
    return worker, watcher


if __name__ == "__main__":
    cfg = app.config["FORMFLOW"]
    assert cfg.host == "127.0.0.1", f"refusing non-local bind: {cfg.host}"
    ok, msg = cfg.inbox_status()
    worker, watcher = None, None
    if not ok:
        print(f"WARNING: {msg}")
        print("Web UI will start, but the watcher is disabled until Inbox exists.")
    else:
        try:
            worker, watcher = start_background()
            print(f"Watcher on {cfg.inbox_dir} (non-recursive, Inbox only).")
        except RuntimeError as e:
            print(f"WARNING: watcher disabled: {e}")
    print(f"FormFlow on http://127.0.0.1:{cfg.port}  db={cfg.db_path}")
    try:
        app.run(host="127.0.0.1", port=cfg.port, debug=False)
    finally:
        if watcher is not None:
            watcher.stop()
        if worker is not None:
            worker.stop()
