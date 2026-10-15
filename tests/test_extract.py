"""Phase 4 pipeline: parse -> match/provisional -> traceable draft fields."""
import json
from pathlib import Path

from app.config import load_config
from app.db import (
    connect,
    create_extracted_field,
    init_db,
    latest_field_value,
    list_case_fields,
)
from app.services.extract import run_extraction
from app.services.fields import extract_candidates, load_field_rules, parse_date_token
from app.services.match import detect_conflicts
from app.services.prepare import prepare_document


def make_cfg(tmp_cfg, **kw):
    d = dict(tmp_cfg)
    d.update({"stable_checks": 2, "stable_interval_sec": 0.0})
    d.update(kw)
    cfg = load_config(d)
    cfg.ensure_data_dirs()
    return cfg


def stage_text_pdf(cfg, name: str, lines: list[str]) -> Path:
    import pymupdf

    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    src = staging / name
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for ln in lines:
        page.insert_text((72, y), ln)
        y += 20
    doc.save(src)
    doc.close()
    return src


DOC_LINES = [
    "Name: Ravi Kumar",
    "Date of Birth: 17-08-1995",
    "Address: 12 MG Road",
    "Bengaluru 560001",
]


def prepare_supporting(cfg, src: Path, **kw):
    return prepare_document(src, cfg, doctype_hint="supporting_ll", **kw)


def fields_by_key(conn, case_pk):
    return {r["field_key"]: dict(r) for r in list_case_fields(conn, case_pk)}


def test_text_pdf_provisional_with_traceable_fields(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage_text_pdf(cfg, "doc1.pdf", DOC_LINES)
    doc = prepare_supporting(cfg, src)
    out = run_extraction(doc["id"], cfg)
    assert out["ocr_status"] == "needs_attention"  # provisional, needs assignment
    assert "provisional" in (out["ocr_error"] or "")
    assert out["case_id"] is not None
    conn = connect(cfg.db_path)
    try:
        rows = list_case_fields(conn, out["case_id"])
        by_key = {r["field_key"]: r for r in rows}
        assert by_key["full_name"]["value"] == "Ravi Kumar"
        assert by_key["dob"]["value"] == "1995-08-17"
        assert "MG Road" in by_key["address"]["value"]
        for r in rows:
            assert r["source_page"] == 1
            assert r["method"].startswith("text:")
            assert r["document_id"] == doc["id"]
            assert "verif" not in set(r.keys()) and "valid" not in "".join(r.keys())
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (out["case_id"],)).fetchone()
        assert case["status"] == "needs_attention"
        assert case["customer_id"] is None
        assert case["appl_number"] is None
    finally:
        conn.close()
    sidecar = cfg.data_dir / "ocr" / f"doc_{doc['id']}.json"
    assert sidecar.exists()
    assert "Ravi Kumar" in sidecar.read_text(encoding="utf-8")


