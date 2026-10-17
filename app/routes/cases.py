from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .. import validate_csrf
from ..db import connect
from ..services.fill_packet import (
    DEFAULT_WORKFLOW,
    WORKFLOWS,
    PacketError,
    build_fill_packet,
)
from ..services.filters import resolve_within, sanitize_filename
from ..services.review import (
    FIELD_LABELS,
    KEEP_BLANK,
    REVIEW_FIELDS,
    assign_document,
    best_and_candidates,
    case_display_name,
    collect_form_values,
    conflict_status,
    form_choices,
    key_conflicts,
    merge_cases,
    review_prefill,
    save_review,
    search_cases,
    with_sources,
)

cases_bp = Blueprint("cases_bp", __name__)


def _db():
    cfg = current_app.config["FORMFLOW"]
    if str(cfg.db_path) == ":memory:":
        return None, None
    return connect(cfg.db_path), cfg


def _case_or_404(conn, case_id: str):
    row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@cases_bp.get("/cases/<case_id>")
def case_detail(case_id: str):
    conn, _ = _db()
    if conn is None:
        abort(404)
    try:
        case = _case_or_404(conn, case_id)
        docs = conn.execute(
            "SELECT * FROM documents WHERE case_id = ? ORDER BY id", (case["id"],)
        ).fetchall()
        info = best_and_candidates(conn, case["id"])
        conflicts = key_conflicts(conn, case["id"])
        info, conflicts = with_sources(conn, info, conflicts)
        events = conn.execute(
            "SELECT * FROM case_events WHERE case_id = ? ORDER BY id DESC LIMIT 50",
            (case["id"],),
        ).fetchall()
        unassigned = conn.execute(
            "SELECT * FROM documents WHERE case_id IS NULL ORDER BY id DESC LIMIT 50"
        ).fetchall()
        others = [c for c in search_cases(conn, limit=200) if c["case_id"] != case_id]
        return render_template(
            "case_detail.html", case=case, name=case_display_name(conn, case["id"]),
            docs=docs, info=info, conflicts=conflicts, events=events,
            unassigned=unassigned, others=others, labels=FIELD_LABELS)
    finally:
        conn.close()


@cases_bp.get("/cases/<case_id>/review")
def review_form(case_id: str):
    conn, _ = _db()
    if conn is None:
        abort(404)
    try:
        case = _case_or_404(conn, case_id)
        info = best_and_candidates(conn, case["id"])
        conflicts = key_conflicts(conn, case["id"])
        info, conflicts = with_sources(conn, info, conflicts)
        return render_template("review.html", case=case,
                               name=case_display_name(conn, case["id"]),
                               info=info, conflicts=conflicts, errors={},
                               prefill=review_prefill(info, case),
                               choices={},
                               confirmed_keys=set(),
                               status=conflict_status(conn, case, info, conflicts),
                               blank_pick=KEEP_BLANK,
                               labels=FIELD_LABELS)
    finally:
        conn.close()


@cases_bp.post("/cases/<case_id>/review")
def review_save(case_id: str):
    validate_csrf()
    conn, _ = _db()
    if conn is None:
        abort(404)
    try:
        values = collect_form_values(request.form)
        try:
            case, changed, errors = save_review(conn, case_id, values)
        except ValueError:
            abort(404)
        if errors:
            info = best_and_candidates(conn, case["id"])
            conflicts = key_conflicts(conn, case["id"])
            info, conflicts = with_sources(conn, info, conflicts)
            return render_template("review.html", case=case,
                                   name=case_display_name(conn, case["id"]),
                                   info=info, conflicts=conflicts,
                                   errors=errors,
                                   prefill=review_prefill(info, case, values),
                                   choices=form_choices(values),
                                   confirmed_keys={k for k in REVIEW_FIELDS if (values.get(f"confirm_{k}") or "").strip().lower() in ("1", "true", "yes", "on")},
                                   status=conflict_status(conn, case, info, conflicts),
                                   blank_pick=KEEP_BLANK,
                                   labels=FIELD_LABELS)
        return redirect(url_for("cases_bp.case_detail", case_id=case["case_id"]))
    finally:
        conn.close()


@cases_bp.post("/cases/<case_id>/assign-document")
def document_assign(case_id: str):
    validate_csrf()
    conn, _ = _db()
    if conn is None:
        abort(404)
    try:
        try:
            doc_id = int(request.form.get("document_id", "0"))
        except ValueError:
            abort(400)
        try:
            assign_document(conn, doc_id, case_id)
        except ValueError:
            abort(404)
        return redirect(url_for("cases_bp.case_detail", case_id=case_id))
    finally:
        conn.close()


@cases_bp.get("/cases/<case_id>/merge")
def merge_confirm(case_id: str):
    source = request.args.get("source", "")
    conn, _ = _db()
    if conn is None:
        abort(404)
    try:
        target = _case_or_404(conn, case_id)
        if not source:
            abort(400)
        src = _case_or_404(conn, source)
        src_docs = conn.execute(
            "SELECT * FROM documents WHERE case_id = ?", (src["id"],)
        ).fetchall()
        return render_template("merge_confirm.html", target=target, source=src,
                               src_docs=src_docs)
    finally:
        conn.close()


@cases_bp.post("/cases/<case_id>/merge")
def merge_execute(case_id: str):
    validate_csrf()
    conn, _ = _db()
    if conn is None:
        abort(404)
    try:
        source = request.form.get("source", "")
        if not source:
            abort(400)
        try:
            merge_cases(conn, source, case_id)
        except ValueError as e:
            return render_template("queue_error.html", message=str(e)), 400
        return redirect(url_for("cases_bp.case_detail", case_id=case_id))
    finally:
        conn.close()


@cases_bp.get("/documents/<int:doc_id>/<kind>")
def serve_file(doc_id: int, kind: str):
    if kind not in ("original", "prepared"):
        abort(404)
    conn, cfg = _db()
    if conn is None:
        abort(404)
    try:
        from ..db import get_document

        doc = get_document(conn, doc_id)
        if doc is None:
            abort(404)
        col = "orig_path" if kind == "original" else "prepared_path"
        keys = set(doc.keys())
        raw = doc[col] if col in keys else None
        if not raw:
            abort(404)
        try:
            path = resolve_within(Path(raw), cfg.data_dir)
        except ValueError:
            abort(404)
        if not path.is_file():
            abort(404)
        download = request.args.get("download") == "1"
        return send_file(
            path, mimetype=doc["mime"] or "application/octet-stream",
            as_attachment=download,
            download_name=sanitize_filename(doc["orig_filename"] or path.name),
        )
    finally:
        conn.close()


@cases_bp.get("/cases/<case_id>/fill-ready")
def fill_ready(case_id: str):
    """Ready to fill view with per-field Copy buttons."""
    conn, _ = _db()
    if conn is None:
        abort(404)
    try:
        case = _case_or_404(conn, case_id)
        workflow = request.args.get("workflow", DEFAULT_WORKFLOW)
        result = build_fill_packet(conn, case_id, workflow)
        if isinstance(result, PacketError):
            wf = WORKFLOWS.get(workflow, WORKFLOWS[DEFAULT_WORKFLOW])
            return render_template(
                "fill_ready.html", case=case, fields=[],
                workflow_label=wf["label"], error=result.reason)
        return render_template(
            "fill_ready.html", case=case, fields=result["fields"],
            workflow_label=result["workflow_label"], error=None)
    finally:
        conn.close()


@cases_bp.get("/documents")
def documents_placeholder():
    return redirect("/queue")
