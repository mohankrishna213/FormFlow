"""Best-effort document classification (Phase 3).

Supports:
- Aadhaar cards and letters (aadhaar)
- PAN cards (pan)
- SSC / 10th marks memo (ssc)
- Learner's Licence (learner_licence)
- Driving Licence (driving_licence)
- Vehicle RC (vehicle_rc)
- Supporting document general (supporting_ll)
- Standalone photos and signatures (photo, signature)
- Unknown documents requiring operator classification (unknown)

Rules:
- Filename clues are weak signals.
- English OCR keywords and document titles are primary evidence.
- Document numbers (Aadhaar, PAN) are supporting evidence only, never sole classifier.
- Dimensions/aspect-ratios are weak clues; identity text ALWAYS overrides photo/sig hints.
- Unknown/generic documents always receive OCR; never silently skipped.
- Preserves 100% backward compatibility with pipeline doctypes (photo, signature, supporting_ll, unknown).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PHOTO_HINTS = {
    "photo", "photograph", "photos", "pic", "pics", "picture",
    "face", "selfie", "passportphoto", "passport", "passphoto",
    "photoid", "idphoto",
}
SIGN_HINTS = {"sign", "signature", "signatures", "sig"}

AADHAAR_HINTS = {"aadhaar", "aadhar", "uidai", "uid"}
PAN_HINTS = {"pan", "pancard"}
SSC_HINTS = {"ssc", "10th", "tenth", "marks", "memo", "marksheet", "matric", "secondary"}
LICENCE_HINTS = {"licence", "license", "ll", "dl", "learner", "learning", "driving", "form3", "form7"}
TR_HINTS = {"tr", "tempreg", "temporary", "form19", "form20", "salecert"}
RC_HINTS = {"rc", "vehicle", "registration", "form23"}
GENERIC_DOC_HINTS = {
    "certificate", "address", "proof", "document", "docs", "form",
    "medical", "voter", "passportdoc", "application", "invoice", "bill",
}

_TOKEN = re.compile(r"[a-z0-9]+")

# OCR Title and Keyword Matches
AADHAAR_OCR_KEYWORDS = [
    "government of india", "unique identification authority",
    "aadhaar", "aadhar", "uidai", "mera aadhaar", "my aadhaar",
    "enrolment no", "help@uidai.gov.in", "1947",
]
PAN_OCR_KEYWORDS = [
    "income tax department", "permanent account number",
    "govt. of india", "pan card", "permanent account",
]
SSC_OCR_KEYWORDS = [
    "secondary school certificate", "board of secondary education",
    "ssc examination", "ssc public", "marks memo", "matriculation",
    "hall ticket", "roll no", "passed ssc", "head master",
    "cumulative grade", "grade points", "cgpa", "school",
    "first language", "second language", "third language",
]
SSC_SUBJECT_KEYWORDS = [
    "telugu", "hindi", "english", "maths", "mathematics",
    "general science", "science", "social studies", "social", "grade",
]
LL_OCR_KEYWORDS = [
    "form 3", "form-3", "learner's licence", "learning licence",
    "learners licence", "licencing authority", "learner's license",
]
DL_OCR_KEYWORDS = [
    "form 7", "form-7", "driving licence", "driving license",
    "driving licance", "indian union driving", "motor driving",
]
TR_OCR_KEYWORDS = [
    "temporary registration", "temp. reg", "temp reg", "temporary certificate of registration",
    "tr no", "tr number", "tr valid", "form 19", "form-19", "form 20", "form-20",
    "sale certificate", "form 21", "form-21", "form 22", "form-22",
    "temporary regn", "chassis no", "engine no", "maker's class", "makers class",
]
RC_OCR_KEYWORDS = [
    "form 23", "form-23", "certificate of registration",
    "registration certificate", "chassis no", "engine no",
]

AADHAAR_NUM_RE = re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\s\d{4})(?!\d)")
PAN_NUM_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
TR_NUM_RE = re.compile(r"\b(?:TS|TG|AP|KA|MH|TN|KL|DL|GJ|RJ|UP|MP|WB)[0-9]{2}TR[0-9A-Z]{4,10}\b")
RC_NUM_RE = re.compile(r"\b[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}\b")


@dataclass
class ClassificationResult:
    category: str
    doctype: str  # pipeline doctype: 'photo', 'signature', 'supporting_ll', 'unknown'
    confidence: float
    reason: str
    evidence: list[str] = field(default_factory=list)
    is_identity_doc: bool = False


CATEGORY_TO_DOCTYPE = {
    "aadhaar": "supporting_ll",
    "pan": "supporting_ll",
    "ssc": "supporting_ll",
    "driving_licence": "supporting_ll",
    "learner_licence": "supporting_ll",
    "tr_document": "supporting_ll",
    "vehicle_rc": "supporting_ll",
    "supporting_ll": "supporting_ll",
    "photo": "photo",
    "signature": "signature",
    "unknown": "unknown",
}


def _tokens(name: str) -> set[str]:
    return set(_TOKEN.findall(name.lower()))


def _hint_hit(token: str, hint: str) -> bool:
    if token == hint:
        return True
    return len(hint) >= 4 and token.startswith(hint)


def classify_document(
    filename: str,
    extension: str = "",
    width: int | None = None,
    height: int | None = None,
    ocr_hints: dict[str, Any] | None = None,
    custom_rules: dict[str, dict[str, Any]] | None = None,
) -> ClassificationResult:
    """Perform evidence-based document classification with complete audit trail."""
    toks = _tokens(Path(filename).stem)
    ext = (extension or Path(filename).suffix).lower()

    scores: dict[str, float] = {
        "aadhaar": 0.0,
        "pan": 0.0,
        "ssc": 0.0,
        "learner_licence": 0.0,
        "driving_licence": 0.0,
        "tr_document": 0.0,
        "vehicle_rc": 0.0,
        "supporting_ll": 0.0,
        "photo": 0.0,
        "signature": 0.0,
    }
    custom_crop_modes: dict[str, str] = {}
    if custom_rules:
        for ck, cr in custom_rules.items():
            if ck and ck != "unknown" and isinstance(cr, dict):
                scores.setdefault(ck, 0.0)
                custom_crop_modes[ck] = str(cr.get("crop_mode") or "document").lower()
    evidence: dict[str, list[str]] = {k: [] for k in scores}

    # 1. Filename tokens (weak evidence)
    for t in toks:
        for hint in AADHAAR_HINTS:
            if _hint_hit(t, hint):
                scores["aadhaar"] += 2.0
                evidence["aadhaar"].append(f"filename:{t}")
                break
        for hint in PAN_HINTS:
            if _hint_hit(t, hint):
                scores["pan"] += 2.0
                evidence["pan"].append(f"filename:{t}")
                break
        for hint in SSC_HINTS:
            if _hint_hit(t, hint):
                scores["ssc"] += 2.0
                evidence["ssc"].append(f"filename:{t}")
                break
        for hint in TR_HINTS:
            if _hint_hit(t, hint):
                scores["tr_document"] += 2.0
                evidence["tr_document"].append(f"filename:{t}")
                break
        for hint in RC_HINTS:
            if _hint_hit(t, hint):
                scores["vehicle_rc"] += 2.0
                evidence["vehicle_rc"].append(f"filename:{t}")
                break
        for hint in LICENCE_HINTS:
            if _hint_hit(t, hint):
                if t in ("ll", "learner", "learning", "form3"):
                    scores["learner_licence"] += 2.0
                    evidence["learner_licence"].append(f"filename:{t}")
                elif t in ("dl", "driving", "form7"):
                    scores["driving_licence"] += 2.0
                    evidence["driving_licence"].append(f"filename:{t}")
                else:
                    scores["supporting_ll"] += 2.0
                    evidence["supporting_ll"].append(f"filename:{t}")
                break
        for hint in PHOTO_HINTS:
            if _hint_hit(t, hint):
                scores["photo"] += 2.0
                evidence["photo"].append(f"filename:{t}")
                break
        for hint in SIGN_HINTS:
            if _hint_hit(t, hint):
                scores["signature"] += 2.0
                evidence["signature"].append(f"filename:{t}")
                break
        for hint in GENERIC_DOC_HINTS:
            if _hint_hit(t, hint):
                scores["supporting_ll"] += 2.0
                evidence["supporting_ll"].append(f"filename:{t}")
                break

    if ext == ".pdf":
        scores["supporting_ll"] += 1.0
        evidence["supporting_ll"].append("ext:pdf")

    # 2. OCR text keyword matching (strong evidence)
    ocr_text = ""
    if ocr_hints:
        ocr_text = " ".join(str(v).lower() for v in ocr_hints.values())
        for kw in AADHAAR_OCR_KEYWORDS:
            if kw in ocr_text:
                scores["aadhaar"] += 4.0
                evidence["aadhaar"].append(f"ocr:{kw}")
        for kw in PAN_OCR_KEYWORDS:
            if kw in ocr_text:
                scores["pan"] += 4.0
                evidence["pan"].append(f"ocr:{kw}")
        for kw in SSC_OCR_KEYWORDS:
            if kw in ocr_text:
                scores["ssc"] += 4.0
                evidence["ssc"].append(f"ocr:{kw}")
        subj_hits = [kw for kw in SSC_SUBJECT_KEYWORDS if re.search(rf"\b{re.escape(kw)}\b", ocr_text)]
        if len(subj_hits) >= 3:
            scores["ssc"] += 4.0 + 0.5 * min(4, len(subj_hits))
            evidence["ssc"].append(f"ocr:subjects({','.join(subj_hits[:4])})")
        for kw in LL_OCR_KEYWORDS:
            if kw in ocr_text:
                scores["learner_licence"] += 4.0
                evidence["learner_licence"].append(f"ocr:{kw}")
        for kw in DL_OCR_KEYWORDS:
            if kw in ocr_text:
                scores["driving_licence"] += 4.0
                evidence["driving_licence"].append(f"ocr:{kw}")
        for kw in TR_OCR_KEYWORDS:
            if kw in ocr_text:
                scores["tr_document"] += 4.0
                evidence["tr_document"].append(f"ocr:{kw}")
        for kw in RC_OCR_KEYWORDS:
            if kw in ocr_text:
                scores["vehicle_rc"] += 3.5
                evidence["vehicle_rc"].append(f"ocr:{kw}")

        # Supporting regex formats in OCR
        if AADHAAR_NUM_RE.search(ocr_text):
            scores["aadhaar"] += 2.5
            evidence["aadhaar"].append("ocr:aadhaar_number_pattern")
            if any(k in ocr_text for k in ("dob", "d0b", "date of birth", "year of birth", "female", "male", "vid")):
                scores["aadhaar"] += 2.0
                evidence["aadhaar"].append("ocr:aadhaar_cosignal")
        if PAN_NUM_RE.search(ocr_text.upper()):
            scores["pan"] += 3.5
            evidence["pan"].append("ocr:pan_number_pattern")
        if TR_NUM_RE.search(ocr_text.upper()):
            scores["tr_document"] += 4.5
            evidence["tr_document"].append("ocr:tr_number_pattern")
        if RC_NUM_RE.search(ocr_text.upper()):
            scores["vehicle_rc"] += 1.5
            evidence["vehicle_rc"].append("ocr:rc_number_pattern")

    # 2b. Dynamic custom profile filename & OCR keyword rules (from profiles.json)
    if custom_rules:
        for ck, cr in custom_rules.items():
            if ck not in scores or not isinstance(cr, dict):
                continue
            for hint in (cr.get("filename_hints") or ()):
                h = str(hint).strip().lower()
                if not h:
                    continue
                if any(_hint_hit(t, h) for t in toks) or (len(h) >= 4 and h in Path(filename).stem.lower()):
                    scores[ck] += 2.5
                    evidence[ck].append(f"filename:{h}")
            if ocr_text:
                for kw in (cr.get("ocr_keywords") or ()):
                    k = str(kw).strip().lower()
                    if k and k in ocr_text:
                        scores[ck] += 4.5
                        evidence[ck].append(f"ocr:{k}")

    # Has identity evidence?
    id_categories = {
        "aadhaar", "pan", "ssc", "learner_licence", "driving_licence",
        "tr_document", "vehicle_rc", "supporting_ll",
    }
    for ck, cmode in custom_crop_modes.items():
        if cmode not in ("photo", "signature"):
            id_categories.add(ck)
    has_identity_evidence = any(scores[c] >= 2.0 for c in id_categories)

    # 3. Dimensional hints (weak; only valid if NO identity text exists)
    if not has_identity_evidence and width and height and height > 0:
        ratio = width / height
        if ratio >= 2.5 and height <= 800:
            scores["signature"] += 0.5
            evidence["signature"].append(f"ratio:{ratio:.2f}")
        elif 0.7 <= ratio <= 1.4:
            scores["photo"] += 0.5
            evidence["photo"].append(f"ratio:{ratio:.2f}")

    # Determine highest-scoring category
    best_cat = max(scores, key=scores.get)
    best_score = scores[best_cat]

    if best_score >= 4.0:
        conf = 0.90
    elif best_score >= 2.0:
        conf = 0.85
    elif best_score >= 1.0:
        conf = 0.60
    else:
        return ClassificationResult(
            category="unknown",
            doctype="unknown",
            confidence=0.30,
            reason=f"no strong filename/property clue in '{filename}'; operator classification required",
            evidence=[],
            is_identity_doc=False,
        )

    if best_cat in CATEGORY_TO_DOCTYPE:
        pipeline_doctype = CATEGORY_TO_DOCTYPE[best_cat]
    else:
        cmode = custom_crop_modes.get(best_cat, "document")
        pipeline_doctype = cmode if cmode in ("photo", "signature") else "supporting_ll"
    matched_details = ", ".join(evidence[best_cat][:4]) or "properties"
    reason = f"matched {matched_details} in '{filename}'"

    return ClassificationResult(
        category=best_cat,
        doctype=pipeline_doctype,
        confidence=conf,
        reason=reason,
        evidence=evidence[best_cat],
        is_identity_doc=(best_cat in id_categories),
    )


def suggest_doctype(
    filename: str,
    extension: str = "",
    width: int | None = None,
    height: int | None = None,
    ocr_hints: dict[str, Any] | None = None,
    detailed: bool = False,
) -> tuple[str, float, str]:
    """Return (doctype, confidence, reason).

    If detailed=False (default), doctype is the pipeline doctype:
    'photo' | 'signature' | 'supporting_ll' | 'unknown'.
    If detailed=True, doctype is the fine-grained category:
    'aadhaar' | 'pan' | 'ssc' | 'learner_licence' | 'driving_licence' | 'vehicle_rc' | ...
    """
    res = classify_document(filename, extension, width, height, ocr_hints)
    doc_type = res.category if detailed else res.doctype
    return doc_type, res.confidence, res.reason
