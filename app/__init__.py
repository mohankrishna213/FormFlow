"""Flask application factory (Phase 2). Local-only."""
from __future__ import annotations

from flask import Flask

from .config import FormFlowConfig, load_config
from .db import init_db


def format_bytes(value) -> str:
    """Human-readable size: B below 1 KB, KB below 1 MB, MB above."""
    if value is None:
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        kb = n / 1024
        return f"{kb:.1f} KB".replace(".0 KB", " KB")
    mb = n / (1024 * 1024)
    return f"{mb:.1f} MB".replace(".0 MB", " MB")


def csrf_token() -> str:
    """Per-session token for state-changing forms (no extra dependency)."""
    import secrets

    from flask import session

    token = session.get("_csrf")
    if not token:
        token = secrets.token_hex(16)
        session["_csrf"] = token
    return token


def validate_csrf() -> None:
    from flask import abort, request, session

    submitted = request.form.get("csrf_token", "")
    if not submitted or submitted != session.get("_csrf"):
        abort(400, description="Invalid or missing form token. Reload the page and try again.")


def active_ui_mode(cfg: FormFlowConfig) -> str:
    """Return active UI profile ('production' or 'dev') from session override or config default."""
    from flask import session

    override = session.get("ui_mode")
    if override in ("production", "dev"):
        return override
    return cfg.ui_mode if cfg.ui_mode in ("production", "dev") else "production"


def create_app(config_override: dict | None = None) -> Flask:
    cfg: FormFlowConfig = load_config(config_override)
    cfg.ensure_data_dirs()
    if str(cfg.db_path) != ":memory:":
        init_db(cfg.db_path)
        # Restart recovery: requeue interrupted jobs without losing queue.
        from .services.ingest import recover_on_startup

        recover_on_startup(cfg)

    app = Flask(__name__)
    app.config["FORMFLOW"] = cfg
    app.config["SECRET_KEY"] = cfg.secret_key
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.add_template_filter(format_bytes, "filesize")

    from .routes.api import api_bp
    from .routes.cases import cases_bp
    from .routes.dashboard import dashboard_bp
    from .routes.health import health_bp
    from .routes.profiles import profiles_bp
    from .routes.queue import queue_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(cases_bp)
    app.register_blueprint(profiles_bp)
    app.register_blueprint(queue_bp)
    app.register_blueprint(api_bp)

    @app.context_processor
    def _inject_cfg():
        cfg_now = app.config["FORMFLOW"]
        ui_mode = active_ui_mode(cfg_now)
        return {
            "cfg": cfg_now,
            "csrf_token": csrf_token,
            "ui_mode": ui_mode,
            "is_dev_mode": ui_mode == "dev",
            "is_prod_mode": ui_mode == "production",
        }

    return app
