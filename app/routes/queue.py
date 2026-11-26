from flask import Blueprint, abort, redirect, render_template, url_for

from .. import validate_csrf
from ..db import connect, list_documents, list_jobs
from ..services.ingest import requeue_job
from . import get_cfg

queue_bp = Blueprint("queue", __name__)


@queue_bp.get("/queue")
def queue_list():
    cfg = get_cfg()
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
    cfg = get_cfg()
    try:
        requeue_job(job_id, cfg)
    except ValueError as e:
        return render_template("queue_error.html", message=str(e)), 400
    return redirect(url_for("queue.queue_list"))


@queue_bp.post("/queue/documents/<int:doc_id>/retry")
def document_retry(doc_id: int):
    validate_csrf()
    cfg = get_cfg()
    try:
        from ..services.prepare import retry_document

        retry_document(doc_id, cfg)
    except ValueError as e:
        return render_template("queue_error.html", message=str(e)), 400
    return redirect(url_for("queue.queue_list"))


@queue_bp.get("/queue/reset")
def reset_confirm():
    cfg = get_cfg()
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
    validate_csrf()
    cfg = get_cfg()
    if not cfg.allow_reset:
        abort(404)
    from ..services.reset import reset_all

    reset_all(cfg)
    return redirect(url_for("queue.queue_list"))
