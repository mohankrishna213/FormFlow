from flask import Blueprint, current_app, redirect, render_template, request, url_for

from .. import validate_csrf
from ..db import connect, get_job, list_documents, list_jobs
from ..services.ingest import requeue_job

queue_bp = Blueprint("queue", __name__)


def _cfg():
    return current_app.config["FORMFLOW"]


@queue_bp.get("/queue")
def queue_list():
    cfg = _cfg()
    conn = connect(cfg.db_path)
    try:
        jobs = list_jobs(conn)
        docs = list_documents(conn)
    finally:
        conn.close()
    return render_template("queue.html", jobs=jobs, docs=docs)


@queue_bp.post("/queue/<int:job_id>/retry")
def queue_retry(job_id: int):
    validate_csrf()
    cfg = _cfg()
    try:
        requeue_job(job_id, cfg)
    except ValueError as e:
        return render_template("queue_error.html", message=str(e)), 400
    return redirect(url_for("queue.queue_list"))


@queue_bp.post("/queue/documents/<int:doc_id>/retry")
def document_retry(doc_id: int):
    validate_csrf()
    cfg = _cfg()
    try:
        from ..services.prepare import retry_document

        retry_document(doc_id, cfg)
    except ValueError as e:
        return render_template("queue_error.html", message=str(e)), 400
    return redirect(url_for("queue.queue_list"))


@queue_bp.get("/queue/reset")
def reset_confirm():
    from flask import abort

    cfg = _cfg()
    if not cfg.allow_reset:
        abort(404)
    conn = connect(cfg.db_path)
    try:
        counts = {t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                  for t in ("cases", "documents", "intake_jobs", "extracted_fields")}
    finally:
        conn.close()
    return render_template("reset_confirm.html", counts=counts)


@queue_bp.post("/queue/reset")
def reset_execute():
    from flask import abort

    validate_csrf()
    cfg = _cfg()
    if not cfg.allow_reset:
        abort(404)
    from ..services.reset import reset_all

    reset_all(cfg)
    return redirect(url_for("queue.queue_list"))
