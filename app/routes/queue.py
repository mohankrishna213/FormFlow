from flask import Blueprint, current_app, redirect, render_template, request, url_for

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
    cfg = _cfg()
    try:
        requeue_job(job_id, cfg)
    except ValueError as e:
        return render_template("queue_error.html", message=str(e)), 400
    return redirect(url_for("queue.queue_list"))


@queue_bp.post("/queue/documents/<int:doc_id>/retry")
def document_retry(doc_id: int):
    cfg = _cfg()
    try:
        from ..services.prepare import retry_document

        retry_document(doc_id, cfg)
    except ValueError as e:
        return render_template("queue_error.html", message=str(e)), 400
    return redirect(url_for("queue.queue_list"))
