"""Phase 4A extraction-rule tests (synthetic documents only, no real PII).

Covers the four targeted improvements derived from the local, operator-
verified evaluation of six learner-licence documents: template-B
"Display Name" rows, template-B "Son/Wife/Daughter of" father rows,
address-block boundaries (five-line/PIN-terminated, no over-capture or
duplicates), and the standalone "No <digits>" licence fallback. All
values below are synthetic placeholders, never document data.
"""
from app.config import load_config
from app.services.fields import extract_candidates, load_field_rules


def rules(tmp_cfg):
    cfg = load_config(dict(tmp_cfg))
    cfg.ensure_data_dirs()
    return load_field_rules(cfg)


def page(text):
    return [{"page": 1, "method": "ocr", "text": text}]


def cands(text, tmp_cfg, key):
    out = extract_candidates(page(text), rules(tmp_cfg))
    return [c for c in out if c["field_key"] == key]


# ---------------------------------------------------------------------------
# Task 1: applicant name -- explicit "Display Name" with "Name" precedence
# ---------------------------------------------------------------------------

def test_display_name_row_is_extracted(tmp_cfg):
    cs = cands("Display Name: Ravi Kumar\n", tmp_cfg, "full_name")
    assert [c["value"] for c in cs] == ["Ravi Kumar"]


def test_fused_display_name_ocr_form(tmp_cfg):
    # OCR fuses label words on the audited documents ("PresentAddress"
    # was observed literally); "DisplayName" must match the same way.
    cs = cands("DisplayName: Ravi Kumar\n", tmp_cfg, "full_name")
    assert [c["value"] for c in cs] == ["Ravi Kumar"]


def test_display_name_precedes_generic_name_row(tmp_cfg):
    text = ("Name: Ram Singh\n"
            "Display Name: Ravi Kumar Rao\n")
    cs = cands(text, tmp_cfg, "full_name")
    # Only the specific label survives: the bare "Name" row is a shorter,
    # unrelated row on template-B documents.
    assert [c["value"] for c in cs] == ["Ravi Kumar Rao"]


def test_template_a_name_labels_still_work(tmp_cfg):
    for line in ("1. Name: Ravi Kumar\n",
                 "Full Name: Ravi Kumar\n",
                 "Name of the Applicant: Ravi Kumar\n"):
        cs = cands(line, tmp_cfg, "full_name")
        assert [c["value"] for c in cs] == ["Ravi Kumar"], line


def test_display_name_missing_or_malformed_value(tmp_cfg):
    for line in ("Display Name:\n",
                 "Display Name: 12345\n",
                 "Display Name: R\n"):
        assert cands(line, tmp_cfg, "full_name") == [], line


def test_display_name_photo_noise_tail_is_trimmed(tmp_cfg):
    cs = cands("Display Name: RAVI KUMAR xyz\n", tmp_cfg, "full_name")
    assert [c["value"] for c in cs] == ["RAVI KUMAR"]


# ---------------------------------------------------------------------------
# Task 2: father's/guardian name -- template-B "Son/Wife/Daughter of"
# ---------------------------------------------------------------------------

def test_son_wife_daughter_of_row_is_father_name(tmp_cfg):
    text = "Son/Wife/Daughter of: Rajesh Kumar\n"
    assert [c["value"] for c in cands(text, tmp_cfg, "father_name")] == \
        ["Rajesh Kumar"]
    # The father row is never the applicant's name.
    assert cands(text, tmp_cfg, "full_name") == []


def test_son_wife_daughter_ocr_variants(tmp_cfg):
    for line in ("Son / Wife / Daughter of: Rajesh Kumar\n",
                 "Son/Wife/Daughter Rajesh Kumar\n",     # OCR dropped "of"
                 "Son/Wife/DaughterOf Rajesh Kumar\n"):  # OCR fused "of"
        cs = cands(line, tmp_cfg, "father_name")
        assert [c["value"] for c in cs] == ["Rajesh Kumar"], line


def test_applicant_and_father_rows_stay_separate(tmp_cfg):
    text = ("Display Name: Ravi Kumar\n"
            "Son/Wife/Daughter of: Rajesh Kumar\n")
    assert [c["value"] for c in cands(text, tmp_cfg, "full_name")] == \
        ["Ravi Kumar"]
    assert [c["value"] for c in cands(text, tmp_cfg, "father_name")] == \
        ["Rajesh Kumar"]


def test_father_relation_prose_is_not_matched(tmp_cfg):
    # Labels are anchored to the line start; mid-line prose must not
    # become a father's name.
    assert cands("The son/wife/daughter of the soil\n", tmp_cfg,
                 "father_name") == []


def test_template_a_father_labels_still_work(tmp_cfg):
    for line in ("2. Father's Name: Suresh Kumar\n",
                 "Father Name: Suresh Kumar\n"):
        cs = cands(line, tmp_cfg, "father_name")
        assert [c["value"] for c in cs] == ["Suresh Kumar"], line


