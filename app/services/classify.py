"""Best-effort document classification (Phase 3).

Filename clues + file properties + optional OCR hints (Phase 4 hook).
Never a cloud model, never certain: low confidence returns 'unknown' so
the operator classifies. Photos/signatures without identity text must NOT
be force-matched; that rule is enforced by callers (Phase 4/5).
"""
from __future__ import annotations

import re
from pathlib import Path

PHOTO_HINTS = {"photo", "photograph", "photos", "pic", "pics", "picture",
               "face", "selfie", "passportphoto", "passport"}
SIGN_HINTS = {"sign", "signature", "signatures", "sig"}
DOC_HINTS = {"aadhaar", "aadhar", "licence", "license", "certificate",
             "address", "proof", "document", "docs", "form", "medical",
             "pan", "voter", "passportdoc", "ll", "dl", "application",
             "invoice", "bill", "marksheet"}

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(name: str) -> set[str]:
    return set(_TOKEN.findall(name.lower()))


def _hint_hit(token: str, hint: str) -> bool:
    if token == hint:
        return True
    # Longer hints may carry suffixes/numbers (license1, passportphoto2).
    # Short hints stay exact-only: 'sign' must not fire on 'assign',
    # 'pan' must not fire on 'panel'.
    return len(hint) >= 4 and token.startswith(hint)


def suggest_doctype(
    filename: str,
    extension: str = "",
    width: int | None = None,
    height: int | None = None,
    ocr_hints: dict | None = None,
) -> tuple[str, float, str]:
    """Return (doctype, confidence 0..1, reason).

    doctypes: 'photo' | 'signature' | 'supporting_ll' | 'unknown'.
    """
    toks = _tokens(Path(filename).stem)
    ext = (extension or Path(filename).suffix).lower()
    scores = {"photo": 0.0, "signature": 0.0, "supporting_ll": 0.0}
    hits: dict[str, list[str]] = {"photo": [], "signature": [], "supporting_ll": []}

    for t in toks:
        for hint in PHOTO_HINTS:
            if _hint_hit(t, hint):
                scores["photo"] += 2.0
                hits["photo"].append(t)
                break
        for hint in SIGN_HINTS:
            if _hint_hit(t, hint):
                scores["signature"] += 2.0
                hits["signature"].append(t)
                break
        for hint in DOC_HINTS:
            if _hint_hit(t, hint):
                scores["supporting_ll"] += 2.0
                hits["supporting_ll"].append(t)
                break

    if ext == ".pdf":
        scores["supporting_ll"] += 1.0
    if ocr_hints:
        text = " ".join(str(v).lower() for v in ocr_hints.values())
        for kw in DOC_HINTS:
            if kw in text:
                scores["supporting_ll"] += 1.0
                hits["supporting_ll"].append(f"ocr:{kw}")
                break

    # Conservative dimension hints only (never decisive alone).
    if width and height and height > 0:
        ratio = width / height
        if ratio >= 2.5 and height <= 800:
            scores["signature"] += 0.5
        elif 0.7 <= ratio <= 1.4:
            scores["photo"] += 0.5

    best = max(scores, key=scores.get)
    best_score = scores[best]
    if best_score >= 2.0:
        conf = 0.85
    elif best_score >= 1.0:
        conf = 0.6
    else:
        return ("unknown", 0.3,
                f"no filename/property clue in '{filename}'; operator classification required")

    if conf < 0.5:
        return ("unknown", conf, "low confidence; operator classification required")
    detail = ",".join(hits[best][:3]) or "properties"
    return (best, conf, f"matched {detail} in '{filename}'")
