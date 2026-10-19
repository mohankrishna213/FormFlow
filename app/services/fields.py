"""Deterministic candidate-field extraction (Phase 4, extended in Phase 3).

Rules are configurable via ocr_rules.json and easy to extend: labels per
field, date handling, address heuristics. Parsers never invent values;
missing stays missing, ambiguous stays low-confidence.

Supported field keys:
- Core 6: full_name, father_name, dob, address, licence_no, application_no
- Extended: gender, mobile_no, email, qualification, place_of_birth,
  blood_group, id_mark_1, id_mark_2, rto_code, pan_no, aadhaar_no,
  vehicle_reg_no, pres_pincode, pres_house_no.

Constraints:
- English OCR only.
- Complete date only: year-only (YYYY) is never converted to a full DOB.
- Aadhaar/PAN/RC formats are attributed only with label or reliable context.
- Spatial/two-line label matching supported when labels and values occupy
  separate lines or regions.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from ..config import FormFlowConfig

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_DEFAULT_RULES: dict[str, Any] = {
    "version": "defaults",
    "dpi": 300,
    "lang": "eng",
    "psm": 6,
    "upscale": False,
    "upscale_min_height": 1500,
    "min_page_chars": 30,
    "binarize": False,
    "fields": {
        "full_name": {
            "labels": [
                "full name", "display name", "name of the applicant",
                "applicant name", "holder name", "candidate name",
                "student name", "name of the candidate", "certified that",
                "customer name", "name",
            ],
            "max_words": 5,
        },
        "father_name": {
            "labels": [
                "father's name", "fathers name", "father name",
                "name of the father", "name of father",
                "father's / husband's name", "father/husband name",
                "father / husband name",
                "son / wife / daughter of", "son / wife / daughter",
                "c/o", "s/o", "d/o", "w/o", "care of",
            ],
            "max_words": 5,
        },
        "dob": {
            "labels": ["date of birth", "dob", "birth date", "born", "d.o.b", "d.o.b.", "birthdate"],
        },
        "gender": {
            "labels": ["gender", "sex"],
        },
        "address": {
            "labels": ["permanent address", "present address", "address", "addr", "residence"],
            "max_lines": 3,
        },
        "mobile_no": {
            "labels": [
                "mobile number", "mobile no", "mobile", "phone number",
                "phone no", "phone", "contact no", "cell phone", "cell",
                "ph no", "ph.no", "mob no", "mob.no",
            ],
        },
        "email": {
            "labels": ["email id", "email address", "email", "e-mail id", "e-mail address", "e-mail"],
        },
        "qualification": {
            "labels": [
                "educational qualification", "qualification", "education",
                "highest qualification", "passed examination", "standard",
                "passed ssc examination",
            ],
        },
        "place_of_birth": {
            "labels": ["place of birth", "birth place", "pob", "born at", "place of origin"],
        },
        "blood_group": {
            "labels": ["blood group", "blood grp", "bloodgroup", "bg", "blood"],
        },
        "id_mark_1": {
            "labels": [
                "identification mark 1", "identification marks 1",
                "identification mark i", "identification marks",
                "identification mark", "id mark 1", "visible mark 1", "visible mark",
            ],
        },
        "id_mark_2": {
            "labels": [
                "identification mark 2", "identification marks 2",
                "identification mark ii", "id mark 2", "visible mark 2",
            ],
        },
        "licence_no": {
            "labels": [
                "driving licence no", "driving license no", "licence no",
                "license no", "licence number", "license number", "lic no",
                "learning licence no", "learning license no",
                "learning licence number", "learning license number",
                "learner's licence no", "learner's license no",
                "learner's licence", "learner's license",
                "learners licence no", "learners license no",
                "learning licence", "learning license",
                "dl no", "ll no", "ll number", "licence", "license",
            ],
        },
        "application_no": {
            "labels": [
                "application number", "application no", "application num",
                "appi no", "appino", "app no", "appno", "app number", "appnumber",
                "app. no", "app.no", "appl no", "appl. no", "appl.no",
                "appl number", "appln no", "appln number",
                "application ref no", "app ref no", "acknowledgement no", "ack no",
            ],
            "min_digits": 8,
            "max_digits": 20,
        },
        "rto_code": {
            "labels": ["rto office", "rto code", "rto", "jurisdiction", "registering authority"],
        },
        "pan_no": {
            "labels": [
                "permanent account number card", "permanent account number",
                "pan card no", "pan card number", "pan no", "pan number", "pan",
            ],
        },
        "aadhaar_no": {
            "labels": ["aadhaar no", "aadhar no", "aadhaar number", "aadhar number", "aadhaar", "aadhar", "uid", "uidai"],
        },
        "vehicle_reg_no": {
            "labels": ["registration no", "registration number", "reg no", "regn no", "vehicle no", "vehicle reg no", "rc no"],
        },
    },
}


def load_field_rules(cfg: FormFlowConfig) -> dict[str, Any]:
    rules: dict[str, Any] = json.loads(json.dumps(_DEFAULT_RULES))
    try:
        raw = json.loads(Path(cfg.ocr_rules_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return rules
    if not isinstance(raw, dict):
        return rules
    for key in ("dpi", "lang", "psm", "upscale", "upscale_min_height",
                "min_page_chars", "binarize", "version"):
        if key in raw and raw[key] not in (None, ""):
            rules[key] = raw[key]
    if isinstance(raw.get("fields"), dict):
        for fname, fval in raw["fields"].items():
            if isinstance(fval, dict):
                rules["fields"].setdefault(fname, {}).update(fval)
    return rules


# Fields for which a deterministic parser exists in this module.
PARSER_FIELDS = frozenset({
    "full_name", "father_name", "dob", "gender", "licence_no", "application_no",
    "address", "pres_pincode", "pres_house_no", "pres_street", "pres_location",
    "pres_district", "pres_state", "pres_mandal", "mobile_no", "email",
    "qualification", "place_of_birth", "blood_group", "id_mark_1", "id_mark_2",
    "rto_code", "pan_no", "aadhaar_no", "vehicle_reg_no",
})


def _label_pattern(labels: list[str], anchored: bool = True) -> re.Pattern:
    """Compile a label matcher exposing named groups ``label`` and ``value``."""
    norm = {re.sub(r"\s+", " ", str(l).lower()).strip() for l in labels}
    ordered = sorted((l for l in norm if l), key=len, reverse=True)
    alt = "|".join(
        r"\s*".join(re.escape(part) for part in l.split(" ")) for l in ordered)
    head = r"^\s*(?:[0-9/\\#*~.-]+\s*[.,/-]?\s*)?" if anchored else r"(?<![A-Za-z0-9])"
    return re.compile(
        rf"(?i){head}(?P<label>{alt})(?![A-Za-z])\s*[:\-]?\s*(?P<value>.*)$"
    )


_STANDALONE_LICENCE_RE = re.compile(r"^\s*No\.?\s*(\d{6,20})\s*$", re.IGNORECASE)
_ADDR_PIN_EXTEND = 1
_PIN6_RE = re.compile(r"\b\d{6}\b")

_NAME_STOPWORDS = frozenset({
    "the", "of", "and", "son", "daughter", "wife", "husband", "father",
    "mother", "parents", "parent", "guardian", "male", "female", "indian",
    "india", "sri", "smt", "shri", "mr", "mrs", "ms", "dr", "no", "nil",
    "name", "names", "address", "date", "birth", "dob", "licence",
    "license", "application", "applicant", "holder", "form", "signature",
    "government", "govt", "telangana", "state", "valid", "issued", "copy",
    "certified", "that", "belongs", "roll", "marks", "memo", "passed",
})

_NAME_WORD_RE = re.compile(r"[A-Za-z][A-Za-z.'\-]*")


def _clean_lines(text: str) -> list[str]:
    return [re.sub(r"[ \t]+", " ", ln.replace("\u2019", "'")).strip()
            for ln in text.splitlines() if ln.strip()]


def parse_name(value: str, max_words: int = 5) -> str | None:
    val = re.sub(r"\s+", " ", value.strip().strip(".,;:-")).strip()
    if not val or any(ch.isdigit() for ch in val):
        return None
    words = val.split(" ")
    if not 2 <= len(words) <= max_words:
        return None
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*", w) for w in words):
        return None
    return " ".join(w[0].upper() + w[1:] if w else w for w in words)


def extract_name_sequence(value: str, max_words: int = 5) -> str | None:
    """Pick the best contiguous name-shaped run from a labeled value area."""
    runs: list[list[str]] = []
    cur: list[str] = []
    for raw in re.findall(r"\S+", value or ""):
        tok = raw.strip(".,;:()[]|<>«»\"'")
        if _NAME_WORD_RE.fullmatch(tok or "") and tok.lower() not in _NAME_STOPWORDS:
            cur.append(tok)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    candidates = [r for r in runs if 2 <= len(r) <= max_words]
    if not candidates:
        return None
    best = max(candidates, key=len)
    while len(best) > 2 and best[-1].islower() and not all(w.islower() for w in best):
        best = best[:-1]
    if not 2 <= len(best) <= max_words:
        return None
    return " ".join(w[0].upper() + w[1:] for w in best)


def parse_application_no(
    value: str, min_digits: int = 8, max_digits: int = 20
) -> str | None:
    val = value or ""
    m = re.search(
        rf"(?<!\d)(\d{{{int(min_digits)},{int(max_digits)}}})(?!\d)", val)
    return m.group(1) if m else None


def _tidy_address_segment(seg: str) -> str:
    toks = re.sub(r"\s+", " ", (seg or "").strip()).split(" ")
    while toks:
        last = toks[-1].strip(".,;:-")
        if not last or not re.search(r"[A-Za-z0-9]", last) or (
                len(last) == 1 and last.isalpha()):
            toks.pop()
        else:
            break
    return " ".join(toks).strip(" ,;:-")


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[.'\"\-]", "", value.lower())).strip()


def parse_dmy(day: int, month: int, year: int) -> tuple[str, bool] | None:
    today = date.today().year
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= today + 1):
        return None
    try:
        iso = date(year, month, day).isoformat()
    except ValueError:
        return None
    return iso, (day <= 12 and month <= 12 and day != month)


_DATE_RES = [
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b"),
    re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b"),
]


def parse_date_token(token: str) -> tuple[str, bool, str] | None:
    """Return (value, ambiguous, kind). Rejects year-only tokens."""
    t = token.strip()
    m = _DATE_RES[0].search(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        parsed = parse_dmy(d, mo, y)
        if parsed is None:
            return None
        iso, ambiguous = parsed
        if ambiguous:
            return m.group(0), True, "dmy-ambiguous"
        return iso, False, "dmy"
    m = _DATE_RES[1].search(t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        parsed = parse_dmy(d, mo, y)
        if parsed is None:
            return None
        return parsed[0], False, "ymd"
    m = re.compile(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b").search(t)
    if m and m.group(2).lower() in MONTHS:
        parsed = parse_dmy(int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3)))
        if parsed is None:
            return None
        return parsed[0], False, "month-name"
    return None


def parse_licence_no(value: str) -> str | None:
    val = re.sub(r"\s+", " ", value.strip().strip(".,;:-/")).strip().upper()
    if not 6 <= len(val) <= 24:
        return None
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9 \-/]*[A-Z0-9]", val):
        return None
    if not re.search(r"\d", val):
        return None
    if re.search(r"[A-Z]{6,}", val.replace(" ", "")):
        return None
    return val


# --- Phase 3 deterministic parsers ---


def parse_gender(value: str) -> str | None:
    val = value.strip().upper()
    if re.search(r"\b(TRANSGENDER|TRANS)\b", val):
        return "Transgender"
    if re.search(r"\bFEMALE\b|\bF\b", val) and not re.search(r"\bMALE\b", val):
        return "Female"
    if re.search(r"\bMALE\b|\bM\b", val) and not re.search(r"\bFEMALE\b", val):
        return "Male"
    return None


_MOBILE_RE = re.compile(r"(?:\+91[\s\-]*)?([6-9]\d{9})\b")


def parse_mobile_no(value: str) -> str | None:
    m = _MOBILE_RE.search(value)
    return m.group(1) if m else None


_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")


def parse_email(value: str) -> str | None:
    m = _EMAIL_RE.search(value)
    return m.group(1).lower() if m else None


_BLOOD_GROUP_RE = re.compile(r"\b(A|B|AB|O)\s*([+\-])(?:VE)?\b", re.IGNORECASE)


def parse_blood_group(value: str) -> str | None:
    m = _BLOOD_GROUP_RE.search(value)
    if m:
        return f"{m.group(1).upper()}{m.group(2)}"
    return None


def parse_qualification(value: str, matched_label: str = "") -> str | None:
    lbl_lower = matched_label.lower()
    if "ssc" in lbl_lower or "secondary school" in lbl_lower:
        return "10th Standard / SSC"
    v = value.strip()
    v_lower = v.lower()
    if re.search(r"\b(ssc|secondary school certificate|10th standard|10th class|matriculation|matric|x class|class 10)\b", v_lower):
        return "10th Standard / SSC"
    if re.search(r"\b(intermediate|12th standard|12th class|higher secondary|10\+2|plus two)\b", v_lower):
        return "10+2 / Intermediate"
    if re.search(r"\b(graduate|b\.?tech|b\.?e|b\.?sc|b\.?com|b\.?a|degree)\b", v_lower):
        return "Graduate"
    if re.search(r"\b(post graduate|m\.?tech|m\.?sc|m\.?com|m\.?a|m\.?b\.?a)\b", v_lower):
        return "Post Graduate"
    if re.match(r"(?i)^\s*held\s+in\b", v_lower):
        return None
    if len(v) >= 3 and len(v.split()) <= 4 and re.match(r"^[A-Za-z0-9\s/.\-]+$", v):
        return v.title()
    return None


_ID_MARK_START = re.compile(
    r"(?i)\b((?:a\s+)?(?:mole|scar|burn\s+mark|black\s+mole|birth\s+mark|cut\s+mark)\s+(?:on|near|over|under|above)\s+[A-Za-z\s,]+)"
)


def parse_id_mark(value: str) -> str | None:
    v = value.strip().strip(".,;:-")
    m = _ID_MARK_START.search(v)
    if m:
        mark = m.group(1).strip()
        words = mark.split()
        return " ".join(w.capitalize() if i == 0 else w.lower() for i, w in enumerate(words))
    if len(v.split()) >= 3 and len(v) <= 120 and re.search(r"[A-Za-z]", v):
        return v
    return None


_PAN_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")


def parse_pan_no(value: str) -> str | None:
    m = _PAN_RE.search(value.upper())
    return m.group(1) if m else None


_AADHAAR_SPACED_RE = re.compile(r"\b(\d{4}\s\d{4}\s\d{4})\b")
_AADHAAR_DENSE_RE = re.compile(r"\b(\d{12})\b")


def parse_aadhaar_no(value: str) -> str | None:
    # Exclude Virtual ID (VID) lines e.g. "VID: 9133 2860 8412 0194"
    if re.search(r"(?i)\bVID\b", value):
        return None
    if re.search(r"\b\d{4}\s\d{4}\s\d{4}\s\d{4}\b", value):
        return None
    m = _AADHAAR_SPACED_RE.search(value)
    if m:
        return m.group(1)
    m2 = _AADHAAR_DENSE_RE.search(value)
    if m2:
        d = m2.group(1)
        return f"{d[:4]} {d[4:8]} {d[8:]}"
    return None


_VEHICLE_REG_RE = re.compile(r"\b([A-Z]{2}[\s\-]?[0-9]{1,2}[\s\-]?[A-Z]{1,3}[\s\-]?[0-9]{4})\b")


def parse_vehicle_reg_no(value: str) -> str | None:
    m = _VEHICLE_REG_RE.search(value.upper())
    if m:
        return re.sub(r"[\s\-]+", "", m.group(1))
    return None


_RTO_CODE_RE = re.compile(r"\b([A-Z]{2}[\s\-]?[0-9]{1,2})\b")


def parse_rto_code(value: str) -> str | None:
    m = _RTO_CODE_RE.search(value.upper())
    if m:
        return re.sub(r"[\s\-]+", "-", m.group(1))
    return None


def parse_pincode(value: str) -> str | None:
    m = _PIN6_RE.search(value)
    return m.group(0) if m else None


def parse_place_of_birth(value: str) -> str | None:
    val = re.sub(r"\s+", " ", value.strip().strip(".,;:-")).strip()
    if 2 <= len(val) <= 40 and re.match(r"^[A-Za-z\s.\-]+$", val):
        return val.title()
    return None


INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
    "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Delhi", "Jammu and Kashmir", "Ladakh", "Puducherry", "Chandigarh"
]


def decompose_address(address_str: str) -> dict[str, str]:
    if not address_str:
        return {}
    raw = address_str.strip()
    result: dict[str, str] = {
        "house_no": "",
        "street": "",
        "location": "",
        "mandal": "",
        "district": "",
        "state": "",
        "pincode": "",
    }
    pin_m = re.search(r"\b([1-9]\d{5})\b", raw)
    if pin_m:
        result["pincode"] = pin_m.group(1)
        raw = re.sub(r"\b[1-9]\d{5}\b", "", raw)
        raw = re.sub(r"(?i)\bpin\s*(?:code)?\s*[:.-]?\s*", "", raw)

    for st in INDIAN_STATES:
        pat = rf"(?i)\b{re.escape(st)}\b"
        if re.search(pat, raw):
            result["state"] = st
            raw = re.sub(pat, "", raw)
            break
    if not result["state"]:
        st_code_m = re.search(r"(?i)\b(TG|TS|AP|MH|KA|TN|KL|DL|GJ|RJ|UP|MP|WB)\b", raw)
        if st_code_m:
            code = st_code_m.group(1).upper()
            code_map = {"TG": "Telangana", "TS": "Telangana", "AP": "Andhra Pradesh", "MH": "Maharashtra", "KA": "Karnataka", "TN": "Tamil Nadu"}
            result["state"] = code_map.get(code, code)
            raw = re.sub(rf"(?i)\b{code}\b", "", raw)

    raw = re.sub(r"(?i)\b(?:s/o|c/o|d/o|w/o)\s*[:\-]?\s*[A-Za-z\s]+?(?=,\s*|\d|address|dist|po|pin|near|\n|$)", "", raw)

    dist_m = re.search(r"(?i)\b(?:dist(?:rict)?)\s*[:\-]?\s*([A-Za-z\s]+?)(?=,\s*|\d|po|pin|state|\n|$)", raw)
    if dist_m:
        result["district"] = dist_m.group(1).strip()
        raw = raw[:dist_m.start()] + raw[dist_m.end():]

    mdl_m = re.search(r"(?i)\b(?:mandal|mdl|taluk|tehsil|tahsil)\s*[:\-]?\s*([A-Za-z\s]+?)(?=,\s*|\d|dist|po|pin|\n|$)", raw)
    if mdl_m:
        result["mandal"] = mdl_m.group(1).strip()
        raw = raw[:mdl_m.start()] + raw[mdl_m.end():]

    h_m = re.search(r"(?i)\b(?:h\.?\s*no\.?|d\.?\s*no\.?|door\s*no\.?|flat\s*no\.?|plot\s*no\.?|qtr\s*no\.?|house\s*no\.?)[\s.:#\-]*([0-9A-Za-z\-/]+)", raw)
    if h_m:
        result["house_no"] = h_m.group(0).strip()
        raw = raw[:h_m.start()] + raw[h_m.end():]
    else:
        door_m = re.search(r"\b(\d{1,4}[A-Za-z]?(?:\s*[-/]\s*\d{1,4}[A-Za-z]?)+)\b", raw)
        if door_m:
            tok = door_m.group(1).replace(" ", "")
            if not re.search(r"\b(19\d\d|20\d\d)\b", tok):
                result["house_no"] = tok
                raw = raw[:door_m.start()] + raw[door_m.end():]

    raw = re.sub(r"(?i)\baddress\s*[:\-]?", "", raw)
    raw = re.sub(r"(?i)\bdet\s*on\s*[:\-]?", "", raw)

    _ADDR_NOISE_TOKENS = frozenset({
        "address", "det on", "dist", "district", "state", "pin", "pincode",
        "mandal", "mdl", "taluk", "tehsil", "po", "p.o", "post office",
    })
    tokens = [
        t.strip().strip(",;:-")
        for t in re.split(r"[\n,]+", raw)
        if t.strip().strip(",;:-") and t.strip().strip(",;:-").lower() not in _ADDR_NOISE_TOKENS
    ]
    landmarks = []
    streets = []
    locations = []
    for t in tokens:
        tl = t.lower()
        if re.search(r"(?i)\b(?:po|p\.o|post\s*office)\b", tl):
            locations.append(t)
        elif re.search(r"(?i)\b(?:near|opp|behind|beside)\b", tl):
            landmarks.append(t)
        elif re.search(r"(?i)\b(?:colony|nagar|street|road|lane|gudem|wada|enclave|layout|bazaar|cross|gali)\b", tl):
            streets.append(t)
        elif not result["district"] and len(tokens) >= 2 and t == tokens[-1]:
            result["district"] = t
        elif not result["mandal"] and len(tokens) >= 3 and t == tokens[-2]:
            result["mandal"] = t
            locations.append(t)
        else:
            if len(streets) == 0:
                streets.append(t)
            else:
                locations.append(t)

    street_combined = ", ".join(dict.fromkeys(streets + landmarks)).strip(" ,;:-")
    loc_combined = ", ".join(dict.fromkeys(locations)).strip(" ,;:-")
    if not loc_combined and (result["mandal"] or result["district"]):
        loc_combined = ", ".join(b for b in [result["mandal"], result["district"]] if b).strip(" ,;:-")
    result["street"] = street_combined
    result["location"] = loc_combined
    return {k: v.strip(" ,;:-") for k, v in result.items()}


def _field_cfg(fields: dict, key: str) -> dict:
    cfg = fields.get(key, {}) if isinstance(fields.get(key), dict) else {}
    return cfg


def _labels_of(fields: dict, key: str) -> list[str]:
    return [str(l) for l in (_field_cfg(fields, key).get("labels") or [])]


def _standalone_app_context(
    digits: str, idx: int, lines: list[str],
    app_pat: re.Pattern | None, app_digits: set[str],
) -> bool:
    if digits in app_digits:
        return True
    if app_pat is None:
        return False
    for j in (idx - 1, idx + 1):
        if 0 <= j < len(lines) and app_pat.search(lines[j]) and not re.search(
                r"(?<!\d)\d{6,20}(?!\d)", lines[j]):
            return True
    return False


def extract_candidates(pages: list[dict], rules: dict[str, Any]) -> list[dict]:
    """Deterministic parse of page texts. Never invents missing values."""
    out: list[dict] = []
    fields = rules.get("fields", {}) if isinstance(rules.get("fields"), dict) else {}

    # Compile patterns for configured fields
    name_labels = _labels_of(fields, "full_name")
    father_labels = _labels_of(fields, "father_name")
    dob_labels = _labels_of(fields, "dob")
    gender_labels = _labels_of(fields, "gender")
    addr_labels = _labels_of(fields, "address")
    mob_labels = _labels_of(fields, "mobile_no")
    email_labels = _labels_of(fields, "email")
    qual_labels = _labels_of(fields, "qualification")
    pob_labels = _labels_of(fields, "place_of_birth")
    blood_labels = _labels_of(fields, "blood_group")
    id_mark_1_labels = _labels_of(fields, "id_mark_1")
    id_mark_2_labels = _labels_of(fields, "id_mark_2")
    lic_labels = _labels_of(fields, "licence_no")
    app_labels = _labels_of(fields, "application_no")
    rto_labels = _labels_of(fields, "rto_code")
    pan_labels = _labels_of(fields, "pan_no")
    aadhaar_labels = _labels_of(fields, "aadhaar_no")
    rc_labels = _labels_of(fields, "vehicle_reg_no")

    addr_max_lines = int(_field_cfg(fields, "address").get("max_lines", 3) or 3)
    name_max_words = int(_field_cfg(fields, "full_name").get("max_words", 5) or 5)
    father_max_words = int(_field_cfg(fields, "father_name").get("max_words", 5) or 5)
    app_cfg = _field_cfg(fields, "application_no")
    app_min_digits = int(app_cfg.get("min_digits", 8) or 8)
    app_max_digits = int(app_cfg.get("max_digits", 20) or 20)

    name_pat = _label_pattern(name_labels) if name_labels else None
    father_pat = _label_pattern(father_labels) if father_labels else None
    dob_pat = _label_pattern(dob_labels) if dob_labels else None
    gender_pat = _label_pattern(gender_labels) if gender_labels else None
    addr_pat = _label_pattern(addr_labels) if addr_labels else None
    mob_pat = _label_pattern(mob_labels) if mob_labels else None
    email_pat = _label_pattern(email_labels) if email_labels else None
    qual_pat = _label_pattern(qual_labels) if qual_labels else None
    pob_pat = _label_pattern(pob_labels) if pob_labels else None
    blood_pat = _label_pattern(blood_labels) if blood_labels else None
    id_mark_1_pat = _label_pattern(id_mark_1_labels) if id_mark_1_labels else None
    id_mark_2_pat = _label_pattern(id_mark_2_labels) if id_mark_2_labels else None
    lic_pat = _label_pattern(lic_labels) if lic_labels else None
    app_pat = _label_pattern(app_labels, anchored=False) if app_labels else None
    rto_pat = _label_pattern(rto_labels) if rto_labels else None
    pan_pat = _label_pattern(pan_labels) if pan_labels else None
    aadhaar_pat = _label_pattern(aadhaar_labels) if aadhaar_labels else None
    rc_pat = _label_pattern(rc_labels) if rc_labels else None

    stop_labels = (
        name_labels + father_labels + dob_labels + gender_labels + addr_labels
        + mob_labels + email_labels + qual_labels + blood_labels + lic_labels
        + app_labels + rto_labels + pan_labels + aadhaar_labels + rc_labels
    )
    stop_pat = _label_pattern(stop_labels) if stop_labels else None

    # Specific name label check
    specific_name_found = False
    if name_pat:
        for pg in pages:
            for line in _clean_lines(pg.get("text", "")):
                m = name_pat.match(line)
                if (m and m.group("value")
                        and m.group("label").strip().lower() != "name"
                        and extract_name_sequence(m.group("value"), name_max_words)):
                    specific_name_found = True
                    break
            if specific_name_found:
                break

    all_pages_text = " ".join(pg.get("text", "") for pg in pages).lower()
    doc_is_ll = bool(re.search(r"\bform[\s\-]*3\b|\blearner['’]?s?\s*licen[cs]e|\blearning\s*licen[cs]e", all_pages_text))
    doc_is_dl = bool(re.search(r"\bform[\s\-]*7\b|\bdriving\s*licen[cs]e", all_pages_text))
    doc_is_pan = bool(re.search(r"\bincome\s*tax\s*department\b|\bpermanent\s*account\s*number\b|\bpan\s*card\b", all_pages_text))
    doc_is_aadhaar = bool(re.search(r"\bunique\s*identification\b|\baadhaar\b|\baadhar\b|\buidai\b", all_pages_text))
    doc_is_ssc = bool(re.search(r"\bsecondary\s*school\s*certificate\b|\bssc\s*examination\b|\bboard\s*of\s*secondary\b", all_pages_text))

    seen_entries: set[tuple[str, str]] = set()

    def _add_candidate(key: str, val: str | None, pnum: int, method: str, conf: float) -> None:
        if not val or not str(val).strip():
            return
        clean_val = str(val).strip()
        pair = (key, clean_val.lower())
        if pair in seen_entries:
            return
        seen_entries.add(pair)
        out.append({
            "field_key": key,
            "value": clean_val,
            "source_page": pnum,
            "method": method,
            "confidence": conf,
        })

    for pg in pages:
        pnum = pg.get("page", 1)
        src_method = pg.get("method", "text")
        lines = _clean_lines(pg.get("text", ""))

        app_digits: set[str] = set()
        if app_pat:
            for line in lines:
                if app_pat.search(line):
                    app_digits.update(re.findall(r"(?<!\d)\d{6,20}(?!\d)", line))

        for idx, line in enumerate(lines):
            next_line = lines[idx + 1] if idx + 1 < len(lines) else ""
            next_is_stop = bool(stop_pat and stop_pat.match(next_line))

            # Full Name
            if name_pat:
                m = name_pat.match(line)
                if m:
                    val_str = m.group("value").strip()
                    if (not val_str or len(val_str) <= 2) and next_line and not next_is_stop:
                        val_str = next_line
                    label = m.group("label").strip().lower()
                    relation = label == "name" and re.match(r"(?i)^\s*of\b", val_str)
                    suppressed = label == "name" and specific_name_found
                    if not relation and not suppressed:
                        name = extract_name_sequence(val_str, name_max_words)
                        if name:
                            _add_candidate("full_name", name, pnum, f"{src_method}:labeled", 0.85)

            # Father's Name
            if father_pat:
                m = father_pat.match(line)
                if m:
                    val_str = m.group("value").strip()
                    if (not val_str or len(val_str) <= 2) and next_line and not next_is_stop:
                        val_str = next_line
                    fname = extract_name_sequence(val_str, father_max_words)
                    if fname:
                        _add_candidate("father_name", fname, pnum, f"{src_method}:labeled", 0.85)

            # Date of Birth
            if dob_pat:
                m = dob_pat.match(line)
                if m:
                    val_str = m.group("value").strip()
                    parsed = parse_date_token(val_str)
                    if not parsed and next_line and not next_is_stop:
                        parsed = parse_date_token(next_line)
                    if parsed:
                        val, ambiguous, kind = parsed
                        _add_candidate("dob", val, pnum, f"{src_method}:labeled-{kind}", 0.40 if ambiguous else 0.85)

            # Gender
            if gender_pat:
                m = gender_pat.match(line)
                if m:
                    val_str = m.group("value").strip()
                    if not val_str and next_line and not next_is_stop:
                        val_str = next_line
                    g = parse_gender(val_str)
                    if g:
                        _add_candidate("gender", g, pnum, f"{src_method}:labeled", 0.85)
            # Standalone gender on its own line (e.g. Aadhaar cards where MALE / FEMALE is printed alone)
            if not any(c["field_key"] == "gender" for c in out):
                if re.fullmatch(r"(?i)\s*(MALE|FEMALE|TRANSGENDER)\s*", line):
                    g = parse_gender(line)
                    if g:
                        _add_candidate("gender", g, pnum, f"{src_method}:standalone", 0.80)

            # Mobile Number
            if mob_pat:
                m = mob_pat.match(line)
                if m:
                    mob = parse_mobile_no(m.group("value") or next_line)
                    if mob:
                        _add_candidate("mobile_no", mob, pnum, f"{src_method}:labeled", 0.85)
            elif re.match(r"(?i)^\s*(?:\+91|ph|mob|cell)[\s.:\-]", line):
                mob = parse_mobile_no(line)
                if mob:
                    _add_candidate("mobile_no", mob, pnum, f"{src_method}:prefix", 0.80)

            # Email
            if email_pat:
                m = email_pat.match(line)
                if m:
                    em = parse_email(m.group("value") or next_line)
                    if em:
                        _add_candidate("email", em, pnum, f"{src_method}:labeled", 0.85)
            elif _EMAIL_RE.search(line):
                em = parse_email(line)
                if em:
                    _add_candidate("email", em, pnum, f"{src_method}:pattern", 0.80)

            # Blood Group
            if blood_pat:
                m = blood_pat.match(line)
                if m:
                    bg = parse_blood_group(m.group("value") or next_line)
                    if bg:
                        _add_candidate("blood_group", bg, pnum, f"{src_method}:labeled", 0.85)

            # Qualification
            if qual_pat:
                m = qual_pat.match(line)
                if m:
                    q = parse_qualification(m.group("value") or next_line, matched_label=m.group("label"))
                    if q:
                        _add_candidate("qualification", q, pnum, f"{src_method}:labeled", 0.85)

            # Place of Birth
            if pob_pat:
                m = pob_pat.match(line)
                if m:
                    pob = parse_place_of_birth(m.group("value") or next_line)
                    if pob:
                        _add_candidate("place_of_birth", pob, pnum, f"{src_method}:labeled", 0.80)

            # Identification Marks
            if id_mark_1_pat:
                m = id_mark_1_pat.match(line)
                if m:
                    im1 = parse_id_mark(m.group("value") or next_line)
                    if im1:
                        _add_candidate("id_mark_1", im1, pnum, f"{src_method}:labeled", 0.85)
            if id_mark_2_pat:
                m = id_mark_2_pat.match(line)
                if m:
                    im2 = parse_id_mark(m.group("value") or next_line)
                    if im2:
                        _add_candidate("id_mark_2", im2, pnum, f"{src_method}:labeled", 0.85)

            # Numbered ID marks (e.g. on SSC memos: 1. A mole on... 2. A scar on...)
            num_mark1 = re.search(r"(?i)\b1\s*[.)]\s*((?:a\s+)?(?:mole|scar|burn|black|cut)[^2\n]+)", line)
            if num_mark1:
                im1 = parse_id_mark(num_mark1.group(1))
                if im1:
                    _add_candidate("id_mark_1", im1, pnum, f"{src_method}:numbered", 0.85)
            num_mark2 = re.search(r"(?i)\b2\s*[.)]\s*((?:a\s+)?(?:mole|scar|burn|black|cut)[^\n]+)", line)
            if num_mark2:
                im2 = parse_id_mark(num_mark2.group(1))
                if im2:
                    _add_candidate("id_mark_2", im2, pnum, f"{src_method}:numbered", 0.85)

            # Licence Number
            if lic_pat:
                m = lic_pat.match(line)
                if m and m.group("value"):
                    lic = parse_licence_no(m.group("value"))
                    if lic:
                        lbl = m.group("label").strip().lower()
                        if re.search(r"\blearner['’]?s?\b|\blearning\b|\bll\b", lbl):
                            lic_kind = "ll"
                        elif re.search(r"\bdriving\b|\bdl\b", lbl):
                            lic_kind = "dl"
                        elif doc_is_ll and not doc_is_dl:
                            lic_kind = "ll"
                        elif doc_is_dl and not doc_is_ll:
                            lic_kind = "dl"
                        else:
                            lic_kind = "unknown"
                        _add_candidate("licence_no", lic, pnum, f"{src_method}:labeled:{lic_kind}", 0.75)

            # Application Number
            if app_pat:
                m = app_pat.search(line)
                if m and m.group("value"):
                    app = parse_application_no(m.group("value"), app_min_digits, app_max_digits)
                    if app:
                        _add_candidate("application_no", app, pnum, f"{src_method}:labeled", 0.75)

            # Standalone "No <digits>" licence fallback
            m = _STANDALONE_LICENCE_RE.match(line)
            if m:
                lic = parse_licence_no(m.group(1))
                if lic and not _standalone_app_context(m.group(1), idx, lines, app_pat, app_digits):
                    _add_candidate("licence_no", lic, pnum, f"{src_method}:standalone-no", 0.55)

            # PAN Number
            pan_cand = None
            if pan_pat:
                m = pan_pat.match(line)
                if m:
                    pan_cand = parse_pan_no(m.group("value") or next_line)
                    if pan_cand:
                        _add_candidate("pan_no", pan_cand, pnum, f"{src_method}:labeled", 0.85)
            if not pan_cand and doc_is_pan:
                pan_cand = parse_pan_no(line)
                if pan_cand:
                    _add_candidate("pan_no", pan_cand, pnum, f"{src_method}:context-pattern", 0.80)

            # Aadhaar Number
            adh_cand = None
            if aadhaar_pat:
                m = aadhaar_pat.match(line)
                if m:
                    adh_cand = parse_aadhaar_no(m.group("value") or next_line)
                    if adh_cand:
                        _add_candidate("aadhaar_no", adh_cand, pnum, f"{src_method}:labeled", 0.85)
            if not adh_cand and doc_is_aadhaar:
                adh_cand = parse_aadhaar_no(line)
                if adh_cand:
                    _add_candidate("aadhaar_no", adh_cand, pnum, f"{src_method}:context-pattern", 0.80)

            # Vehicle Registration Number
            if rc_pat:
                m = rc_pat.match(line)
                if m:
                    reg = parse_vehicle_reg_no(m.group("value") or next_line)
                    if reg:
                        _add_candidate("vehicle_reg_no", reg, pnum, f"{src_method}:labeled", 0.85)

            # RTO Code
            if rto_pat:
                m = rto_pat.match(line)
                if m:
                    rto = parse_rto_code(m.group("value") or next_line)
                    if rto:
                        _add_candidate("rto_code", rto, pnum, f"{src_method}:labeled", 0.75)

        # SSC Document title gives qualification
        if doc_is_ssc and not any(c["field_key"] == "qualification" for c in out):
            _add_candidate("qualification", "10th Standard / SSC", pnum, f"{src_method}:document-title", 0.85)

        # Aadhaar applicant name directly preceding DOB or following header
        if doc_is_aadhaar and not any(c["field_key"] == "full_name" for c in out):
            _AADHAAR_HDR = ("government of india", "unique identification", "authority of india", "aadhaar", "aadhar")
            found_adh_name = False
            # Strategy 1: line right above DOB
            for idx, line in enumerate(lines):
                if (dob_pat and dob_pat.match(line)) or re.search(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}\b", line):
                    for prev_idx in range(idx - 1, max(-1, idx - 4), -1):
                        cand_line = lines[prev_idx]
                        if any(h in cand_line.lower() for h in _AADHAAR_HDR):
                            continue
                        if re.search(r"(?i)\b(?:s/o|d/o|w/o|c/o|son|daughter|wife|care\s+of|father)\b", cand_line):
                            continue
                        cand = extract_name_sequence(cand_line, name_max_words)
                        if cand and len(cand_line) >= 4:
                            _add_candidate("full_name", cand, pnum, f"{src_method}:aadhaar-above-dob", 0.85)
                            found_adh_name = True
                            break
                    if found_adh_name:
                        break
            # Strategy 2: line directly following header
            if not found_adh_name:
                for idx, line in enumerate(lines):
                    line_lower = line.lower()
                    if any(h in line_lower for h in _AADHAAR_HDR):
                        continue
                    if idx > 0 and any(h in lines[idx - 1].lower() for h in _AADHAAR_HDR):
                        if not (stop_pat and stop_pat.match(line)):
                            if not re.search(r"(?i)\b(?:s/o|d/o|w/o|c/o|son|daughter|wife|care\s+of|father)\b", line):
                                adh_name = extract_name_sequence(line, name_max_words)
                                if adh_name:
                                    _add_candidate("full_name", adh_name, pnum, f"{src_method}:aadhaar-header", 0.85)
                                    break

        # Inline Guardian/Father relation e.g. "S/O: Katta Somi Reddy" on Aadhaar back
        if not any(c["field_key"] == "father_name" for c in out):
            _REL_RE = re.compile(
                r"(?i)\b(?:s/o|c/o|d/o|w/o)\s*[:\-]?\s*([A-Za-z\s]+?)(?:,\s*|\d|address|dist|po|pin|near|\n|$)"
            )
            for line in lines:
                m_rel = _REL_RE.search(line)
                if m_rel:
                    rel_name = extract_name_sequence(m_rel.group(1).strip(), father_max_words)
                    if rel_name:
                        _add_candidate("father_name", rel_name, pnum, f"{src_method}:inline-relation", 0.85)
                        break

        # Address Blocks
        for idx, line in enumerate(lines):
            m = addr_pat.match(line) if addr_pat else None
            rest = ""
            if m:
                rest = m.group("value").strip()
            elif addr_pat and re.search(r"(?i)(?<!the\s)(?<!this\s)\baddress\s*[:\-]", line):
                m_sub = re.search(r"(?i)\baddress\s*[:\-]\s*(.*)$", line)
                rest = m_sub.group(1).strip() if m_sub else ""
                if not rest and _PIN6_RE.search(line):
                    rest = re.sub(r"(?i),?\s*address\s*[:\-]?\s*$", "", line).strip()
            else:
                continue
            block = [_tidy_address_segment(rest)] if rest else []
            if not (rest and _PIN6_RE.search(rest)):
                window = lines[idx + 1: idx + 1 + max(addr_max_lines + 3, 8)]
                for offset, nxt in enumerate(window):
                    if stop_pat and stop_pat.match(nxt):
                        if not re.match(r"(?i)^\s*(?:s/o|c/o|d/o|w/o)\b", nxt):
                            break
                    if _AADHAAR_SPACED_RE.search(nxt):
                        break
                    block.append(_tidy_address_segment(nxt))
                    if _PIN6_RE.search(nxt):
                        break
            value = ", ".join(b for b in block if b)[:500]
            if value:
                _add_candidate("address", value, pnum, f"{src_method}:labeled-block", 0.70)
                decomp = decompose_address(value)
                if decomp.get("pincode"):
                    _add_candidate("pres_pincode", decomp["pincode"], pnum, f"{src_method}:address-decomp", 0.85)
                if decomp.get("house_no"):
                    _add_candidate("pres_house_no", decomp["house_no"], pnum, f"{src_method}:address-decomp", 0.85)
                if decomp.get("street"):
                    _add_candidate("pres_street", decomp["street"], pnum, f"{src_method}:address-decomp", 0.80)
                if decomp.get("location"):
                    _add_candidate("pres_location", decomp["location"], pnum, f"{src_method}:address-decomp", 0.80)
                if decomp.get("district"):
                    _add_candidate("pres_district", decomp["district"], pnum, f"{src_method}:address-decomp", 0.80)
                if decomp.get("state"):
                    _add_candidate("pres_state", decomp["state"], pnum, f"{src_method}:address-decomp", 0.80)
                if decomp.get("mandal"):
                    _add_candidate("pres_mandal", decomp["mandal"], pnum, f"{src_method}:address-decomp", 0.75)

    # Address heuristic (clearly marked): PIN line + line above, no label.
    if not any(c["field_key"] == "address" for c in out):
        for pg in pages:
            pnum = pg.get("page", 1)
            src_method = pg.get("method", "text")
            lines = _clean_lines(pg.get("text", ""))
            for idx, line in enumerate(lines):
                if _PIN6_RE.search(line) and idx > 0:
                    prev = _tidy_address_segment(lines[idx - 1])
                    if not prev or (stop_pat and stop_pat.match(prev)):
                        continue
                    value = f"{prev}, {_tidy_address_segment(line)}"[:500]
                    _add_candidate("address", value, pnum, f"{src_method}:heuristic-pin", 0.50)
                    decomp = decompose_address(value)
                    if decomp.get("pincode"):
                        _add_candidate("pres_pincode", decomp["pincode"], pnum, f"{src_method}:address-decomp", 0.80)
                    if decomp.get("house_no"):
                        _add_candidate("pres_house_no", decomp["house_no"], pnum, f"{src_method}:address-decomp", 0.75)
                    if decomp.get("street"):
                        _add_candidate("pres_street", decomp["street"], pnum, f"{src_method}:address-decomp", 0.70)
                    if decomp.get("location"):
                        _add_candidate("pres_location", decomp["location"], pnum, f"{src_method}:address-decomp", 0.70)
                    if decomp.get("district"):
                        _add_candidate("pres_district", decomp["district"], pnum, f"{src_method}:address-decomp", 0.70)
                    if decomp.get("state"):
                        _add_candidate("pres_state", decomp["state"], pnum, f"{src_method}:address-decomp", 0.70)
                    break
    return out
