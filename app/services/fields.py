"""Deterministic candidate-field extraction (Phase 4).

Rules are configurable via ocr_rules.json and easy to extend: labels per
field, date handling, address heuristics. Parsers never invent values;
missing stays missing, ambiguous stays low-confidence.

Implemented field keys: full_name, dob, address.
Heuristic-only (clearly marked): nothing else -- new keys arrive as explicit
parsers, not silent guesses.
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
    "min_page_chars": 30,
    "binarize": False,
    "fields": {
        "full_name": {
            "labels": ["full name", "applicant name", "holder name", "name"],
            "max_words": 5,
        },
        "dob": {"labels": ["date of birth", "dob", "birth date", "born"]},
        "address": {
            "labels": ["permanent address", "present address", "address", "addr", "residence"],
            "max_lines": 3,
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
    for key in ("dpi", "lang", "min_page_chars", "binarize", "version"):
        if key in raw and raw[key] not in (None, ""):
            rules[key] = raw[key]
    if isinstance(raw.get("fields"), dict):
        for fname, fval in raw["fields"].items():
            if isinstance(fval, dict):
                rules["fields"].setdefault(fname, {}).update(fval)
    return rules


def _label_pattern(labels: list[str]) -> re.Pattern:
    ordered = sorted(labels, key=len, reverse=True)
    return re.compile(r"(?i)^\s*(?:" + "|".join(re.escape(l) for l in ordered) + r")\s*[:\-]?\s*(.*)$")


def _clean_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


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


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[.'\"\-]", "", value.lower())).strip()


def parse_dmy(day: int, month: int, year: int) -> tuple[str, bool] | None:
    """Return (ISO date, ambiguous). None if out of range."""
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
    """Return (value, ambiguous, kind). Ambiguous keeps the RAW token."""
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


def extract_candidates(pages: list[dict], rules: dict[str, Any]) -> list[dict]:
    """Deterministic parse of page texts. Never invents missing values."""
    out: list[dict] = []
    fields = rules.get("fields", {})
    name_labels = fields.get("full_name", {}).get("labels", [])
    dob_labels = fields.get("dob", {}).get("labels", [])
    addr_labels = fields.get("address", {}).get("labels", [])
    addr_max_lines = int(fields.get("address", {}).get("max_lines", 3))
    max_words = int(fields.get("full_name", {}).get("max_words", 5))
    name_pat = _label_pattern(name_labels) if name_labels else None
    dob_pat = _label_pattern(dob_labels) if dob_labels else None
    addr_pat = _label_pattern(addr_labels) if addr_labels else None
    stop_pat = _label_pattern(name_labels + dob_labels + addr_labels)

    for pg in pages:
        pnum = pg.get("page", 1)
        src_method = pg.get("method", "text")
        lines = _clean_lines(pg.get("text", ""))
        for idx, line in enumerate(lines):
            if name_pat:
                m = name_pat.match(line)
                if m and m.group(1):
                    name = parse_name(m.group(1), max_words)
                    if name:
                        out.append({"field_key": "full_name", "value": name,
                                    "source_page": pnum, "method": f"{src_method}:labeled",
                                    "confidence": 0.85})
            if dob_pat:
                m = dob_pat.match(line)
                if m and m.group(1):
                    parsed = parse_date_token(m.group(1))
                    if parsed:
                        val, ambiguous, kind = parsed
                        out.append({"field_key": "dob", "value": val,
                                    "source_page": pnum,
                                    "method": f"{src_method}:labeled-{kind}",
                                    "confidence": 0.4 if ambiguous else 0.85})
        # Address: labeled block of following lines (multi-line kept together).
        for idx, line in enumerate(lines):
            if addr_pat and addr_pat.match(line):
                rest = addr_pat.match(line).group(1).strip()
                block = [rest] if rest else []
                for nxt in lines[idx + 1: idx + 1 + addr_max_lines]:
                    if stop_pat and stop_pat.match(nxt):
                        break
                    block.append(nxt)
                    if re.search(r"\b\d{6}\b", nxt):
                        break
                value = ", ".join(b for b in block if b)[:500]
                if value:
                    out.append({"field_key": "address", "value": value,
                                "source_page": pnum, "method": f"{src_method}:labeled-block",
                                "confidence": 0.7})
    # Address heuristic (clearly marked): PIN line + line above, no label.
    if not any(c["field_key"] == "address" for c in out):
        for pg in pages:
            pnum = pg.get("page", 1)
            src_method = pg.get("method", "text")
            lines = _clean_lines(pg.get("text", ""))
            for idx, line in enumerate(lines):
                if re.search(r"\b\d{6}\b", line) and idx > 0:
                    prev = lines[idx - 1]
                    if stop_pat and stop_pat.match(prev):
                        continue
                    value = f"{prev}, {line}"[:500]
                    out.append({"field_key": "address", "value": value,
                                "source_page": pnum, "method": f"{src_method}:heuristic-pin",
                                "confidence": 0.5})
                    break
    return out
