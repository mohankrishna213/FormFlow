from flask import Blueprint, jsonify, redirect, render_template, request, session

from .. import validate_csrf
from ..db import job_counts
from ..services.profiles import load_profiles
from ..services.review import (
    PER_PAGE,
    case_display_name,
    count_cases,
    needs_attention_items,
    next_action,
    search_cases,
)
from . import get_cfg, open_db

dashboard_bp = Blueprint("dashboard", __name__)

DOC_TYPE_LABELS = {
    "photo": "Passphoto",
    "signature": "Signature",
    "aadhaar": "Aadhaar",
    "ssc": "10th Memo",
    "pan": "PAN",
    "learner_licence": "LL",
    "driving_licence": "DL",
    "tr_document": "TR",
    "vehicle_rc": "TR",
    "supporting_ll": "Document",
}


def doc_type_label(
    doctype: str | None,
    profile_name: str | None = None,
    profiles: dict | None = None,
) -> str:
    key = (profile_name or doctype or "").strip().lower()
    if isinstance(profiles, dict) and key in profiles and isinstance(profiles[key], dict):
        lbl = profiles[key].get("label")
        if lbl and str(lbl).strip():
            return str(lbl).strip()
    return DOC_TYPE_LABELS.get(key, (doctype or "Document").replace("_", " ").title())


def _db():
    conn, _ = open_db()
    return conn


def _case_rows(cases, conn, profiles=None):
    rows = []
    for c in cases:
        n = conn.execute(
            "SELECT COUNT(*) n FROM extracted_fields WHERE case_id = ?", (c["id"],)
        ).fetchone()["n"]
        doc_rows = conn.execute(
            "SELECT id, doctype, profile_name, status FROM documents WHERE case_id = ? ORDER BY id",
            (c["id"],),
        ).fetchall()
        doc_badges = [
            {
                "id": d["id"],
                "label": doc_type_label(d["doctype"], d["profile_name"], profiles=profiles),
                "ready": d["status"] == "ready",
            }
            for d in doc_rows
        ]
        rows.append({
            "case": c,
            "name": case_display_name(conn, c["id"]),
            "action": next_action(c["status"], n),
            "field_count": n,
            "doc_badges": doc_badges,
        })
    return rows


def _dashboard_snapshot(conn, cfg) -> dict:
    """Minimal non-PII fingerprint for silent dashboard auto-refresh."""
    inbox_ok, _ = cfg.inbox_status()
    if conn is None:
        return {
            "inbox_ok": inbox_ok,
            "case_count": 0,
            "attention": 0,
            "processing": 0,
            "ready_docs": 0,
            "latest_sig": "",
        }
    counts = job_counts(conn)
    case_row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(MAX(updated_at), '') last_ts FROM cases"
    ).fetchone()
    doc_row = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) ready_n, "
        "COALESCE(MAX(updated_at), '') last_ts FROM documents"
    ).fetchone()
    att = conn.execute(
        "SELECT COUNT(*) n FROM cases WHERE status = 'needs_attention'"
    ).fetchone()["n"]
    processing = int(counts.get("waiting", 0)) + int(counts.get("queued", 0)) + int(counts.get("processing", 0))
    latest_sig = f"{case_row['n']}|{case_row['last_ts']}|{doc_row['n']}|{doc_row['ready_n']}|{doc_row['last_ts']}|{processing}"
    return {
        "inbox_ok": inbox_ok,
        "case_count": int(case_row["n"]),
        "attention": int(att),
        "processing": processing,
        "ready_docs": int(doc_row["ready_n"] or 0),
        "latest_sig": latest_sig,
    }


@dashboard_bp.post("/ui-mode")
def switch_ui_mode():
    """Switch active UI profile ('production' <-> 'dev') in the user's browser session."""
    validate_csrf()
    mode = (request.form.get("mode") or "").strip().lower()
    if mode in ("production", "dev"):
        session["ui_mode"] = mode
    next_url = (request.form.get("next") or "/").strip()
    # Prevent open redirects: allow only local relative paths starting with single '/'.
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    return redirect(next_url)


@dashboard_bp.get("/dashboard/status")
def dashboard_status():
    """Lightweight non-PII status snapshot for silent auto-refresh."""
    conn, cfg = open_db()
    try:
        resp = jsonify(_dashboard_snapshot(conn, cfg))
        resp.headers["Cache-Control"] = "no-store"
        return resp
    finally:
        if conn is not None:
            conn.close()


@dashboard_bp.get("/")
def index():
    conn, cfg = open_db()
    if conn is None:
        return render_template(
            "dashboard.html", rows=[], counts={}, attention=0, q="",
            snapshot=_dashboard_snapshot(None, cfg),
        )
    try:
        cases = search_cases(conn, limit=12)
        counts = job_counts(conn)
        attention = (
            conn.execute("SELECT COUNT(*) n FROM cases WHERE status = 'needs_attention'")
            .fetchone()["n"]
        )
        snapshot = _dashboard_snapshot(conn, cfg)
        active_profiles, _ = load_profiles(cfg)
        return render_template(
            "dashboard.html",
            rows=_case_rows(cases, conn, profiles=active_profiles),
            counts=counts,
            attention=attention,
            q="",
            snapshot=snapshot,
        )
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
    conn, cfg = open_db()
    if conn is None:
        return render_template("cases_list.html", rows=[], q=q, status=status,
                               page=1, pages=1, total=0)
    try:
        total = count_cases(conn, q, status)
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages)
        cases = search_cases(conn, q, status, PER_PAGE, (page - 1) * PER_PAGE)
        active_profiles, _ = load_profiles(cfg)
        return render_template("cases_list.html", rows=_case_rows(cases, conn, profiles=active_profiles),
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
