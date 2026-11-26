"""100% Offline QR Code decoder for Indian Aadhaar (Secure QR & XML QR) and PAN cards.

Design rules:
- Strictly local execution using ``zxingcpp`` (primary, handles dense Version 20-40
  UIDAI Secure QR codes reliably) with OpenCV ``cv2.QRCodeDetector`` fallback.
- Never logs or prints applicant values.
- Never fabricates values (e.g., never converts year-only YOB to a full DOB, never
  invents a full 12-digit Aadhaar number from a Secure QR reference ID).
"""
from __future__ import annotations

import re
import zlib
from typing import Any

from PIL import Image, ImageOps


def _decode_qr_payloads(img: Image.Image) -> list[str]:
    """Extract raw QR string payloads from an image (full image + upscaled crops if needed)."""
    payloads: list[str] = []
    seen: set[str] = set()
    work = ImageOps.exif_transpose(img).convert("RGB")

    def _add(txt: str | None) -> None:
        if txt and txt.strip() and txt.strip() not in seen:
            seen.add(txt.strip())
            payloads.append(txt.strip())

    # Candidate views: full image, plus 2x upscaled right-half/bottom-right crop where
    # Aadhaar & PAN QR codes sit on small hand-held WhatsApp photos.
    views: list[Image.Image] = [work]
    w, h = work.size
    if max(w, h) < 1200 and min(w, h) >= 120:
        views.append(work.resize((w * 2, h * 2), Image.LANCZOS))

    # 1. Primary: zxing-cpp (fast native C++ decoder, excels on dense UIDAI Secure QRs)
    try:
        import zxingcpp  # type: ignore

        for view in views:
            for res in zxingcpp.read_barcodes(view):
                if "QR" in str(res.format).upper() and res.text:
                    _add(res.text)
            if payloads:
                return payloads
        # Also try cropped right half at 2x if full image had small blurry QR
        if w >= 240 and h >= 160:
            crop_r = work.crop((int(w * 0.35), 0, w, h))
            if max(crop_r.size) < 1200:
                crop_r = crop_r.resize((crop_r.width * 2, crop_r.height * 2), Image.LANCZOS)
            for res in zxingcpp.read_barcodes(crop_r):
                if "QR" in str(res.format).upper() and res.text:
                    _add(res.text)
            if payloads:
                return payloads
    except Exception:
        pass

    # 2. Fallback: OpenCV QRCodeDetector (zero extra dependencies)
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        det = cv2.QRCodeDetector()
        for view in views:
            bgr = cv2.cvtColor(np.array(view), cv2.COLOR_RGB2BGR)
            data, _, _ = det.detectAndDecode(bgr)
            if data:
                _add(data)
                return payloads
    except Exception:
        pass

    return payloads


def detect_has_qr(img: Image.Image) -> bool:
    """Fast check returning True if the image contains a readable or localized QR code."""
    if _decode_qr_payloads(img):
        return True
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        work = ImageOps.exif_transpose(img).convert("RGB")
        if max(work.size) > 1000:
            sc = 1000.0 / max(work.size)
            work = work.resize(
                (max(1, int(work.width * sc)), max(1, int(work.height * sc))),
                Image.LANCZOS,
            )
        bgr = cv2.cvtColor(np.array(work), cv2.COLOR_RGB2BGR)
        ok, pts = cv2.QRCodeDetector().detect(bgr)
        return bool(ok and pts is not None and len(pts) > 0)
    except Exception:
        return False


def _clean_relation_name(raw: str) -> str | None:
    """Strip S/O, D/O, W/O, C/O prefixes from Aadhaar care-of field."""
    val = re.sub(r"\s+", " ", (raw or "").strip())
    val = re.sub(r"(?i)^(?:s/o|d/o|w/o|c/o|so|do|wo|co)\s*[:.,\-]?\s*", "", val).strip(" ,.;:-")
    if len(re.sub(r"[^A-Za-z]", "", val)) < 3:
        return None
    if any(ch.isdigit() for ch in val):
        return None
    return val


def _normalize_gender(raw: str) -> str | None:
    g = (raw or "").strip().lower()
    if g in ("m", "male"):
        return "Male"
    if g in ("f", "female"):
        return "Female"
    if g in ("t", "transgender", "other"):
        return "Transgender"
    return None