def test_second_doc_with_same_identity_attaches(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    d1 = prepare_supporting(cfg, stage_text_pdf(cfg, "a.pdf", DOC_LINES))
    c1 = run_extraction(d1["id"], cfg)["case_id"]
    d2 = prepare_supporting(cfg, stage_text_pdf(cfg, "b.pdf", DOC_LINES))
    out2 = run_extraction(d2["id"], cfg)
    assert out2["ocr_status"] == "text"  # attached, no attention needed
    assert out2["case_id"] == c1
    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM cases").fetchone()["c"] == 1
    finally:
        conn.close()


def test_name_only_never_merges(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    d1 = prepare_supporting(cfg, stage_text_pdf(cfg, "full.pdf", DOC_LINES))
    c1 = run_extraction(d1["id"], cfg)["case_id"]
    d2 = prepare_supporting(cfg, stage_text_pdf(cfg, "nameonly.pdf", ["Name: Ravi Kumar"]))
    out2 = run_extraction(d2["id"], cfg)
    assert out2["case_id"] != c1  # new provisional, not merged on name alone
    assert out2["ocr_status"] == "needs_attention"
    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM cases").fetchone()["c"] == 2
    finally:
        conn.close()


def test_photo_skipped_no_ocr(tmp_cfg, monkeypatch):
    import pytesseract

    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no OCR for photos")))
    from PIL import Image

    cfg = make_cfg(tmp_cfg)
    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    src = staging / "face.png"
    Image.new("RGB", (200, 200), (30, 30, 30)).save(src, "PNG")
    doc = prepare_document(src, cfg, doctype_hint="photo")
    out = run_extraction(doc["id"], cfg)
    assert out["ocr_status"] == "skipped"
    conn = connect(cfg.db_path)
    try:
        assert list_case_fields(conn, 99999) == []
    finally:
        conn.close()


def test_conflicting_dobs_surfaced_not_guessed(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage_text_pdf(cfg, "conf.pdf", ["Name: Ravi Kumar", "DOB: 17-08-1995",
                                           "Date of Birth: 13-06-1999"])
    doc = prepare_supporting(cfg, src)
    out = run_extraction(doc["id"], cfg)
    assert out["ocr_status"] == "needs_attention"
    conn = connect(cfg.db_path)
    try:
        dobs = {r["value"] for r in list_case_fields(conn, out["case_id"]) if r["field_key"] == "dob"}
        assert dobs == {"1995-08-17", "1999-06-13"}  # both retained
    finally:
        conn.close()


def test_date_ambiguity_low_confidence_raw_kept():
    assert parse_date_token("05/06/2020") == ("05/06/2020", True, "dmy-ambiguous")
    assert parse_date_token("13/06/2020") == ("2020-06-13", False, "dmy")
    assert parse_date_token("1998-05-12") == ("1998-05-12", False, "ymd")
    assert parse_date_token("12 May 1998") == ("1998-05-12", False, "month-name")
    assert parse_date_token("99/99/9999") is None


def test_missing_fields_needs_attention(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    src = stage_text_pdf(cfg, "empty.pdf", ["Hello world, no labels here"])
    doc = prepare_supporting(cfg, src)
    out = run_extraction(doc["id"], cfg)
    assert out["ocr_status"] == "needs_attention"
    assert "no candidate" in (out["ocr_error"] or "")


def test_image_doc_unavailable_without_tesseract(tmp_cfg, monkeypatch):
    import app.services.ocr as ocr_mod
    from PIL import Image

    monkeypatch.setattr(ocr_mod, "_COMMON_WINDOWS_PATHS", ())
    cfg = make_cfg(tmp_cfg, tesseract_cmd="tesseract-definitely-missing-xyz")
    staging = cfg.data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    src = staging / "scan.png"
    Image.new("RGB", (300, 100), (255, 255, 255)).save(src, "PNG")
    doc = prepare_document(src, cfg, doctype_hint="supporting_ll")
    out = run_extraction(doc["id"], cfg)  # must not raise
    assert out["ocr_status"] == "unavailable"
    assert "nstall" in (out["ocr_error"] or "")


def test_extraction_idempotent_and_survives_restart(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    doc = prepare_supporting(cfg, stage_text_pdf(cfg, "r.pdf", DOC_LINES))
    c1 = run_extraction(doc["id"], cfg)["case_id"]
    again = run_extraction(doc["id"], cfg)
    assert again["case_id"] == c1
    conn = connect(cfg.db_path)
    try:
        n1 = len(list_case_fields(conn, c1))
    finally:
        conn.close()
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        assert len(list_case_fields(conn, c1)) == n1
    finally:
        conn.close()


def test_detect_conflicts_unit(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        case = conn.execute(
            "INSERT INTO cases (case_id, status, review_version, created_at, updated_at)"
            " VALUES ('CASE-20261015-001','needs_attention',1,'x','x') RETURNING *").fetchone()
        create_extracted_field(conn, case["id"], "dob", "1998-05-12")
        assert detect_conflicts(conn, case["id"]) == []
        create_extracted_field(conn, case["id"], "dob", "1999-06-13")
        assert detect_conflicts(conn, case["id"]) == ["dob"]
    finally:
        conn.close()


def test_values_never_invented(tmp_cfg):
    cfg = make_cfg(tmp_cfg)
    rules = load_field_rules(cfg)
    pages = [{"page": 1, "text": "Name: Ravi Kumar\nDate of Birth: 12-05-1998\n", "method": "text"}]
    cands = extract_candidates(pages, rules)
    text = pages[0]["text"]
    for c in cands:
        if c["field_key"] == "dob" and "-" in c["value"] and len(c["value"]) == 10:
            continue  # ISO normalization of a present date is allowed
        assert c["value"] in text
    assert extract_candidates([{"page": 1, "text": "nothing here", "method": "text"}], rules) == []
