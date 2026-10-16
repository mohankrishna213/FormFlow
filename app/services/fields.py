"""Deterministic candidate-field extraction (Phase 4, extended in Phase 3).

Rules are configurable via ocr_rules.json and easy to extend: labels per
field, date handling, address heuristics. Parsers never invent values;
missing stays missing, ambiguous stays low-confidence.

Implemented field keys: full_name, father_name, dob, address, licence_no
(display-only anchor; never used for case matching) and application_no
(digits-only; never used for case matching either).
Heuristic-only (clearly marked): address PIN fallback -- new keys arrive as
explicit parsers, not silent guesses.

Phase 3 evidence notes (local LL sample, never committed):
  * The document prints numbered field labels ("3. Father's Name", ...)
    and a table header fragment whose OCR reads "AppiNo" for the printed
    "App.No." -- both forms are in the configured label lists.
  * Photo-area noise is sometimes merged into the same OCR line as the
    printed name (low-confidence lowercase fragments far right of the
    text). ``extract_name_sequence`` therefore takes runs of name-shaped
    tokens and drops a trailing all-lowercase fragment only when two or
    more non-lowercase tokens precede it. It never invents a value.

Phase 4A evidence notes (six operator-verified LL documents, local only):
  * Photo OCR fuses label words ("PresentAddress"); whitespace inside a
    multi-word label is therefore optional when matching.
  * Template-B applicant rows print "Display Name" (OCR also emits
    "DisplayName"). When a specific name label yields a candidate,
    bare "Name" rows are not emitted for the same document -- on those
    documents the bare row is a shorter, unrelated name row.
  * Template-B father rows print "Son/Wife/Daughter of"; OCR often drops
    the "of" (and may fuse it). Both forms are configured, with slash
    spacing optional.
  * Addresses are five-line, PIN-terminated blocks on the audited form
    family; a block may extend one line past ``max_lines`` only through
    its closing PIN line, stops at any configured field label
    (application_no included) and identical blocks are emitted once.
  * A bare "No <digits>" line is never a licence candidate when its
    digits appear in a recognized application-number area (same digits
    on an application-labeled line, or an adjacent split label row).
    Otherwise it is kept at low confidence (0.55: attribution is
    ambiguous) -- never guessed, never silently discarded.
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
            "labels": ["full name", "display name", "name of the applicant",
                       "applicant name", "holder name", "name"],
            "max_words": 5,
        },
        "father_name": {
            "labels": ["father's name", "fathers name", "father name",
                       "name of the father", "name of father",
                       "father's / husband's name", "father/husband name",
                       "father / husband name",
                       "son / wife / daughter of", "son / wife / daughter"],
            "max_words": 5,
        },
        "dob": {"labels": ["date of birth", "dob", "birth date", "born"]},
        "address": {
            "labels": ["permanent address", "present address", "address", "addr", "residence"],
            "max_lines": 3,
        },
        "licence_no": {
            "labels": ["driving licence no", "driving license no", "licence no",
                       "license no", "licence number", "license number",
                       "dl no", "ll no"],
        },
        "application_no": {
            "labels": ["application number", "application no", "appi no",
                       "appino", "app no", "appno", "app. no", "app.no",
                       "appl no", "appln no"],
            "min_digits": 8,
            "max_digits": 20,
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


# Fields for which a deterministic parser exists in this module. The
# benchmark reports ``parser_missing``/``labels_missing`` per configured
# field so gaps stay visible instead of being silently invented.
PARSER_FIELDS = frozenset({
    "full_name", "father_name", "dob", "licence_no", "application_no",
    "address",
})


def _label_pattern(labels: list[str], anchored: bool = True) -> re.Pattern:
    """Compile a label matcher exposing named groups ``label`` and ``value``.

    ``anchored=True`` requires the label to start the line (after an
    optional form number such as "6. Present Address"). ``anchored=False``
    matches the label anywhere on the line as a standalone token; used
    only for ``application_no`` because photo OCR merges table header
    cells ("AppiNo . 3542873326") into one line.

    Phase 4A: whitespace between the words of a label is optional, because
    the audited OCR fuses label words ("PresentAddress"). A label written
    with slashes as separate words ("son / wife / daughter") therefore
    also matches the hugging form ("son/wife/daughter").
    """
    norm = {re.sub(r"\s+", " ", str(l).lower()).strip() for l in labels}
    ordered = sorted((l for l in norm if l), key=len, reverse=True)
    alt = "|".join(
        r"\s*".join(re.escape(part) for part in l.split(" ")) for l in ordered)
    head = r"^\s*(?:\d+\s*[.,]\s*)?" if anchored else r"(?<![A-Za-z0-9])"
    return re.compile(
        rf"(?i){head}(?P<label>{alt})(?![A-Za-z])\s*[:\-]?\s*(?P<value>.*)$"
    )


_STANDALONE_LICENCE_RE = re.compile(r"^\s*No\.?\s*(\d{6,20})\s*$", re.IGNORECASE)

# Address blocks on the audited form family are five-line, PIN-terminated
# blocks: the last line carries the 6-digit PIN. A block may extend one
# line past the configured ``max_lines`` window, but only through that
# closing PIN line -- without the PIN signal an extra line is OCR junk or
# the next field, not address content (Phase 4A evidence).
_ADDR_PIN_EXTEND = 1
_PIN6_RE = re.compile(r"\b\d{6}\b")

# Tokens that only split name runs; they never become part of a name.
# Relation words, form vocabulary and country/state words found in the
# audited document's boilerplate. Source: Phase 3 audit (see docstring).
_NAME_STOPWORDS = frozenset({
    "the", "of", "and", "son", "daughter", "wife", "husband", "father",
    "mother", "parents", "parent", "guardian", "male", "female", "indian",
    "india", "sri", "smt", "shri", "mr", "mrs", "ms", "dr", "no", "nil",
    "name", "names", "address", "date", "birth", "dob", "licence",
    "license", "application", "applicant", "holder", "form", "signature",
    "government", "govt", "telangana", "state", "valid", "issued", "copy",
})

_NAME_WORD_RE = re.compile(r"[A-Za-z][A-Za-z.'\-]*")


def _clean_lines(text: str) -> list[str]:
    # Collapse repeated spaces/tabs (photo OCR emits ragged runs) and
    # normalize the typographic apostrophe so label patterns can use "'".
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
    """Pick the best contiguous name-shaped run from a labeled value area.

    Photo OCR merges unrelated fragments onto the label line (Phase 3
    audit: a low-confidence 3-letter photo-area token landed after the
    printed name). This parser takes maximal runs of name-shaped tokens,
    separated by stopwords/digits/punctuation, and selects the longest run
    of 2..max_words tokens. A trailing all-lowercase token is dropped only
    when two or more non-lowercase tokens precede it (printed names in the
    audited document are uppercase; such a tail is an OCR fragment, not a
    name word). Never returns a value that is not present in ``value``.
    """
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
    best = max(candidates, key=len)  # longest; leftmost on ties
    while len(best) > 2 and best[-1].islower() and not all(w.islower() for w in best):
        best = best[:-1]
    if not 2 <= len(best) <= max_words:
        return None
    return " ".join(w[0].upper() + w[1:] for w in best)


def parse_application_no(
    value: str, min_digits: int = 8, max_digits: int = 20
) -> str | None:
    """Digits-only application number, else None.

    Sarathi application numbers are numeric; the audited document prints
    one 10-digit number. Bounds 8..20 reject embedded dates/short codes
    and long ID concatenations ("...30500076472026..." would otherwise
    partially match). The whole digit run must satisfy the bounds.
    """
    val = value or ""
    m = re.search(
        rf"(?<!\d)(\d{{{int(min_digits)},{int(max_digits)}}})(?!\d)", val)
    return m.group(1) if m else None


def _tidy_address_segment(seg: str) -> str:
    """Trim trailing OCR junk tokens from one address segment.

    Only tokens that are punctuation-only or a single alphabetic character
    are removed (repeatedly), plus trailing separators. Digits and real
    words are kept; the original token order is untouched.
    """
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


def parse_licence_no(value: str) -> str | None:
    """Official-number anchor for display only. Strict shape, else None."""
    val = re.sub(r"\s+", " ", value.strip().strip(".,;:-/")).strip().upper()
    if not 6 <= len(val) <= 24:
        return None
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9 \-/]*[A-Z0-9]", val):
        return None
    if not re.search(r"\d", val):
        return None
    if re.search(r"[A-Z]{6,}", val.replace(" ", "")):
        return None  # prose, not a number
    return val


def _field_cfg(fields: dict, key: str) -> dict:
    cfg = fields.get(key, {}) if isinstance(fields.get(key), dict) else {}
    return cfg


def _labels_of(fields: dict, key: str) -> list[str]:
    return [str(l) for l in (_field_cfg(fields, key).get("labels") or [])]


def _standalone_app_context(
    digits: str, idx: int, lines: list[str],
    app_pat: re.Pattern | None, app_digits: set[str],
) -> bool:
    """True when a bare "No <digits>" line belongs to an application number.

    Two evidence-backed signals (Phase 4A): the digits already appear on a
    line that matched a configured application-number label, or an
    adjacent line is an application-number label whose own number is
    missing (OCR split the label row, leaving "No <digits>" behind).
    Anything else is left to the caller, which keeps the candidate but
    marks it low-confidence instead of guessing.
    """
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
    name_labels = _labels_of(fields, "full_name")
    father_labels = _labels_of(fields, "father_name")
    dob_labels = _labels_of(fields, "dob")
    addr_labels = _labels_of(fields, "address")
    lic_labels = _labels_of(fields, "licence_no")
    app_labels = _labels_of(fields, "application_no")
    addr_max_lines = int(_field_cfg(fields, "address").get("max_lines", 3) or 3)
    name_max_words = int(_field_cfg(fields, "full_name").get("max_words", 5) or 5)
    father_max_words = int(_field_cfg(fields, "father_name").get("max_words", 5) or 5)
    app_cfg = _field_cfg(fields, "application_no")
    app_min_digits = int(app_cfg.get("min_digits", 8) or 8)
    app_max_digits = int(app_cfg.get("max_digits", 20) or 20)
    name_pat = _label_pattern(name_labels) if name_labels else None
    father_pat = _label_pattern(father_labels) if father_labels else None
    dob_pat = _label_pattern(dob_labels) if dob_labels else None
    addr_pat = _label_pattern(addr_labels) if addr_labels else None
    lic_pat = _label_pattern(lic_labels) if lic_labels else None
    # Search semantics only for application_no: photo OCR merges the
    # table header ("AppiNo . 3542873326") with neighbouring cells, so the
    # label is not always at the line start. The digit-shape check below
    # keeps false positives out.
    app_pat = _label_pattern(app_labels, anchored=False) if app_labels else None
    stop_labels = (name_labels + father_labels + dob_labels + addr_labels
                   + lic_labels + app_labels)
    stop_pat = _label_pattern(stop_labels) if stop_labels else None

    # Display-Name precedence (Phase 4A): when a *specific* name label
    # ("Display Name", "Full Name", ...) yields a candidate anywhere in
    # the document, bare "Name" rows are not emitted -- on template-B
    # documents the bare row is a shorter, unrelated name row.
    specific_name_found = False
    if name_pat:
        for pg in pages:
            for line in _clean_lines(pg.get("text", "")):
                m = name_pat.match(line)
                if (m and m.group("value")
                        and m.group("label").strip().lower() != "name"
                        and extract_name_sequence(m.group("value"),
                                                   name_max_words)):
                    specific_name_found = True
                    break
            if specific_name_found:
                break

    for pg in pages:
        pnum = pg.get("page", 1)
        src_method = pg.get("method", "text")
        lines = _clean_lines(pg.get("text", ""))
        # Digits on a line matching a recognized application-number label
        # (Phase 4A): used to reject application-number lines that the
        # standalone-licence fallback would otherwise claim.
        app_digits: set[str] = set()
        if app_pat:
            for line in lines:
                if app_pat.search(line):
                    app_digits.update(
                        re.findall(r"(?<!\d)\d{6,20}(?!\d)", line))
        for idx, line in enumerate(lines):
            if name_pat:
                m = name_pat.match(line)
                if m and m.group("value"):
                    label = m.group("label").strip().lower()
                    # A bare "Name" followed by "of the ..." is a relation
                    # phrase (e.g. "Name of the Father"), not the applicant.
                    relation = label == "name" and re.match(
                        r"(?i)^\s*of\b", m.group("value"))
                    # Bare "Name" rows yield to specific name labels.
                    suppressed = label == "name" and specific_name_found
                    if not relation and not suppressed:
                        name = extract_name_sequence(m.group("value"),
                                                     name_max_words)
                        if name:
                            out.append({"field_key": "full_name", "value": name,
                                        "source_page": pnum, "method": f"{src_method}:labeled",
                                        "confidence": 0.85})
            if father_pat:
                m = father_pat.match(line)
                if m and m.group("value"):
                    fname = extract_name_sequence(m.group("value"), father_max_words)
                    if fname:
                        out.append({"field_key": "father_name", "value": fname,
                                    "source_page": pnum, "method": f"{src_method}:labeled",
                                    "confidence": 0.85})
            if dob_pat:
                m = dob_pat.match(line)
                if m and m.group("value"):
                    parsed = parse_date_token(m.group("value"))
                    if parsed:
                        val, ambiguous, kind = parsed
                        out.append({"field_key": "dob", "value": val,
                                    "source_page": pnum,
                                    "method": f"{src_method}:labeled-{kind}",
                                    "confidence": 0.4 if ambiguous else 0.85})
            if lic_pat:
                m = lic_pat.match(line)
                if m and m.group("value"):
                    lic = parse_licence_no(m.group("value"))
                    if lic:
                        out.append({"field_key": "licence_no", "value": lic,
                                    "source_page": pnum, "method": f"{src_method}:labeled",
                                    "confidence": 0.75})
            if app_pat:
                m = app_pat.search(line)
                if m and m.group("value"):
                    app = parse_application_no(m.group("value"), app_min_digits,
                                               app_max_digits)
                    if app:
                        out.append({"field_key": "application_no", "value": app,
                                    "source_page": pnum, "method": f"{src_method}:labeled",
                                    "confidence": 0.75})
            # Standalone "No <digits>" line (a label was lost in OCR). It
            # is skipped only when the digits belong to a recognized
            # application-number area; otherwise it stays, flagged
            # low-confidence because the attribution is ambiguous.
            m = _STANDALONE_LICENCE_RE.match(line)
            if m:
                lic = parse_licence_no(m.group(1))
                if lic and not _standalone_app_context(
                        m.group(1), idx, lines, app_pat, app_digits):
                    out.append({"field_key": "licence_no", "value": lic,
                                "source_page": pnum,
                                "method": f"{src_method}:standalone-no",
                                "confidence": 0.55})
        # Address: labeled block of following lines (multi-line kept
        # together). Blocks on the audited form family are five-line,
        # PIN-terminated layouts; a block may extend one line past
        # ``max_lines`` only through its closing PIN line and stops at any
        # other configured field label (application_no included).
        # Identical blocks are emitted once (no duplicate candidates).
        seen_addr: set[str] = set()
        for idx, line in enumerate(lines):
            m = addr_pat.match(line) if addr_pat else None
            if not m:
                continue
            rest = m.group("value").strip()
            block = [_tidy_address_segment(rest)] if rest else []
            window = lines[idx + 1: idx + 1 + addr_max_lines + _ADDR_PIN_EXTEND]
            for offset, nxt in enumerate(window):
                if stop_pat and stop_pat.match(nxt):
                    break
                if offset >= addr_max_lines and not _PIN6_RE.search(nxt):
                    break
                block.append(_tidy_address_segment(nxt))
                if _PIN6_RE.search(nxt):
                    break
            value = ", ".join(b for b in block if b)[:500]
            if value and value not in seen_addr:
                seen_addr.add(value)
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
                if _PIN6_RE.search(line) and idx > 0:
                    prev = _tidy_address_segment(lines[idx - 1])
                    if not prev or (stop_pat and stop_pat.match(prev)):
                        continue
                    value = f"{prev}, {_tidy_address_segment(line)}"[:500]
                    out.append({"field_key": "address", "value": value,
                                "source_page": pnum, "method": f"{src_method}:heuristic-pin",
                                "confidence": 0.5})
                    break
    return out
