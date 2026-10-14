"""Local-only runner. Binds 127.0.0.1; refuses public binding."""
from app import create_app

app = create_app()

if __name__ == "__main__":
    cfg = app.config["FORMFLOW"]
    assert cfg.host == "127.0.0.1", f"refusing non-local bind: {cfg.host}"
    ok, msg = cfg.inbox_status()
    if not ok:
        print(f"WARNING: {msg}")
    print(f"FormFlow on http://127.0.0.1:{cfg.port}  db={cfg.db_path}")
    app.run(host="127.0.0.1", port=cfg.port, debug=False)
