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
from . import open_db
from .dashboard import doc_type_label
from ..services.fill_packet import (
    DEFAULT_WORKFLOW,
    WORKFLOWS,
    PacketError,
    build_fill_packet,
)
from ..services.filters import resolve_within, sanitize_filename
from ..services.prepare import (
    SUPPORTED_IMAGE_EXTS,
    mime_for,
    parse_prep_meta,
    reprepare_document,
    sync_inbox_export,
)
from ..services.profiles import load_profiles
from ..services.review import (
    FIELD_LABELS,
    FORM_KEY,
    KEEP_BLANK,
    REQUIRED_FIELDS,
    REVIEW_FIELDS,
    assign_document,
    best_and_candidates,
    case_display_name,
    categorized_fields,
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
    conn, cfg = open_db()
    return (conn, cfg) if conn is not None else (None, None)


def _case_or_404(conn, case_id: str):
    row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@cases_bp.get("/cases/<case_id>")
def case_detail(case_id: str):
    conn, cfg = _db()
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
        doc_meta = {}
        doc_is_image = {}
        doc_prep_is_image = {}
        doc_inbox_name = {}
        doc_labels = {}
        active_profiles, _ = load_profiles(cfg)
        for d in docs:
            dkeys = set(d.keys())
            doc_meta[d["id"]] = parse_prep_meta(d["prep_meta"] if "prep_meta" in dkeys else None)
            doc_is_image[d["id"]] = Path(d["orig_path"] or "").suffix.lower() in SUPPORTED_IMAGE_EXTS
            doc_prep_is_image[d["id"]] = Path(d["prepared_path"] or "").suffix.lower() in SUPPORTED_IMAGE_EXTS
            exp = d["inbox_export_path"] if "inbox_export_path" in dkeys else None
            doc_inbox_name[d["id"]] = Path(exp).name if exp else None
            doc_labels[d["id"]] = doc_type_label(d["doctype"], d["profile_name"], profiles=active_profiles)
        packet = build_fill_packet(
            conn,
            case_id,
            "auto",
            auto_ready=bool(getattr(cfg, "auto_ready_fill", True)),
            min_confidence=float(getattr(cfg, "auto_fill_min_confidence", 0.70)),
        )
        copy_fields = packet["fields"] if not isinstance(packet, PacketError) else []
        return render_template(
            "case_detail.html",
            case=case,
            name=case_display_name(conn, case["id"]),
            docs=docs,
            doc_meta=doc_meta,
            doc_is_image=doc_is_image,
            doc_prep_is_image=doc_prep_is_image,
            doc_inbox_name=doc_inbox_name,
            doc_labels=doc_labels,
            info=info,
            copy_fields=copy_fields,
            conflicts=conflicts,
            events=events,
            unassigned=unassigned,
            others=others,
            labels=FIELD_LABELS,
        )
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
                               labels=FIELD_LABELS,
                               categories=categorized_fields(info),
                               form_keys=FORM_KEY,
                               required_fields=REQUIRED_FIELDS)
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
            confirmed = {k[len("confirm_"):] for k, v in values.items() if k.startswith("confirm_") and (v or "").strip().lower() in ("1", "true", "yes", "on")}
            confirmed.update({k for k in REVIEW_FIELDS if (values.get(f"confirm_{k}") or "").strip().lower() in ("1", "true", "yes", "on")})
            return render_template("review.html", case=case,
                                   name=case_display_name(conn, case["id"]),
                                   info=info, conflicts=conflicts,
                                   errors=errors,
                                   prefill=review_prefill(info, case, values),
                                   choices=form_choices(values),
                                   confirmed_keys=confirmed,
                                   status=conflict_status(conn, case, info, conflicts),
                                   blank_pick=KEEP_BLANK,
                                   labels=FIELD_LABELS,
                                   categories=categorized_fields(info),
                                   form_keys=FORM_KEY,
                                   required_fields=REQUIRED_FIELDS)
        return redirect(url_for("cases_bp.case_detail", case_id=case["case_id"]))
    finally:
        conn.close()


@cases_bp.post("/cases/<case_id>/assign-document")
def document_assign(case_id: str):
    validate_csrf()
    conn, cfg = _db()
    if conn is None:
        abort(404)
    try:
        try:
            doc_id = int(request.form.get("document_id", "0"))
        except ValueError:
            abort(400)
        try:
            assign_document(conn, doc_id, case_id)
            sync_inbox_export(conn, doc_id, cfg)
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
        served_mime = mime_for(path.suffix, prepared=(kind == "prepared"))
        if served_mime == "application/octet-stream" and doc["mime"]:
            served_mime = doc["mime"]
        dl_name = path.name if kind == "prepared" else (doc["orig_filename"] or path.name)
        resp = send_file(
            path, mimetype=served_mime,
            as_attachment=download,
            download_name=sanitize_filename(dl_name),
        )
        if kind == "prepared":
            resp.headers["Cache-Control"] = "no-store"
        return resp
    finally:
        conn.close()