def _normalize_dob(raw: str) -> str | None:
    from .fields import parse_date_token

    parsed = parse_date_token((raw or "").strip(), indian_dmy=True)
    if parsed and not parsed[1]:
        return parsed[0]
    return None


def _assemble_address(parts: list[str]) -> str | None:
    cleaned: list[str] = []
    seen: set[str] = set()
    for p in parts:
        seg = re.sub(r"\s+", " ", (p or "").strip(" ,.;:-"))
        if not seg:
            continue
        key = seg.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(seg)
    return ", ".join(cleaned)[:500] if cleaned else None


def _build_aadhaar_candidates(
    *,
    name: str = "",
    dob: str = "",
    gender: str = "",
    care_of: str = "",
    house: str = "",
    street: str = "",
    landmark: str = "",
    location: str = "",
    vtc: str = "",
    po: str = "",
    subdist: str = "",
    district: str = "",
    state: str = "",
    pincode: str = "",
    aadhaar_no: str = "",
    method: str = "qr:aadhaar-secure",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def _emit(key: str, val: str | None, conf: float = 0.97) -> None:
        if val and str(val).strip():
            out.append({
                "field_key": key,
                "value": str(val).strip(),
                "source_page": 1,
                "method": method,
                "confidence": conf,
            })

    clean_name = re.sub(r"\s+", " ", name.strip())
    if len(re.sub(r"[^A-Za-z]", "", clean_name)) >= 3:
        _emit("full_name", clean_name, 0.97)

    norm_dob = _normalize_dob(dob)
    if norm_dob:
        _emit("dob", norm_dob, 0.97)

    norm_gender = _normalize_gender(gender)
    if norm_gender:
        _emit("gender", norm_gender, 0.97)

    father = _clean_relation_name(care_of)
    if father:
        _emit("father_name", father, 0.95)

    digits = re.sub(r"\D", "", aadhaar_no)
    if len(digits) == 12:
        _emit("aadhaar_no", f"{digits[:4]} {digits[4:8]} {digits[8:]}", 0.97)

    pin = re.sub(r"\D", "", pincode)
    if len(pin) == 6 and pin[0] != "0":
        _emit("pres_pincode", pin, 0.97)
    else:
        pin = ""

    if house.strip():
        _emit("pres_house_no", house.strip(), 0.95)
    if street.strip():
        _emit("pres_street", street.strip(), 0.95)
    loc_val = _assemble_address([landmark, location, vtc, po])
    if loc_val:
        _emit("pres_location", loc_val, 0.95)
    if subdist.strip():
        _emit("pres_mandal", subdist.strip(), 0.94)
    if district.strip():
        _emit("pres_district", district.strip(), 0.96)
    if state.strip():
        _emit("pres_state", state.strip(), 0.96)

    addr = _assemble_address(
        [care_of, house, street, landmark, location, vtc, po, subdist, district, state, pin]
    )
    if addr:
        _emit("address", addr, 0.97)

    return out


def decode_aadhaar_secure_qr(payload: str) -> list[dict[str, Any]] | None:
    """Decode official UIDAI Secure QR V2/V3/V4 big-integer zlib payload."""
    s = (payload or "").strip()
    if len(s) < 60 or not s.isdigit():
        return None
    try:
        big_int = int(s)
        raw_bytes = big_int.to_bytes((big_int.bit_length() + 7) // 8, byteorder="big")
        decompressed = zlib.decompress(raw_bytes)
    except Exception:
        return None

    parts = decompressed.split(b"\xff")
    if len(parts) < 16:
        return None

    def _dec(b: bytes) -> str:
        return b.decode("iso-8859-1", errors="ignore").strip()

    first = _dec(parts[0])
    # UIDAI V2/V3/V4 puts version marker ("V2"/"V3"/"V4") at index 0, followed by
    # email/mobile indicator at index 1; older V1/unversioned starts with indicator at 0.
    offset = 1 if re.fullmatch(r"V\d+", first, re.IGNORECASE) else 0
    if len(parts) < offset + 16:
        return None

    name = _dec(parts[offset + 2])
    dob = _dec(parts[offset + 3])
    gender = _dec(parts[offset + 4])
    care_of = _dec(parts[offset + 5])
    district = _dec(parts[offset + 6])
    landmark = _dec(parts[offset + 7])
    house = _dec(parts[offset + 8])
    location = _dec(parts[offset + 9])
    pincode = _dec(parts[offset + 10])
    po = _dec(parts[offset + 11])
    state = _dec(parts[offset + 12])
    street = _dec(parts[offset + 13])
    subdist = _dec(parts[offset + 14])
    vtc = _dec(parts[offset + 15])

    cands = _build_aadhaar_candidates(
        name=name, dob=dob, gender=gender, care_of=care_of,
        house=house, street=street, landmark=landmark, location=location,
        vtc=vtc, po=po, subdist=subdist, district=district, state=state,
        pincode=pincode, method="qr:aadhaar-secure",
    )
    return cands or None


_XML_ATTR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"')


def decode_aadhaar_xml_qr(payload: str) -> list[dict[str, Any]] | None:
    """Decode legacy UIDAI ``<PrintLetterBarcodeData .../>`` XML QR payload safely."""
    s = (payload or "").strip()
    if "PrintLetterBarcodeData" not in s:
        return None
    attrs = {k.lower(): v for k, v in _XML_ATTR_RE.findall(s)}
    if not attrs:
        return None
    cands = _build_aadhaar_candidates(
        name=attrs.get("name", ""),
        dob=attrs.get("dob", ""),
        gender=attrs.get("gender", ""),
        care_of=attrs.get("co", "") or attrs.get("gname", ""),
        house=attrs.get("house", ""),
        street=attrs.get("street", ""),
        landmark=attrs.get("lm", ""),
        location=attrs.get("loc", ""),
        vtc=attrs.get("vtc", ""),
        po=attrs.get("po", ""),
        subdist=attrs.get("subdist", ""),
        district=attrs.get("dist", ""),
        state=attrs.get("state", ""),
        pincode=attrs.get("pc", ""),
        aadhaar_no=attrs.get("uid", ""),
        method="qr:aadhaar-xml",
    )
    return cands or None


_PAN_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")


def decode_pan_qr(payload: str) -> list[dict[str, Any]] | None:
    """Decode plain-text/key-value PAN QR payloads when readable."""
    s = (payload or "").strip()
    if not s or s.isdigit():
        return None
    m_pan = _PAN_RE.search(s.upper())
    if not m_pan:
        return None
    if not any(k in s.lower() for k in ("pan", "income tax", "nsdl", "utiitsl", "name", "father", "dob")):
        return None

    out: list[dict[str, Any]] = [{
        "field_key": "pan_no",
        "value": m_pan.group(1),
        "source_page": 1,
        "method": "qr:pan",
        "confidence": 0.97,
    }]

    def _kv(patterns: tuple[str, ...]) -> str:
        for pat in patterns:
            m = re.search(rf"(?im){pat}\s*[:=]\s*([^\r\n;|,]+)", s)
            if m and m.group(1).strip():
                return m.group(1).strip()
        return ""

    nm = _kv((r"\b(?:applicant\s+)?name", r"\bholder\s+name"))
    if nm and len(re.sub(r"[^A-Za-z]", "", nm)) >= 3 and not any(c.isdigit() for c in nm):
        out.append({"field_key": "full_name", "value": nm, "source_page": 1, "method": "qr:pan", "confidence": 0.96})

    fn = _kv((r"\bfather(?:'?s)?\s*name",))
    if fn and len(re.sub(r"[^A-Za-z]", "", fn)) >= 3 and not any(c.isdigit() for c in fn):
        out.append({"field_key": "father_name", "value": fn, "source_page": 1, "method": "qr:pan", "confidence": 0.95})

    dob_raw = _kv((r"\b(?:date\s+of\s+birth|dob)",))
    norm_dob = _normalize_dob(dob_raw)
    if norm_dob:
        out.append({"field_key": "dob", "value": norm_dob, "source_page": 1, "method": "qr:pan", "confidence": 0.96})

    return out


def extract_qr_candidates(img: Image.Image) -> tuple[list[dict[str, Any]], str | None]:
    """Scan an image for Aadhaar or PAN QR codes. Returns ``(candidates, doc_category)``."""
    payloads = _decode_qr_payloads(img)
    for raw in payloads:
        cands = decode_aadhaar_secure_qr(raw)
        if cands:
            return cands, "aadhaar"
        cands = decode_aadhaar_xml_qr(raw)
        if cands:
            return cands, "aadhaar"
        cands = decode_pan_qr(raw)
        if cands:
            return cands, "pan"
    return [], None
