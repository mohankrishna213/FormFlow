"""Flask application factory (Phase 2). Local-only."""
from __future__ import annotations

from flask import Flask

from .config import FormFlowConfig, load_config
from .db import connect, init_db


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

    from .routes.cases import cases_bp
    from .routes.dashboard import dashboard_bp
    from .routes.health import health_bp
    from .routes.queue import queue_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(cases_bp)
    app.register_blueprint(queue_bp)

    @app.context_processor
    def _inject_cfg():
        return {"cfg": app.config["FORMFLOW"]}

    return app


def get_db():
    from flask import current_app, g

    if "db" not in g:
        cfg: FormFlowConfig = current_app.config["FORMFLOW"]
        # :memory: does not survive across connections; only used with
        # explicit test wiring, not the running server.
        g.db = connect(cfg.db_path)
    return g.db


def close_db(exc=None):
    from flask import g

    db = g.pop("db", None)
    if db is not None:
        db.close()


def wire_teardown(app: Flask) -> Flask:
    app.teardown_appcontext(close_db)
    return app
