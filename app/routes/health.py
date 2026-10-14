from flask import Blueprint, jsonify, current_app

health_bp = Blueprint("health", __name__)


@health_bp.get("/healthz")
def healthz():
    cfg = current_app.config["FORMFLOW"]
    ok, msg = cfg.inbox_status()
    return jsonify({"status": "ok", "db": str(cfg.db_path), "inbox": msg, "inbox_ok": ok})
