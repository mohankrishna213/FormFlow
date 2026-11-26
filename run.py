"""Local-only production runner. Binds strictly to 127.0.0.1."""
from __future__ import annotations

from app import create_app

app = create_app()


def start_background(flask_app=None):
    """Start worker + Inbox watcher. Auto-creates Inbox\\ if missing. Returns (worker, watcher)."""
    from app.services.jobs import Worker
    from app.services.watcher import InboxWatcher

    target_app = flask_app or app
    cfg = target_app.config["FORMFLOW"]
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    worker = Worker(cfg).start()
    watcher = InboxWatcher(cfg, worker)
    watcher.start()
    watcher.scan_existing()
    return worker, watcher


def create_http_server(flask_app=None):
    """Build a local-only Waitress WSGI server bound strictly to 127.0.0.1."""
    target_app = flask_app or app
    cfg = target_app.config["FORMFLOW"]
    assert cfg.host == "127.0.0.1", f"refusing non-local bind: {cfg.host}"
    try:
        from waitress.server import create_server

        return create_server(target_app, host="127.0.0.1", port=cfg.port, threads=6)
    except ImportError:
        return None


if __name__ == "__main__":
    cfg = app.config["FORMFLOW"]
    assert cfg.host == "127.0.0.1", f"refusing non-local bind: {cfg.host}"
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    ok, msg = cfg.inbox_status()
    worker, watcher = None, None
    if not ok:
        print(f"WARNING: {msg}")
        print("Web UI will start, but the watcher is disabled until Inbox exists.")
    else:
        try:
            worker, watcher = start_background(app)
            print(f"Watcher on {cfg.inbox_dir} (non-recursive, Inbox only).")
        except RuntimeError as e:
            print(f"WARNING: watcher disabled: {e}")
    print(f"FormFlow on http://127.0.0.1:{cfg.port}  db={cfg.db_path}")
    server = create_http_server(app)
    try:
        if server is not None:
            server.run()
        else:
            app.run(host="127.0.0.1", port=cfg.port, debug=False)
    finally:
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        if watcher is not None:
            watcher.stop()
        if worker is not None:
            worker.stop()
