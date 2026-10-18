"""Reusable Field Catalogue and Schema Definitions (Phase 2).

Provides an extensible, local, deterministic field catalogue:
- Canonical keys, human-friendly labels, categories, data types, validation rules,
  and alias mappings.
- Reusable customer-level profile fields vs case-specific fields.
- Security constraints: bounded key/value sizes, strict key name syntax, and
  reserved-key protection.
- Fully backwards-compatible with FormFlow's original six fields.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_KEY_LENGTH = 64
MAX_VALUE_LENGTH = 1000
MAX_FIELDS_PER_CASE = 50

FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

RESERVED_KEYS = frozenset({
    "csrf_token",
    "case_id",
    "submit",
    "action",
    "status",
    "review_version",
    "created_at",
    "updated_at",
    "id",
    "customer_id",
    "__proto__",
    "constructor",
    "prototype",
})

CATEGORIES = [
    ("identity", "Personal & Identity Details"),
    ("contact", "Contact Information"),
    ("address", "Residential Address"),
    ("licence", "Licence Details"),
    ("application", "Application Details"),
    ("other", "Other Extracted Fields"),
]

CATEGORY_LABELS = dict(CATEGORIES)


@dataclass
class FieldDef:
    key: str
    label: str
    category: str = "other"
    data_type: str = "string"  # "string", "date", "select", "address"
    required: bool = False
    scope: str = "case"  # "customer" (reusable profile) or "case" (case-specific)
    options: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    description: str = ""


# Default catalogue entries
DEFAULT_CATALOGUE: list[FieldDef] = [
    # Core Identity (Customer Reusable)
    FieldDef(
        key="full_name",
        label="Full name",
        category="identity",
        data_type="string",
        required=True,
        scope="customer",
        aliases=["name", "display_name", "applicant_name", "holder_name", "full_name_as_per_records"],
        description="Official full name of applicant",
    ),
    FieldDef(
        key="father_name",
        label="Father's / related person's name",
        category="identity",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["fathers_name", "father", "relation_name", "guardian_name", "son_wife_daughter_of"],
        description="Name of father, mother, spouse, or guardian",
    ),
    FieldDef(
        key="dob",
        label="Date of birth",
        category="identity",
        data_type="date",
        required=True,
        scope="customer",
        aliases=["date_of_birth", "birth_date", "birthdate", "d_o_b"],
        description="Date of birth in YYYY-MM-DD format",
    ),
    FieldDef(
        key="gender",
        label="Gender",
        category="identity",
        data_type="select",
        required=False,
        scope="customer",
        options=["Male", "Female", "Transgender"],
        aliases=["sex"],
        description="Applicant gender",
    ),
    FieldDef(
        key="place_of_birth",
        label="Place of birth",
        category="identity",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["birth_place", "pob"],
        description="City/town of birth",
    ),
    FieldDef(
        key="blood_group",
        label="Blood group",
        category="identity",
        data_type="select",
        required=False,
        scope="customer",
        options=["Unknown", "A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"],
        aliases=["blood_grp", "bloodgroup"],
        description="Applicant blood group",
    ),
    FieldDef(
        key="qualification",
        label="Educational qualification",
        category="identity",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["education", "edu_qualification"],
        description="Highest educational qualification",
    ),
    FieldDef(
        key="id_mark_1",
        label="Identification mark 1",
        category="identity",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["identification_mark_1", "mark_1"],
        description="Visible physical identification mark",
    ),
    FieldDef(
        key="id_mark_2",
        label="Identification mark 2",
        category="identity",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["identification_mark_2", "mark_2"],
        description="Second visible identification mark",
    ),

    # Contact Details (Customer Reusable)
    FieldDef(
        key="mobile_no",
        label="Mobile number",
        category="contact",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["mobile", "phone", "contact_no", "cell_phone"],
        description="Primary mobile phone number",
    ),
    FieldDef(
        key="email",
        label="Email ID",
        category="contact",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["email_id", "email_address"],
        description="Applicant email address",
    ),
    FieldDef(
        key="emergency_mobile_no",
        label="Emergency mobile number",
        category="contact",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["emergency_phone", "alt_mobile", "alternate_mobile"],
        description="Emergency contact number",
    ),

    # Address (Customer Reusable)
    FieldDef(
        key="address",
        label="Address",
        category="address",
        data_type="address",
        required=False,
        scope="customer",
        aliases=["present_address", "residence", "residential_address"],
        description="Complete residential address",
    ),
    FieldDef(
        key="pres_house_no",
        label="Present house/door/flat no",
        category="address",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["house_no", "door_no", "flat_no"],
        description="House, door, or flat number",
    ),
    FieldDef(
        key="pres_street",
        label="Present street/locality",
        category="address",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["street", "locality", "street_name"],
        description="Street, road, or locality",
    ),
    FieldDef(
        key="pres_location",
        label="Present location/landmark",
        category="address",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["location", "landmark", "sub_district"],
        description="Sub-district, location, or landmark",
    ),
    FieldDef(
        key="pres_pincode",
        label="Present pincode",
        category="address",
        data_type="string",
        required=False,
        scope="customer",
        aliases=["pincode", "pin_code"],
        description="6-digit postal index number",
    ),
    FieldDef(
        key="perm_house_no",
        label="Permanent house/door/flat no",
        category="address",
        data_type="string",
        required=False,
        scope="customer",
        description="Permanent house number",
    ),
    FieldDef(
        key="perm_street",
        label="Permanent street/locality",
        category="address",
        data_type="string",
        required=False,
        scope="customer",
        description="Permanent street name",
    ),
    FieldDef(
        key="perm_location",
        label="Permanent location/landmark",
        category="address",
        data_type="string",
        required=False,
        scope="customer",
        description="Permanent landmark/location",
    ),
    FieldDef(
        key="perm_pincode",
        label="Permanent pincode",
        category="address",
        data_type="string",
        required=False,
        scope="customer",
        description="Permanent 6-digit PIN code",
    ),
    FieldDef(
        key="stay_duration",
        label="Duration of stay at present address",
        category="address",
        data_type="string",
        required=False,
        scope="case",
        description="Duration of stay in years/months",
    ),

    # Licence Details (Case Specific)
    FieldDef(
        key="licence_no",
        label="Learner's Licence Number",
        category="licence",
        data_type="string",
        required=False,
        scope="case",
        aliases=["dl_no", "ll_no", "license_no", "learning_licence_no", "driving_licence_no"],
        description="Driving or Learner's Licence Number",
    ),

    # Application Details (Case Specific)
    FieldDef(
        key="application_no",
        label="Application number",
        category="application",
        data_type="string",
        required=False,
        scope="case",
        aliases=["appl_no", "app_no", "appl_number", "application_number"],
        description="Official Parivahan/Sarathi application number",
    ),
    FieldDef(
        key="rto_code",
        label="RTO office",
        category="application",
        data_type="string",
        required=False,
        scope="case",
        aliases=["rto", "rto_office"],
        description="RTO jurisdiction office code",
    ),
]


def validate_field_key(key: str) -> None:
    """Ensure field key conforms to security syntax limits."""
    if not key or not isinstance(key, str):
        raise ValueError("Field key must be a non-empty string.")
    k = key.strip()
    if len(k) > MAX_KEY_LENGTH:
        raise ValueError(f"Field key '{k[:20]}...' exceeds maximum length of {MAX_KEY_LENGTH} chars.")
    if k in RESERVED_KEYS:
        raise ValueError(f"Field key '{k}' is a reserved internal identifier.")
    if not FIELD_KEY_RE.match(k):
        raise ValueError(
            f"Invalid field key '{k}'. Must start with a letter and contain only lowercase letters, digits, and underscores."
        )


def sanitize_field_value(val: str | None) -> str:
    """Trim, enforce length bounds, and strip control characters."""
    if val is None:
        return ""
    s = str(val).strip()
    if len(s) > MAX_VALUE_LENGTH:
        s = s[:MAX_VALUE_LENGTH]
    # Strip dangerous control chars (preserve newlines and tabs)
    return "".join(ch for ch in s if ch in ("\n", "\r", "\t") or ord(ch) >= 32)


class FieldCatalog:
    """Manages field definitions, alias lookups, categories, and validation."""

    def __init__(self, definitions: list[FieldDef] | None = None):
        self._fields: dict[str, FieldDef] = {}
        self._alias_map: dict[str, str] = {}
        for f in (definitions or DEFAULT_CATALOGUE):
            self.register(f)

    def register(self, f: FieldDef) -> None:
        validate_field_key(f.key)
        self._fields[f.key] = f
        self._alias_map[f.key.lower()] = f.key
        for a in f.aliases:
            self._alias_map[a.lower()] = f.key

    def get(self, key: str) -> FieldDef | None:
        canonical = self.resolve_key(key)
        return self._fields.get(canonical)

    def resolve_key(self, key: str) -> str:
        """Resolve an alias or raw key to its canonical catalogue key."""
        k = (key or "").strip().lower()
        return self._alias_map.get(k, key.strip())

    def get_label(self, key: str) -> str:
        f = self.get(key)
        if f:
            return f.label
        # Format unknown keys nicely: 'passport_no' -> 'Passport no'
        cleaned = key.replace("_", " ").strip()
        return cleaned.capitalize() if cleaned else key

    def get_category(self, key: str) -> str:
        f = self.get(key)
        return f.category if f else "other"

    def is_customer_scoped(self, key: str) -> bool:
        f = self.get(key)
        return (f.scope == "customer") if f else False

    def list_fields(self) -> list[FieldDef]:
        return list(self._fields.values())

    def list_keys(self) -> list[str]:
        return list(self._fields.keys())


_GLOBAL_CATALOG = FieldCatalog()


def get_catalog() -> FieldCatalog:
    return _GLOBAL_CATALOG