# ---------------------------------------------------------------------------
# Task 3: address-block boundaries
# ---------------------------------------------------------------------------

def test_five_line_address_not_truncated(tmp_cfg):
    text = ("6. Present Address: 12 MG Road\n"
            "Flat 4\n"
            "Banjara Hills\n"
            "Hyderabad\n"
            "Telangana 500034\n")
    cs = cands(text, tmp_cfg, "address")
    assert [c["value"] for c in cs] == [
        "12 MG Road, Flat 4, Banjara Hills, Hyderabad, Telangana 500034"]


def test_address_stops_at_next_field_label(tmp_cfg):
    text = ("Address: 12 MG Road\n"
            "Date of Birth: 01-02-1990\n")
    cs = cands(text, tmp_cfg, "address")
    assert [c["value"] for c in cs] == ["12 MG Road"]


def test_address_stops_at_application_no_label(tmp_cfg):
    # application_no labels are part of the address boundary set; without
    # that boundary the 10-digit number line would be swallowed.
    text = ("Address: 12 MG Road\n"
            "Application No: 3542873326\n")
    cs = cands(text, tmp_cfg, "address")
    assert [c["value"] for c in cs] == ["12 MG Road"]


def test_present_and_permanent_addresses_not_concatenated(tmp_cfg):
    text = ("Present Address: A Block\n"
            "Hyderabad\n"
            "Permanent Address: B Block\n"
            "Vijayawada\n")
    cs = cands(text, tmp_cfg, "address")
    assert [c["value"] for c in cs] == \
        ["A Block, Hyderabad", "B Block, Vijayawada"]


def test_fused_address_section_label_is_boundary(tmp_cfg):
    # The audited OCR fuses section labels ("PermanentAddress"); the
    # fused line ends the first block and starts its own.
    text = ("Address: Sector 12\n"
            "PermanentAddress: Sector 9\n")
    cs = cands(text, tmp_cfg, "address")
    assert [c["value"] for c in cs] == ["Sector 12", "Sector 9"]


def test_pin_on_final_address_line_stops_the_block(tmp_cfg):
    text = ("Address: 12 MG Road\n"
            "Hyderabad 500001\n"
            "Unrelated noise after the address\n"
            "More noise\n")
    cs = cands(text, tmp_cfg, "address")
    assert [c["value"] for c in cs] == ["12 MG Road, Hyderabad 500001"]


def test_identical_address_rows_emit_one_candidate(tmp_cfg):
    text = ("Address: 12 MG Road\n"
            "Address: 12 MG Road\n")
    cs = cands(text, tmp_cfg, "address")
    assert [c["value"] for c in cs] == ["12 MG Road"]


def test_no_address_label_keeps_conservative_pin_heuristic(tmp_cfg):
    cs = cands("12 MG Road\nHyderabad 500001\n", tmp_cfg, "address")
    assert len(cs) == 1
    assert cs[0]["value"] == "12 MG Road, Hyderabad 500001"
    assert cs[0]["method"].endswith("heuristic-pin")
    assert cs[0]["confidence"] < 0.6
    # A PIN line whose previous line is a field label is not an address.
    assert cands("Name: Ravi Kumar\n500001\n", tmp_cfg, "address") == []


# ---------------------------------------------------------------------------
# Task 4: standalone "No <digits>" licence fallback safety
# ---------------------------------------------------------------------------

def test_standalone_licence_line_kept_with_low_confidence(tmp_cfg):
    cs = cands("No 1234567890\n", tmp_cfg, "licence_no")
    assert [c["value"] for c in cs] == ["1234567890"]
    assert cs[0]["method"].endswith("standalone-no")
    # Attribution is ambiguous without licence/application context, so the
    # candidate is kept but visibly low-confidence (never silently dropped).
    assert cs[0]["confidence"] < 0.6


def test_standalone_no_matching_application_number_is_not_licence(tmp_cfg):
    text = ("5. App.No. 3542873326\n"
            "No 3542873326\n")
    assert cands(text, tmp_cfg, "licence_no") == []
    assert [c["value"] for c in cands(text, tmp_cfg, "application_no")] == \
        ["3542873326"]


def test_standalone_no_adjacent_to_split_app_label_is_not_licence(tmp_cfg):
    # OCR split the application label row: the label lost its number, the
    # number lost its label. The remnant must not become a licence.
    text = ("Application No:\n"
            "No 1234567890\n")
    assert cands(text, tmp_cfg, "licence_no") == []


def test_standalone_no_with_unrelated_application_number_stays_licence(tmp_cfg):
    # A complete, different application number on the adjacent line means
    # the standalone line is not that row's remnant: keep the licence.
    text = ("App.No. 1111222233\n"
            "No 9876501234\n")
    cs = cands(text, tmp_cfg, "licence_no")
    assert [c["value"] for c in cs] == ["9876501234"]
    assert cs[0]["confidence"] < 0.6
