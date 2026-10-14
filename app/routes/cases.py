from flask import Blueprint, abort, render_template

from ..db import connect

cases_bp = Blueprint("cases_bp", __name__)


@cases_bp.get("/cases/<case_id>")
def case_detail(case_id: str):
    from flask import current_app

    cfg = current_app.config["FORMFLOW"]
    if str(cfg.db_path) == ":memory:":
        abort(404)
    conn = connect(cfg.db_path)
    try:
        row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            abort(404)
        docs = conn.execute("SELECT * FROM documents WHERE case_id = ?", (row["id"],)).fetchall()
        fields = conn.execute(
            "SELECT * FROM extracted_fields WHERE case_id = ?", (row["id"],)
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM case_events WHERE case_id = ? ORDER BY created_at DESC LIMIT 50",
            (row["id"],),
        ).fetchall()
    finally:
        conn.close()
    # Full editable review form lands in Phase 5; this is a read-only placeholder.
    return render_template("case_detail.html", case=row, docs=docs, fields=fields, events=events)


@cases_bp.get("/documents")
def documents_placeholder():
    # Real document pipeline lands in Phases 2-4. Placeholder only.
    return render_template("documents.html")