@cases_bp.get("/documents/<int:doc_id>/crop")
def document_crop_view(doc_id: int):
    conn, cfg = _db()
    if conn is None:
        abort(404)
    try:
        from ..db import get_document

        doc = get_document(conn, doc_id)
        if doc is None:
            abort(404)
        ext = Path(doc["orig_path"] or "").suffix.lower()
        is_image = ext in SUPPORTED_IMAGE_EXTS
        profiles, _ = load_profiles(cfg)
        case_code = None
        if doc["case_id"]:
            crow = conn.execute("SELECT case_id FROM cases WHERE id = ?", (doc["case_id"],)).fetchone()
            if crow:
                case_code = crow["case_id"]
        dkeys = set(doc.keys())
        meta = parse_prep_meta(doc["prep_meta"] if "prep_meta" in dkeys else None)
        exp_raw = doc["inbox_export_path"] if "inbox_export_path" in dkeys else None
        inbox_export_name = Path(exp_raw).name if exp_raw else None
        saved = request.args.get("saved") == "1"
        return render_template(
            "document_crop.html",
            doc=doc,
            meta=meta,
            inbox_export_name=inbox_export_name,
            is_image=is_image,
            profiles=profiles,
            case_code=case_code,
            saved=saved,
            error_msg=None,
        )
    finally:
        conn.close()


@cases_bp.post("/documents/<int:doc_id>/crop")
def document_crop_apply(doc_id: int):
    validate_csrf()
    conn, cfg = _db()
    if conn is None:
        abort(404)
    try:
        from ..db import get_document

        doc = get_document(conn, doc_id)
        if doc is None:
            abort(404)
        ext = Path(doc["orig_path"] or "").suffix.lower()
        if ext not in SUPPORTED_IMAGE_EXTS:
            return render_template(
                "queue_error.html",
                message="Interactive cropping is only available for JPG and PNG images.",
            ), 400
    finally:
        conn.close()

    profiles, _ = load_profiles(cfg)
    doctype = (request.form.get("doctype") or "").strip().lower()
    if doctype not in profiles or doctype == "unknown":
        doctype = "supporting_ll"
    mode = (request.form.get("mode") or "manual").strip().lower()

    try:
        if mode == "auto":
            reprepare_document(doc_id, cfg, doctype_hint=doctype, auto_crop=True)
        else:
            def _f(name: str, default: float) -> float:
                try:
                    return float(request.form.get(name, default))
                except (TypeError, ValueError):
                    return default

            cx = _f("crop_x", 0.0)
            cy = _f("crop_y", 0.0)
            cw = _f("crop_w", 1.0)
            ch = _f("crop_h", 1.0)
            try:
                rot = int(request.form.get("rotate_deg", "0"))
            except ValueError:
                rot = 0
            clean_bg = (request.form.get("clean_bg") or "").strip().lower() in ("1", "true", "yes", "on")
            reprepare_document(
                doc_id,
                cfg,
                doctype_hint=doctype,
                manual_crop=(cx, cy, cw, ch),
                rotate_deg=rot,
                clean_signature_bg=clean_bg,
            )
    except ValueError as e:
        return render_template("queue_error.html", message=str(e)), 400

    return redirect(url_for("cases_bp.document_crop_view", doc_id=doc_id, saved=1))


@cases_bp.get("/cases/<case_id>/fill-ready")
def fill_ready(case_id: str):
    """Ready to fill view with per-field Copy buttons."""
    conn, cfg = _db()
    if conn is None:
        abort(404)
    try:
        case = _case_or_404(conn, case_id)
        auto_ready = bool(getattr(cfg, "auto_ready_fill", True))
        min_conf = float(getattr(cfg, "auto_fill_min_confidence", 0.70))
        default_wf = "auto" if auto_ready else DEFAULT_WORKFLOW
        workflow = request.args.get("workflow", default_wf)
        result = build_fill_packet(
            conn, case_id, workflow,
            auto_ready=auto_ready, min_confidence=min_conf,
        )
        if isinstance(result, PacketError):
            wf = WORKFLOWS.get(workflow, WORKFLOWS[DEFAULT_WORKFLOW])
            return render_template(
                "fill_ready.html", case=case, fields=[],
                workflow_label=wf["label"], error=result.reason,
                auto_ready=auto_ready, auto_resolved_conflicts=[])
        return render_template(
            "fill_ready.html", case=case, fields=result["fields"],
            workflow_label=result["workflow_label"], error=None,
            auto_ready=bool(result.get("auto_ready", False)),
            auto_resolved_conflicts=result.get("auto_resolved_conflicts", []))
    finally:
        conn.close()


@cases_bp.get("/documents")
def documents_placeholder():
    return redirect("/queue")
