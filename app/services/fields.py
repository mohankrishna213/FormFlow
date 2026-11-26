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
                "applicant name", "holder name", "candidate's name",
                "candidate name", "student's name", "student name",
                "pupil's name", "name of the candidate", "owner's name",
                "owner name", "purchaser name", "certified that",
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
                "son / daughter / wife of", "son/daughter/wife of",
                "sonwife/daughter of", "father",
                "c/o", "s/o", "d/o", "w/o", "care of",
            ],
            "max_words": 5,
        },
        "dob": {
            "labels": [
                "date of birth", "date of bith", "date of bath",
                "dob", "d0b", "birth date", "born", "d.o.b", "d.o.b.", "birthdate",
            ],
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
            "labels": [
                "temporary registration no", "temporary registration number",
                "temp. reg. no", "temp reg no", "tr no", "tr number",
                "registration no", "registration number", "reg no",
                "regn no", "vehicle no", "vehicle reg no", "rc no",
            ],
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
    head = r"^\s*(?:[0-9/\\#*~.|‘’'\"\-¥£§]+\s*[.,/:\-]?\s*)?" if anchored else r"(?<![A-Za-z0-9])"
    return re.compile(
        rf"(?i){head}(?P<label>{alt})(?![A-Za-z])\s*[:;\-]?\s*(?P<value>.*)$"
    )


_STANDALONE_LICENCE_RE = re.compile(r"^\s*No\.?\s*(\d{6,20})\s*$", re.IGNORECASE)
_ADDR_PIN_EXTEND = 1
_PIN6_RE = re.compile(r"(?<![A-Za-z0-9$#/])([1-9]\d{5})(?!\d)")
_PIN_EXCLUDE_LINE_RE = re.compile(
    r"(?i)\b(?:reference|ref\.?\s*no|badge|licen[cs]e|application|appl|vid|aadhaar|aadhar|valid|issue|dob|d0b|date\s+of)\b"
)
_BILINGUAL_DOB_RE = re.compile(
    r"(?i)(?:date\s*of\s*(?:birth|bith|bath)(?:\s*[/\\Y|]?\s*d\.?\s*[o0]\.?\s*b\.?)?|(?<![A-Za-z])d\.?\s*[o0]\.?\s*b\.?)(?![A-Za-z])\s*[:;\-/]?\s*(?P<value>.+)$"
)
_BILINGUAL_FATHER_RE = re.compile(
    r"(?i)(?:^|[/|│\[\]()\s'\"“”‘’])(?:father(?:'?s)?(?:\s*name)?|s[ou]n[\s/\\]*(?:wife|wile|daughter|osughter)[\s/\\]*(?:daughter|wife|wile)?\s*of|s[ou]m\s*/\s*osughter\s*/\s*wile\s*of)\s*[:;\-]?\s*(?P<value>.+)$"
)
_DL_STATE_LICENCE_RE = re.compile(
    r"\b((?:LLR)?(?:DLF)?(?:AP|AR|AS|BR|CG|CH|DD|DL|DN|GA|GJ|HP|HR|JH|JK|KA|KL|LA|LD|MH|ML|MN|MP|MZ|NL|OD|OR|PB|PY|RJ|SK|TG|TN|TR|TS|UK|UP|WB)[0-9]{2}[0-9A-Z]{8,14})\b"
)

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
        tok = raw.strip(".,;:()[]|<>«»\"'‘’“”¥§~`")
        if _NAME_WORD_RE.fullmatch(tok or "") and tok.lower() not in _NAME_STOPWORDS:
            cur.append(tok)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    # Trim leading/trailing lowercase OCR border artifacts (e.g. "i Devisha Singh i",
    # "'~33 St se Nilanjana Dhar", "MOHAMMED HAROON waren") when core words are capitalized.
    cleaned_runs: list[list[str]] = []
    for r in runs:
        sub = list(r)
        if not all(w.islower() for w in sub):
            # If lowercase junk splits a capitalized prefix from a real name (e.g. St se Nilanjana Dhar),
            # prefer the longest contiguous capitalized subsequence if it has >= 2 words.
            cap_runs: list[list[str]] = []
            ccur: list[str] = []
            for w in sub:
                if w[0].isupper() and (len(w) > 1 or len(ccur) > 0):
                    ccur.append(w)
                else:
                    if ccur:
                        cap_runs.append(ccur)
                        ccur = []
            if ccur:
                cap_runs.append(ccur)
            good_cap = [cr for cr in cap_runs if 2 <= len(cr) <= max_words]
            if good_cap:
                sub = max(good_cap, key=lambda cr: (len(cr), sum(len(x) for x in cr)))
            else:
                while len(sub) > 2 and sub[0].islower():
                    sub = sub[1:]
                while len(sub) > 2 and sub[-1].islower():
                    sub = sub[:-1]
        if 2 <= len(sub) <= max_words:
            alpha_len = sum(sum(1 for ch in w if ch.isalpha()) for w in sub)
            has_real_word = any(sum(1 for ch in w if ch.isalpha()) >= 3 for w in sub)
            if alpha_len >= 4 and has_real_word:
                cleaned_runs.append(sub)
    if not cleaned_runs:
        return None

    def _run_score(cr: list[str]) -> tuple[int, int, int, int]:
        has_cap = 1 if any(w[0].isupper() for w in cr) else 0
        all_cap = 1 if all(w[0].isupper() for w in cr) else 0
        long_chars = sum(len(w) for w in cr if len(w) >= 3)
        real_words = sum(1 for w in cr if len(w) >= 3)
        return (has_cap, all_cap, long_chars, real_words)

    best = max(cleaned_runs, key=_run_score)
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


def parse_date_token(token: str, indian_dmy: bool = False) -> tuple[str, bool, str] | None:
    """Return (value, ambiguous, kind). Rejects year-only tokens.

    When ``indian_dmy=True`` (Indian ID cards/certificates: Aadhaar, SSC memo,
    DL, LL, PAN, TR) or when a matching month word appears alongside the numeric
    date on the same line (e.g. SSC '05/07/2005 ZERO FIVE - JUL - ...'), DD/MM/YYYY
    is unambiguously normalized to ISO ``YYYY-MM-DD``.
    """
    t = token.strip()
    m = _DATE_RES[0].search(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        parsed = parse_dmy(d, mo, y)
        if parsed is None:
            return None
        iso, ambiguous = parsed
        if ambiguous:
            if indian_dmy:
                return iso, False, "dmy"
            # Check if the same line also spells out the month matching ``mo`` (e.g. JUL)
            for mw, mnum in MONTHS.items():
                if mnum == mo and re.search(rf"(?<![A-Za-z]){re.escape(mw)}(?![A-Za-z])", t, re.IGNORECASE):
                    return iso, False, "dmy"
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


_VEHICLE_REG_RE = re.compile(
    r"\b([A-Z]{2}[\s\-]?[0-9]{1,2}[\s\-]?(?:TR[\s\-]?[0-9A-Z]{4,10}|[A-Z]{1,3}[\s\-]?[0-9]{4}))\b"
)


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

    raw = re.sub(r"(?i)\b(?:permanent|present)\s+address\s+if\s+different\s+from\s+present\s*(?:address)?\b", "", raw)
    raw = re.sub(r"(?i)\bif\s+different\s+from\s+present\s*(?:address)?\b", "", raw)
    raw = re.sub(r"(?i)\bdifferent\s+from\s+present\b", "", raw)
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


def _detect_doc_type_flags(pages: list[dict], all_pages_text: str) -> dict[str, bool]:
    """Detect Indian document families from OCR text to guide layout & date parsing."""
    doc_is_ll = bool(re.search(r"\bform[\s\-]*3\b|\blearner['’]?s?\s*lic[ea]n[cs]e|\blearning\s*lic[ea]n[cs]e", all_pages_text))
    doc_is_dl = bool(re.search(r"\bform[\s\-]*7\b|\bdriving\s*lic[ea]n[cs]e|\bindian\s+union\s+driving\b", all_pages_text))
    doc_is_pan = bool(re.search(r"\bincome\s*tax\s*department\b|\bpermanent\s*account\s*number\b|\bpan\s*card\b", all_pages_text))
    has_12digit_uid = any(parse_aadhaar_no(ln) for pg in pages for ln in _clean_lines(pg.get("text", "")))
    has_aadhaar_cosignal = bool(re.search(
        r"\bgovernment\s+of\s+india\b|\bvid\s*[:;]?|\b(?:male|female|transgender)\b|\bd\.?[o0]\.?b\b|\bdate\s*of\s*birth\b|\byear\s*of\s*birth\b",
        all_pages_text,
    ))
    doc_is_aadhaar = bool(
        re.search(r"\bunique\s*identification\b|\baadhaar\b|\baadhar\b|\buidai\b", all_pages_text)
        or (has_12digit_uid and has_aadhaar_cosignal and not doc_is_dl and not doc_is_ll and not doc_is_pan)
    )
    doc_is_ssc = bool(
        re.search(r"\bsecondary\s*school\s*certificate\b|\bssc\s*(?:examination|public)\b|\bboard\s*of\s*secondary\b|\bmarks\s*memo\b|\bcumulative\s*grade\b", all_pages_text)
        or (
            sum(1 for kw in ("telugu", "hindi", "english", "maths", "mathematics", "science", "social", "grade", "cgpa", "school", "marks")
                if re.search(rf"\b{kw}\b", all_pages_text)) >= 4
        )
    )
    doc_is_tr = bool(
        re.search(r"\btemporary\s*(?:certificate\s*of\s*)?reg(?:istration|n)?\b|\btemp\.?\s*reg\b|\btr\s*no\b|\bform\s*19\b|\bform\s*20\b|\bsale\s*certificate\b", all_pages_text)
    )
    return {
        "ll": doc_is_ll,
        "dl": doc_is_dl,
        "pan": doc_is_pan,
        "aadhaar": doc_is_aadhaar,
        "ssc": doc_is_ssc,
        "tr": doc_is_tr,
        "indian": bool(doc_is_aadhaar or doc_is_ssc or doc_is_dl or doc_is_ll or doc_is_pan or doc_is_tr),
    }


_ADDR_DECOMP_CONF: tuple[tuple[str, str, float, float], ...] = (
    ("pincode", "pres_pincode", 0.85, 0.80),
    ("house_no", "pres_house_no", 0.85, 0.75),
    ("street", "pres_street", 0.80, 0.70),
    ("location", "pres_location", 0.80, 0.70),
    ("district", "pres_district", 0.80, 0.70),
    ("state", "pres_state", 0.80, 0.70),
    ("mandal", "pres_mandal", 0.75, 0.0),
)


def _emit_address_decomp(add_fn, address_value: str, pnum: int, src_method: str, *, heuristic: bool = False) -> None:
    decomp = decompose_address(address_value)
    for src_k, field_k, conf_lbl, conf_heur in _ADDR_DECOMP_CONF:
        conf = conf_heur if heuristic else conf_lbl
        if conf > 0 and decomp.get(src_k):
            add_fn(field_k, decomp[src_k], pnum, f"{src_method}:address-decomp", conf)


def _extract_ssc_fallbacks(
    lines: list[str],
    pnum: int,
    src_method: str,
    name_max_words: int,
    father_max_words: int,
    has_key,
    add_fn,
) -> None:
    """OCR-tolerant fallbacks for SSC 10th memos (Telangana/AP colored grade memos)."""
    if not has_key("qualification"):
        add_fn("qualification", "10th Standard / SSC", pnum, f"{src_method}:document-title", 0.85)
    ssc_cand_re = re.compile(r"(?i)^\s*cand[a-z0-9'’.\-]{2,12}[a-z]{2,4}\s*[:;\-]?\s*(?P<value>.+)$")
    ssc_father_re = re.compile(r"(?i)^\s*(?:fath|fat|pat)[a-z0-9'’.\-]{1,6}\s+n[a-z]{2,4}\s*[:;\-]?\s*(?P<value>.+)$")
    ssc_dob_re = re.compile(r"(?i)\bdate\s+of\s+[a-z0-9]{3,7}\s*[:;\-]?\s*(?P<value>.+)$")
    ssc_name_idx: int | None = None
    for idx, line in enumerate(lines):
        next_line = lines[idx + 1] if idx + 1 < len(lines) else ""
        if not has_key("full_name"):
            m_sc = ssc_cand_re.match(line)
            if m_sc:
                nm = extract_name_sequence(m_sc.group("value").strip() or next_line, name_max_words)
                if nm:
                    add_fn("full_name", nm, pnum, f"{src_method}:ssc-fuzzy-label", 0.82)
                    ssc_name_idx = idx
        if not has_key("father_name"):
            m_sf = ssc_father_re.match(line)
            if m_sf:
                fnm = extract_name_sequence(m_sf.group("value").strip() or next_line, father_max_words)
                if fnm:
                    add_fn("father_name", fnm, pnum, f"{src_method}:ssc-fuzzy-label", 0.82)
            elif ssc_name_idx is not None and idx == ssc_name_idx + 1:
                m_fn = re.match(r"(?i)^\s*\S+\s+name\s*[:;\-]?\s*(?P<value>.+)$", line)
                if m_fn:
                    fnm = extract_name_sequence(m_fn.group("value").strip(), father_max_words)
                    if fnm:
                        add_fn("father_name", fnm, pnum, f"{src_method}:ssc-below-name", 0.80)
        if not has_key("dob"):
            m_sd = ssc_dob_re.search(line)
            if m_sd:
                parsed = parse_date_token(m_sd.group("value").strip(), indian_dmy=True) or (
                    parse_date_token(next_line, indian_dmy=True) if next_line else None
                )
                if parsed:
                    val, ambiguous, kind = parsed
                    add_fn("dob", val, pnum, f"{src_method}:ssc-fuzzy-{kind}", 0.40 if ambiguous else 0.82)
    if not has_key("dob"):
        for line in lines[:15]:
            if re.search(r"(?i)\b(?:issue|issued|exam|held|march|april|may|june|print|download)\b", line):
                continue
            parsed = parse_date_token(line, indian_dmy=True)
            if parsed:
                val, ambiguous, kind = parsed
                add_fn("dob", val, pnum, f"{src_method}:ssc-header-{kind}", 0.40 if ambiguous else 0.80)
                break


def _extract_aadhaar_name_fallback(
    lines: list[str],
    pnum: int,
    src_method: str,
    name_max_words: int,
    dob_pat: re.Pattern | None,
    stop_pat: re.Pattern | None,
    add_fn,
) -> None:
    """Extract Aadhaar applicant name preceding DOB/Father or following the header."""
    aadhaar_hdr = ("government of india", "unique identification", "authority of india", "aadhaar", "aadhar")
    found = False
    for idx, line in enumerate(lines):
        has_year_or_date = bool(re.search(r"\b(?:19|20)\d{2}\b", line))
        is_disclaimer = bool(re.search(r"(?i)\b(?:citizenship|proof\s+of\s+identity|not\s+of)\b", line))
        is_anchor = (not is_disclaimer) and bool(
            (has_year_or_date and (
                (dob_pat and dob_pat.match(line))
                or _BILINGUAL_DOB_RE.search(line)
                or re.search(r"(?i)\byear\s*of\s*birth\b", line)
                or re.search(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}\b", line)
            ))
            or _BILINGUAL_FATHER_RE.search(line)
        )
        if not is_anchor:
            continue
        for prev_idx in range(idx - 1, max(-1, idx - 4), -1):
            cand_line = lines[prev_idx]
            if any(h in cand_line.lower() for h in aadhaar_hdr):
                continue
            if re.search(r"(?i)\b(?:s/o|d/o|w/o|c/o|son|daughter|wife|care\s+of|father|citizenship|proof\s+of)\b", cand_line):
                continue
            cand = extract_name_sequence(cand_line, name_max_words)
            if cand and len(cand) >= 5 and any(len(w) >= 3 for w in cand.split()) and not cand_line.islower():
                add_fn("full_name", cand, pnum, f"{src_method}:aadhaar-above-dob", 0.85)
                found = True
                break
        if not found:
            for prev_idx in range(idx - 1, max(-1, idx - 3), -1):
                cand_line = lines[prev_idx]
                if any(h in cand_line.lower() for h in aadhaar_hdr):
                    continue
                toks = [t.strip(".,;:()[]|<>\"'‘’~") for t in cand_line.split()]
                toks = [t for t in toks if re.fullmatch(r"[A-Z][a-z]{3,20}", t) and t.lower() not in _NAME_STOPWORDS]
                if len(toks) == 1:
                    add_fn("full_name", toks[0], pnum, f"{src_method}:aadhaar-single-name", 0.72)
                    found = True
                    break
        if found:
            break

    if not found:
        for idx, line in enumerate(lines):
            if any(h in line.lower() for h in aadhaar_hdr):
                continue
            if idx > 0 and any(h in lines[idx - 1].lower() for h in aadhaar_hdr):
                if not (stop_pat and stop_pat.match(line)) and not re.search(
                    r"(?i)\b(?:s/o|d/o|w/o|c/o|son|daughter|wife|care\s+of|father)\b", line
                ):
                    adh_name = extract_name_sequence(line, name_max_words)
                    if adh_name:
                        add_fn("full_name", adh_name, pnum, f"{src_method}:aadhaar-header", 0.85)
                        break


def _extract_aadhaar_pin_state(lines: list[str], pnum: int, src_method: str, has_key, add_fn) -> None:
    """Fill missing State and PIN Code from crisp e-Aadhaar letter blocks."""
    if not has_key("pres_pincode"):
        for line in lines:
            m_pin = re.search(r"(?i)\bpin\s*(?:code)?\s*[:;\-]?\s*([1-9]\d{5})\b", line)
            if not m_pin:
                for st in INDIAN_STATES:
                    m_st_pin = re.search(rf"(?i)\b{re.escape(st)}\s*[-–:,]?\s*([1-9]\d{{5}})\b", line)
                    if m_st_pin:
                        m_pin = m_st_pin
                        break
            if m_pin:
                add_fn("pres_pincode", m_pin.group(1), pnum, f"{src_method}:aadhaar-pin", 0.85)
                break
    if not has_key("pres_state"):
        for line in lines:
            if re.search(r"(?i)\b(?:state|dist|district|pin)\b", line):
                for st in INDIAN_STATES:
                    if re.search(rf"(?i)\b{re.escape(st)}\b", line):
                        add_fn("pres_state", st, pnum, f"{src_method}:aadhaar-state", 0.82)
                        return


def _extract_dl_name_fallback(
    lines: list[str], pnum: int, src_method: str, name_max_words: int, has_key, add_fn,
) -> None:
    """Extract Name & Father below DL number/state header on unlabeled Telangana paper DLs."""
    for idx, line in enumerate(lines):
        if not (
            _DL_STATE_LICENCE_RE.search(line.upper().replace(" ", ""))
            or re.search(r"(?i)\btelangana\s+state\b", line)
        ):
            continue
        found_names: list[str] = []
        for nxt_idx in range(idx + 1, min(len(lines), idx + 5)):
            cand_line = lines[nxt_idx]
            if re.search(r"\d", cand_line):
                if found_names:
                    break
                continue
            if re.search(r"(?i)\b(?:near|road|street|nagar|colony|school|mandal|dist|telangana|driving|license|licence|transport|validity)\b", cand_line):
                break
            nm = extract_name_sequence(cand_line, name_max_words)
            if nm and all(w.isupper() for w in nm.split() if len(w) > 1):
                found_names.append(nm)
                if len(found_names) == 2:
                    break
        if found_names:
            add_fn("full_name", found_names[0], pnum, f"{src_method}:dl-below-id", 0.78)
            if len(found_names) >= 2 and not has_key("father_name"):
                add_fn("father_name", found_names[1], pnum, f"{src_method}:dl-below-name", 0.75)
            break


_LABEL_RESIDUE_RE = re.compile(
    r"(?i)^[\s'’\"./\\:\-|]*(?:(?:s|es)\s+)?(?:na(?:m|me)?|fath(?:er)?(?:'?s)?\s*na(?:m|me)?|of\s+(?:the\s+)?(?:applicant|holder|father))[\s'’\"./\\:\-|]*$"
)
_PAN_HEADER_SKIP_RE = re.compile(
    r"(?i)\b(?:income\s*tax|department|govt\.?\s*of\s*india|government\s*of\s*india|"
    r"permanent\s*account|account\s*number|signature|date\s*of\s*birth|dob|card|"
    r"father(?:'?s)?\s*na(?:m|me)?|holder|applicant)\b"
)


def _extract_pan_fallbacks(
    lines: list[str], pnum: int, src_method: str, name_max_words: int, father_max_words: int, has_key, add_fn,
) -> None:
    """Extract Name & Father's Name on both bilingual and old unlabeled Indian PAN cards."""
    if has_key("full_name") and has_key("father_name"):
        return

    def _clean_pan_name_line(raw: str, max_w: int) -> str | None:
        if not raw or any(ch.isdigit() for ch in raw):
            return None
        if _PAN_HEADER_SKIP_RE.search(raw):
            return None
        nm = extract_name_sequence(raw, max_w)
        if not nm:
            return None
        words = nm.split()
        # PAN card names are printed in uppercase (allow single-letter initials)
        if not all(w.isupper() for w in words if len(w) > 1):
            return None
        return nm

    # Pass 1: Bilingual/garbled PAN labels (e.g. "... / Name", "... / Father's Na")
    for idx, line in enumerate(lines):
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        if not has_key("full_name") and re.search(r"(?i)(?:^|[/|\\])\s*name\s*$", line):
            nm = _clean_pan_name_line(nxt, name_max_words)
            if nm:
                add_fn("full_name", nm, pnum, f"{src_method}:pan-bilingual-next", 0.83)
        if not has_key("father_name") and re.search(r"(?i)father(?:'?s|\s+'?s)?\s*na(?:m|me)?", line):
            nm = _clean_pan_name_line(nxt, father_max_words)
            if nm:
                add_fn("father_name", nm, pnum, f"{src_method}:pan-bilingual-father-next", 0.82)

    if has_key("full_name") and has_key("father_name"):
        return

    # Pass 2: Positional fallback between header/PAN number and DOB/Signature
    start_idx = 0
    end_idx = len(lines)
    for idx, line in enumerate(lines):
        if re.search(r"(?i)\b(?:income\s*tax|govt\.?\s*of\s*india|permanent\s*account)\b", line) or _PAN_RE.search(line.upper()):
            start_idx = max(start_idx, idx + 1)
        if idx > start_idx and (
            re.search(r"(?i)\b(?:signature|date\s*of\s*birth|dob)\b", line)
            or re.search(r"\b\d{2}[/-]\d{2}[/-]\d{4}\b", line)
        ):
            end_idx = min(end_idx, idx)
            break

    found: list[str] = []
    for line in lines[start_idx:end_idx]:
        nm = _clean_pan_name_line(line, name_max_words)
        if nm and nm.lower() not in {f.lower() for f in found}:
            found.append(nm)
            if len(found) == 2:
                break

    if found:
        if not has_key("full_name"):
            add_fn("full_name", found[0], pnum, f"{src_method}:pan-positional", 0.82)
        if len(found) >= 2 and not has_key("father_name"):
            add_fn("father_name", found[1], pnum, f"{src_method}:pan-positional-father", 0.80)

    # Pass 3: Old PAN cards print DOB standalone without a "Date of Birth" label
    if not has_key("dob"):
        for line in lines[start_idx:]:
            m_dt = re.search(r"\b(\d{2}[/-]\d{2}[/-]\d{4})\b", line)
            if m_dt:
                parsed = parse_date_token(m_dt.group(1), indian_dmy=True)
                if parsed:
                    val, ambiguous, kind = parsed
                    add_fn("dob", val, pnum, f"{src_method}:pan-standalone-{kind}", 0.40 if ambiguous else 0.82)
                    break


def _extract_address_blocks(
    lines: list[str],
    pnum: int,
    src_method: str,
    addr_pat: re.Pattern | None,
    stop_pat: re.Pattern | None,
    addr_max_lines: int,
    add_fn,
) -> None:
    for idx, line in enumerate(lines):
        m = addr_pat.match(line) if addr_pat else None
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
            for nxt in window:
                if stop_pat and stop_pat.match(nxt) and not re.match(r"(?i)^\s*(?:s/o|c/o|d/o|w/o)\b", nxt):
                    break
                if _AADHAAR_SPACED_RE.search(nxt):
                    break
                block.append(_tidy_address_segment(nxt))
                if _PIN6_RE.search(nxt):
                    break
        value = ", ".join(b for b in block if b)[:500]
        if value:
            add_fn("address", value, pnum, f"{src_method}:labeled-block", 0.70)
            _emit_address_decomp(add_fn, value, pnum, src_method, heuristic=False)


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
    doc_flags = _detect_doc_type_flags(pages, all_pages_text)
    doc_is_ll = doc_flags["ll"]
    doc_is_dl = doc_flags["dl"]
    doc_is_pan = doc_flags["pan"]
    doc_is_aadhaar = doc_flags["aadhaar"]
    doc_is_ssc = doc_flags["ssc"]
    doc_is_tr = doc_flags["tr"]
    is_indian_doc = doc_flags["indian"]

    seen_entries: set[tuple[str, str]] = set()
    present_keys: set[str] = set()

    def _has_key(key: str) -> bool:
        return key in present_keys

    def _add_candidate(key: str, val: str | None, pnum: int, method: str, conf: float) -> None:
        if not val or not str(val).strip():
            return
        clean_val = str(val).strip()
        pair = (key, clean_val.lower())
        if pair in seen_entries:
            return
        seen_entries.add(pair)
        present_keys.add(key)
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
                    if (not val_str or len(val_str) <= 2 or _LABEL_RESIDUE_RE.match(val_str)) and next_line and not next_is_stop:
                        val_str = next_line
                    label = m.group("label").strip().lower()
                    relation = label == "name" and re.match(r"(?i)^\s*of\b", val_str)
                    suppressed = label == "name" and specific_name_found
                    if not relation and not suppressed:
                        name = extract_name_sequence(val_str, name_max_words)
                        if name:
                            _add_candidate("full_name", name, pnum, f"{src_method}:labeled", 0.85)
                elif doc_is_dl:
                    m_dl_name = re.match(r"^\s*['\"‘’`|:;.\-~]+\s*ame\s*[:;\-]\s*(?P<value>.+)$", line, re.IGNORECASE)
                    if m_dl_name:
                        name = extract_name_sequence(m_dl_name.group("value"), name_max_words)
                        if name:
                            _add_candidate("full_name", name, pnum, f"{src_method}:dl-clipped-label", 0.80)

            # Father's Name
            father_matched = False
            if father_pat:
                m = father_pat.match(line)
                if m:
                    val_str = m.group("value").strip()
                    if (not val_str or len(val_str) <= 2 or _LABEL_RESIDUE_RE.match(val_str)) and next_line and not next_is_stop:
                        val_str = next_line
                    fname = extract_name_sequence(val_str, father_max_words)
                    if fname:
                        _add_candidate("father_name", fname, pnum, f"{src_method}:labeled", 0.85)
                        father_matched = True
            if not father_matched:
                m_bf = _BILINGUAL_FATHER_RE.search(line)
                if m_bf:
                    val_str = m_bf.group("value").strip()
                    if (not val_str or len(val_str) <= 2 or _LABEL_RESIDUE_RE.match(val_str)) and next_line and not next_is_stop:
                        val_str = next_line
                    fname = extract_name_sequence(val_str, father_max_words)
                    if fname:
                        _add_candidate("father_name", fname, pnum, f"{src_method}:bilingual-labeled", 0.82)

            # Date of Birth
            dob_matched = False
            if dob_pat:
                m = dob_pat.match(line)
                if m:
                    parsed = parse_date_token(m.group("value").strip(), indian_dmy=is_indian_doc)
                    if not parsed and next_line and not next_is_stop:
                        parsed = parse_date_token(next_line, indian_dmy=is_indian_doc)
                    if parsed:
                        val, ambiguous, kind = parsed
                        _add_candidate("dob", val, pnum, f"{src_method}:labeled-{kind}", 0.40 if ambiguous else 0.85)
                        dob_matched = True
            if not dob_matched:
                m_bd = _BILINGUAL_DOB_RE.search(line)
                if m_bd:
                    parsed = parse_date_token(m_bd.group("value").strip(), indian_dmy=is_indian_doc)
                    if not parsed and next_line and not next_is_stop:
                        parsed = parse_date_token(next_line, indian_dmy=is_indian_doc)
                    if parsed:
                        val, ambiguous, kind = parsed
                        _add_candidate("dob", val, pnum, f"{src_method}:bilingual-{kind}", 0.40 if ambiguous else 0.82)

            # Gender
            if gender_pat:
                m = gender_pat.match(line)
                if m:
                    val_str = m.group("value").strip() or (next_line if not next_is_stop else "")
                    g = parse_gender(val_str)
                    if g:
                        _add_candidate("gender", g, pnum, f"{src_method}:labeled", 0.85)
            if not _has_key("gender"):
                if re.fullmatch(r"(?i)\s*(MALE|FEMALE|TRANSGENDER)\s*", line):
                    g = parse_gender(line)
                    if g:
                        _add_candidate("gender", g, pnum, f"{src_method}:standalone", 0.80)
                elif len(line.split()) <= 6:
                    m_bg = re.search(r"(?i)(?<![A-Za-z])(TRANSGENDER|FEMALE|MALE)(?![A-Za-z])", line)
                    if m_bg:
                        g = parse_gender(m_bg.group(1))
                        if g:
                            _add_candidate("gender", g, pnum, f"{src_method}:bilingual-standalone", 0.78)

            # Mobile, Email, Blood Group, Qualification, Place of Birth, ID Marks
            if mob_pat and (m := mob_pat.match(line)):
                _add_candidate("mobile_no", parse_mobile_no(m.group("value") or next_line), pnum, f"{src_method}:labeled", 0.85)
            elif not mob_pat and re.match(r"(?i)^\s*(?:\+91|ph|mob|cell)[\s.:\-]", line):
                _add_candidate("mobile_no", parse_mobile_no(line), pnum, f"{src_method}:prefix", 0.80)

            if email_pat and (m := email_pat.match(line)):
                _add_candidate("email", parse_email(m.group("value") or next_line), pnum, f"{src_method}:labeled", 0.85)
            elif not email_pat and _EMAIL_RE.search(line):
                _add_candidate("email", parse_email(line), pnum, f"{src_method}:pattern", 0.80)

            if blood_pat and (m := blood_pat.match(line)):
                _add_candidate("blood_group", parse_blood_group(m.group("value") or next_line), pnum, f"{src_method}:labeled", 0.85)
            if qual_pat and (m := qual_pat.match(line)):
                _add_candidate("qualification", parse_qualification(m.group("value") or next_line, matched_label=m.group("label")), pnum, f"{src_method}:labeled", 0.85)
            if pob_pat and (m := pob_pat.match(line)):
                _add_candidate("place_of_birth", parse_place_of_birth(m.group("value") or next_line), pnum, f"{src_method}:labeled", 0.80)
            if id_mark_1_pat and (m := id_mark_1_pat.match(line)):
                _add_candidate("id_mark_1", parse_id_mark(m.group("value") or next_line), pnum, f"{src_method}:labeled", 0.85)
            if id_mark_2_pat and (m := id_mark_2_pat.match(line)):
                _add_candidate("id_mark_2", parse_id_mark(m.group("value") or next_line), pnum, f"{src_method}:labeled", 0.85)

            if (num_mark1 := re.search(r"(?i)\b1\s*[.)]\s*((?:a\s+)?(?:mole|scar|burn|black|cut)[^2\n]+)", line)):
                _add_candidate("id_mark_1", parse_id_mark(num_mark1.group(1)), pnum, f"{src_method}:numbered", 0.85)
            if (num_mark2 := re.search(r"(?i)\b2\s*[.)]\s*((?:a\s+)?(?:mole|scar|burn|black|cut)[^\n]+)", line)):
                _add_candidate("id_mark_2", parse_id_mark(num_mark2.group(1)), pnum, f"{src_method}:numbered", 0.85)

            # Licence Number & Application Number
            if lic_pat and (m := lic_pat.match(line)) and m.group("value"):
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

            if app_pat and (m := app_pat.search(line)) and m.group("value"):
                _add_candidate("application_no", parse_application_no(m.group("value"), app_min_digits, app_max_digits), pnum, f"{src_method}:labeled", 0.75)

            if (m := _STANDALONE_LICENCE_RE.match(line)):
                lic = parse_licence_no(m.group(1))
                if lic and not _standalone_app_context(m.group(1), idx, lines, app_pat, app_digits):
                    _add_candidate("licence_no", lic, pnum, f"{src_method}:standalone-no", 0.55)

            if (doc_is_dl or doc_is_ll) and not _has_key("licence_no") and not re.search(r"(?i)\b(?:reference|ref\.?\s*no)\b", line):
                if (m_dlid := _DL_STATE_LICENCE_RE.search(line.upper().replace(" ", ""))):
                    dl_val = parse_licence_no(m_dlid.group(1))
                    if dl_val:
                        lic_kind = "ll" if (doc_is_ll and not doc_is_dl) or dl_val.startswith("LLR") else "dl"
                        _add_candidate("licence_no", dl_val, pnum, f"{src_method}:context-dl:{lic_kind}", 0.78)

            # ID numbers (PAN, Aadhaar, Vehicle Reg/TR, RTO)
            pan_cand = parse_pan_no(m.group("value") or next_line) if (pan_pat and (m := pan_pat.match(line))) else None
            if pan_cand:
                _add_candidate("pan_no", pan_cand, pnum, f"{src_method}:labeled", 0.85)
            elif doc_is_pan:
                _add_candidate("pan_no", parse_pan_no(line), pnum, f"{src_method}:context-pattern", 0.80)

            adh_cand = parse_aadhaar_no(m.group("value") or next_line) if (aadhaar_pat and (m := aadhaar_pat.match(line))) else None
            if adh_cand:
                _add_candidate("aadhaar_no", adh_cand, pnum, f"{src_method}:labeled", 0.85)
            elif doc_is_aadhaar:
                _add_candidate("aadhaar_no", parse_aadhaar_no(line), pnum, f"{src_method}:context-pattern", 0.80)

            tr_cand = parse_vehicle_reg_no(m.group("value") or next_line) if (rc_pat and (m := rc_pat.match(line))) else None
            if tr_cand:
                _add_candidate("vehicle_reg_no", tr_cand, pnum, f"{src_method}:labeled", 0.85)
            elif doc_is_tr:
                _add_candidate("vehicle_reg_no", parse_vehicle_reg_no(line), pnum, f"{src_method}:context-pattern", 0.80)

            if rto_pat and (m := rto_pat.match(line)):
                _add_candidate("rto_code", parse_rto_code(m.group("value") or next_line), pnum, f"{src_method}:labeled", 0.75)

        # Document-family layout fallbacks
        if doc_is_ssc:
            _extract_ssc_fallbacks(lines, pnum, src_method, name_max_words, father_max_words, _has_key, _add_candidate)
        if doc_is_pan:
            _extract_pan_fallbacks(lines, pnum, src_method, name_max_words, father_max_words, _has_key, _add_candidate)
        if doc_is_aadhaar and not _has_key("full_name"):
            _extract_aadhaar_name_fallback(lines, pnum, src_method, name_max_words, dob_pat, stop_pat, _add_candidate)
        if doc_is_dl and not _has_key("full_name"):
            _extract_dl_name_fallback(lines, pnum, src_method, name_max_words, _has_key, _add_candidate)

        if not _has_key("father_name"):
            rel_re = re.compile(r"(?i)\b(?:s/o|c/o|d/o|w/o)\s*[:\-]?\s*([A-Za-z\s]+?)(?:,\s*|\d|address|dist|po|pin|near|\n|$)")
            for line in lines:
                if (m_rel := rel_re.search(line)):
                    rel_name = extract_name_sequence(m_rel.group(1).strip(), father_max_words)
                    if rel_name:
                        _add_candidate("father_name", rel_name, pnum, f"{src_method}:inline-relation", 0.85)
                        break

        _extract_address_blocks(lines, pnum, src_method, addr_pat, stop_pat, addr_max_lines, _add_candidate)
        if doc_is_aadhaar:
            _extract_aadhaar_pin_state(lines, pnum, src_method, _has_key, _add_candidate)

    # Address heuristic (clearly marked): PIN line + line above, no label.
    if not _has_key("address"):
        for pg in pages:
            pnum = pg.get("page", 1)
            src_method = pg.get("method", "text")
            lines = _clean_lines(pg.get("text", ""))
            for idx, line in enumerate(lines):
                if _PIN_EXCLUDE_LINE_RE.search(line):
                    continue
                if _PIN6_RE.search(line) and idx > 0:
                    prev = _tidy_address_segment(lines[idx - 1])
                    if not prev or _PIN_EXCLUDE_LINE_RE.search(prev) or (stop_pat and stop_pat.match(prev)):
                        continue
                    value = f"{prev}, {_tidy_address_segment(line)}"[:500]
                    _add_candidate("address", value, pnum, f"{src_method}:heuristic-pin", 0.50)
                    _emit_address_decomp(_add_candidate, value, pnum, src_method, heuristic=True)
                    break
    return out
