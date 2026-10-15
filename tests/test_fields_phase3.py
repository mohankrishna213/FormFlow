"""Phase 3 parser tests (synthetic documents only; no real PII).

The label lists and heuristics under test were derived from the local
document-format audit recorded in the ``app/services/fields.py`` module
docstring; these tests pin the behavior on synthetic text, never on real
applicant data.
"""
from app.config import load_config
from app.services.fields import (
    _tidy_address_segment,
    extract_candidates,
    extract_name_sequence,
    load_field_rules,
    parse_application_no,
    parse_licence_no,
)


def rules(tmp_cfg):
    cfg = load_config(dict(tmp_cfg))
    cfg.ensure_data_dirs()
    return load_field_rules(cfg)


def page(text):
    return [{"page": 1, "method": "ocr", "text": text}]


def cands(text, tmp_cfg, key):
    out = extract_candidates(page(text), rules(tmp_cfg))
    return [c for c in out if c["field_key"] == key]


def test_father_name_from_printed_label(tmp_cfg):
    cs = cands("2. Father's Name: Suresh Kumar\n", tmp_cfg, "father_name")
    assert [c["value"] for c in cs] == ["Suresh Kumar"]


def test_father_name_label_variants(tmp_cfg):
    for line in ("Name of the Father: Suresh Kumar\n",
                 "Father Name: Suresh Kumar\n",
                 "Father's / Husband's Name: Suresh Kumar\n"):
        cs = cands(line, tmp_cfg, "father_name")
        assert [c["value"] for c in cs] == ["Suresh Kumar"], line


def test_name_of_the_father_is_not_the_applicant(tmp_cfg):
    line = "2. Name of the Father: Suresh Kumar\n"
    assert cands(line, tmp_cfg, "full_name") == []
    assert [c["value"] for c in cands(line, tmp_cfg, "father_name")] == \
        ["Suresh Kumar"]


def test_photo_noise_tail_trim_is_documented_and_narrow(tmp_cfg):
    # Trailing all-lowercase fragment after 2+ non-lowercase tokens is
    # dropped (Phase 3 audit: a photo-area artifact merged into the
    # printed Name line). Casing is preserved as printed; matching is
    # case-insensitive through normalize_name.
    cs = cands("1. Name: RAVI KUMAR xyz\n", tmp_cfg, "full_name")
    assert [c["value"] for c in cs] == ["RAVI KUMAR"]
    # Two-token runs are never trimmed (precondition is 2+ non-lowercase
    # tokens BEFORE the lowercase fragment, i.e. 3+ tokens total).
    cs2 = cands("1. Name: RAVI kumar\n", tmp_cfg, "full_name")
    assert [c["value"] for c in cs2] == ["RAVI Kumar"]
    # All-lowercase names are never trimmed; only the first letter is
    # uppercased, existing casing is otherwise preserved.
    cs3 = cands("1. Name: ravi kumar\n", tmp_cfg, "full_name")
    assert [c["value"] for c in cs3] == ["Ravi Kumar"]


def test_application_no_observed_ocr_label_forms(tmp_cfg):
    for line in ("AppiNo . 3542873326\n",
                 "5. App.No. 3542873326\n",
                 "Application No: 3542873326\n"):
        cs = cands(line, tmp_cfg, "application_no")
        assert [c["value"] for c in cs] == ["3542873326"], line


def test_application_no_digit_bounds_are_strict(tmp_cfg):
    assert parse_application_no("1234567") is None       # 7 digits: too short
    assert parse_application_no("12345678") == "12345678"  # 8: lower bound
    assert parse_application_no("1" * 20) == "1" * 20    # 20: upper bound
    assert parse_application_no("1" * 21) is None        # 21: too long
    assert parse_application_no("1" * 25) is None
    assert parse_application_no("no digits here") is None


def test_application_no_label_must_not_split_a_word(tmp_cfg):
    # 'appno' inside a larger token must not match (lookbehind guard).
    assert cands("happno 12345678\n", tmp_cfg, "application_no") == []


def test_licence_no_parser_and_standalone_line(tmp_cfg):
    # OCR left a trailing slash; the display anchor strips it.
    assert parse_licence_no("tg 1234567890/") == "TG 1234567890"
    assert parse_licence_no("Ravi Kumar") is None  # prose, no digits
    cs = cands("No 4061084326\n", tmp_cfg, "licence_no")
    assert [c["value"] for c in cs] == ["4061084326"]
    assert cs[0]["method"].endswith("standalone-no")


def test_address_trailing_artifact_trim():
    assert _tidy_address_segment("12 MG Road A") == "12 MG Road"
    assert _tidy_address_segment("12 MG Road -") == "12 MG Road"
    assert _tidy_address_segment("12 MG Road") == "12 MG Road"
    # Digits and real words are never removed.
    assert _tidy_address_segment("12 MG Road 500001") == "12 MG Road 500001"


def test_address_block_stops_after_pin(tmp_cfg):
    text = ("6. Present Address: 12 MG Road\n"
            "Hyderabad 500001\n"
            "Extra noise line\n")
    cs = cands(text, tmp_cfg, "address")
    assert len(cs) == 1
    assert cs[0]["value"] == "12 MG Road, Hyderabad 500001"
    assert cs[0]["method"].endswith("labeled-block")


def test_extract_name_sequence_never_invents_text():
    assert extract_name_sequence("12345") is None
    assert extract_name_sequence("") is None
    assert extract_name_sequence("Kumar") is None       # single token
    assert extract_name_sequence("a b c d e f g") is None  # > max_words
    # Casing is preserved except for the first letter of each token.
    assert extract_name_sequence("RAVI KUMAR") == "RAVI KUMAR"
    assert extract_name_sequence("ravi kumar") == "Ravi Kumar"
