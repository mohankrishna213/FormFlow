# FormFlow — Phase 2: Extensible Customer/Case Fields and Review Architecture

## Overview

Phase 2 replaces the fixed six-field ceiling with a reusable, extensible field catalogue while preserving 100% backward compatibility with existing reviewed cases, case history, conflict handling, application-number handling, and universal autofill APIs.

---

## 1. Field Catalogue & Alias Model (`app/services/catalog.py`)

The field catalogue is a local, deterministic, configuration-driven system that does not require an LLM or network dependencies.

### Core Concepts

- **Canonical Keys**: Stable, lowercased, identifier-safe keys (`^[a-z][a-z0-9_]{1,63}$`).
- **Human-Friendly Display Labels**: Proper title-cased labels for user interfaces.
- **Alias Resolution**: Maps legacy parser keys, OCR variants, or portal identifiers to canonical keys (e.g. `pincode` &rarr; `pres_pincode`, `mobile` &rarr; `mobile_no`, `sex` &rarr; `gender`, `appl_no` &rarr; `application_no`, `birth_date` &rarr; `dob`).
- **Categories**: Logical groupings for UI rendering:
  - `identity`: Personal & Identity Details (`full_name`, `dob`, `gender`, `father_name`, `blood_group`, etc.)
  - `contact`: Contact Information (`mobile_no`, `email`, `emergency_mobile_no`)
  - `address`: Residential Address (`address`, `pres_house_no`, `pres_street`, `pres_pincode`, etc.)
  - `licence`: Licence Details (`licence_no`)
  - `application`: Application Details (`application_no`, `rto_code`)
  - `other`: Unrecognised or arbitrary custom extracted keys
- **Scope**:
  - `customer`: Reusable customer profile data (name, DOB, gender, permanent address, contact details).
  - `case`: Case-specific transactional data (`application_no`, `licence_no`, `stay_duration`, `rto_code`).

### Security Limits & Auditing

- **Key Limits**: Max 64 characters, strict regex `^[a-z][a-z0-9_]{1,63}$`.
- **Reserved Identifier Protection**: Internal identifiers (`csrf_token`, `case_id`, `submit`, `action`, `status`, `review_version`, `__proto__`, `constructor`, `prototype`, etc.) are rejected as field keys.
- **Value Bounds**: Max 1,000 characters; dangerous non-printable ASCII control characters are stripped while preserving newlines and tabs.
- **Payload Ceiling**: Max 50 fields per case (`MAX_FIELDS_PER_CASE`).

---

## 2. Safe Database Migration & Schema Compatibility (`app/db.py`)

### Schema Addition

A lightweight, dedicated table `customer_fields` is added to manage reusable customer-level profile data without modifying the existing `customers` or `cases` tables:

```sql
CREATE TABLE IF NOT EXISTS customer_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    value TEXT NULL,
    source_case_id INTEGER REFERENCES cases(id) ON DELETE SET NULL,
    source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    confirmed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(customer_id, field_key)
);
CREATE INDEX IF NOT EXISTS idx_customer_fields ON customer_fields(customer_id, field_key);
```

### Migration Safety

- **Idempotent**: Implemented in `_migrate(conn)` and `SCHEMA`. Can run repeatedly on new or existing databases.
- **Zero Data Loss**: Existing rows in `cases`, `customers`, `documents`, `extracted_fields`, `conflict_acks`, and `case_events` remain completely untouched.
- **Tested**: Verified with `tests/test_migration.py` against a database initialized under the pre-Phase 2 schema.

---

## 3. Customer Profile vs Case-Specific Precedence

When assembling candidate values for a case:

1. **Case-Level Confirmed Value**: Manual edits or verified operator values on the case take top priority.
2. **Case-Level OCR Candidates**: Extracted candidate evidence from attached documents takes second priority.
3. **Customer Reusable Profile**: If no case-level candidate exists and the case has an associated `customer_id`, FormFlow falls back to the customer's confirmed profile data from `customer_fields` (`method = 'customer_profile'`).

### Synchronization Rules

When an operator clicks **Validate & Save**:
- Fields with `scope == "customer"` that are confirmed (manual edits, validated identity fields, or explicitly confirmed candidates) are synced into `customer_fields` for that customer.
- Case-specific fields (`application_no`, `licence_no`, `stay_duration`, etc.) are never synced to `customer_fields`.
- Cases without an associated `customer_id` operate completely standalone without profile syncing.

---

## 4. Dynamic Review Interface (`review.py` & `review.html`)

- **Dynamic Catalogue Rendering**: Fields are dynamically loaded from the active catalogue, case candidates, and customer profile.
- **Unrecognised Field Retention**: Extracted keys from OCR that are not in the catalogue are never dropped; they are grouped under *Other Extracted Fields* with provenance badges (`method`, `confidence`, `document_id`, `source_page`).
- **Single Validate & Save Action**: Preserves FormFlow's core philosophy of a single case-level Validate & Save action.
- **Conflict Resolution & Freshness**: Retains multi-candidate display, radio pickers, and `conflict_acks` evidence anchors across all fields.

---

## 5. Autofill API Packet Contract (`fill_packet.py`)

### Eligibility Invariant

A field is `eligible_to_fill: True` only when:
1. Operator manually confirmed it (`method == 'manual'`), OR
2. Official application number exists (`cases.appl_number`), OR
3. Core identity field (`full_name`, `father_name`, `dob`) validated on a reviewed case, OR
4. Customer profile has a confirmed value (`method == 'customer_profile'`).

> [!IMPORTANT]
> Unconfirmed OCR candidates for any field are strictly `eligible_to_fill: False`. Reviewing a case does **NOT** accidentally make every unconfirmed candidate eligible for autofill.

### Workflow Selection

- `dl_application`: Fixed allowlist containing the 6 driving licence application fields.
- `app_status`: Fixed allowlist strictly containing `application_no` and `dob`.
- `auto`: Dynamically exposes all confirmed eligible fields for that case/customer.

---

## 6. Verification and Test Suites

| Test Suite | Scope | Status |
| :--- | :--- | :--- |
| `tests/test_migration.py` | Legacy database migration, idempotency, data preservation | PASS (1/1) |
| `tests/test_field_catalog.py` | Catalogue lookup, aliases, security limits, customer precedence, eligibility | PASS (11/11) |
| Full Python Test Suite | All 356 regression & feature tests | PASS (356/356) |
| Portal Adapter Tests (`node`) | Sarathi DOM matching, LL/DL semantics, label-to-input association | PASS (37/37) |
| Extension API Client Tests (`node`) | Client auth, origin checks, event recording, workflow packets | PASS (18/18) |
