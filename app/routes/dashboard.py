from flask import Blueprint, current_app, render_template, request

from ..db import connect, job_counts
from ..services.review import (
    PER_PAGE,
    case_display_name,
    count_cases,
    needs_attention_items,
    next_action,
    search_cases,
)

dashboard_bp = Blueprint("dashboard", __name__)


def _db():
    cfg = current_app.config["FORMFLOW"]
    if str(cfg.db_path) == ":memory:":
        return None
    return connect(cfg.db_path)


def _case_rows(cases, conn):
    rows = []
    for c in cases:
        n = conn.execute(
            "SELECT COUNT(*) n FROM extracted_fields WHERE case_id = ?", (c["id"],)
        ).fetchone()["n"]
        rows.append({"case": c, "name": case_display_name(conn, c["id"]),
                     "action": next_action(c["status"], n)})
    return rows


@dashboard_bp.get("/")
def index():
    conn = _db()
    if conn is None:
        return render_template("dashboard.html", rows=[], counts={}, attention=0, q="")
    try:
        cases = search_cases(conn, limit=10)
        counts = job_counts(conn)
        attention = (
            conn.execute("SELECT COUNT(*) n FROM cases WHERE status = 'needs_attention'")
            .fetchone()["n"]
        )
        return render_template("dashboard.html", rows=_case_rows(cases, conn),
                               counts=counts, attention=attention, q="")
    finally:
        conn.close()


@dashboard_bp.get("/cases")
def cases_list():
    q = request.args.get("q", "")
    status = request.args.get("status", "")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    conn = _db()
    if conn is None:
        return render_template("cases_list.html", rows=[], q=q, status=status,
                               page=1, pages=1, total=0)
    try:
        total = count_cases(conn, q, status)
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages)
        cases = search_cases(conn, q, status, PER_PAGE, (page - 1) * PER_PAGE)
        return render_template("cases_list.html", rows=_case_rows(cases, conn),
                               q=q, status=status, page=page, pages=pages, total=total)
    finally:
        conn.close()


@dashboard_bp.get("/needs-attention")
def needs_attention():
    conn = _db()
    if conn is None:
        return render_template("needs_attention.html",
                               cases=[], jobs=[], docs=[], unassigned=[])
    try:
        items = needs_attention_items(conn)
        return render_template(
            "needs_attention.html", cases=items["cases"], jobs=items["jobs"],
            docs=items["docs"], unassigned=items["unassigned"])
    finally:
        conn.close()
