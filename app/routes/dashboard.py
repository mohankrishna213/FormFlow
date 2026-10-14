from flask import Blueprint, render_template

from ..db import connect, job_counts, list_cases

dashboard_bp = Blueprint("dashboard", __name__)


def _cases():
    from flask import current_app

    cfg = current_app.config["FORMFLOW"]
    if str(cfg.db_path) == ":memory:":
        return []
    conn = connect(cfg.db_path)
    try:
        return list_cases(conn)
    finally:
        conn.close()


def _counts():
    from flask import current_app

    cfg = current_app.config["FORMFLOW"]
    if str(cfg.db_path) == ":memory:":
        return {}
    conn = connect(cfg.db_path)
    try:
        return job_counts(conn)
    finally:
        conn.close()


@dashboard_bp.get("/")
def index():
    return render_template("dashboard.html", cases=_cases(), counts=_counts())


@dashboard_bp.get("/cases")
def cases_list():
    # Placeholder route; full queue/search lands in Phase 5.
    return render_template("cases_list.html", cases=_cases())
