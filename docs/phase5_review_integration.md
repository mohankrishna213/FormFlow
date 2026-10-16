# Phase 5.1 — Six-field review integration

Scope: complete the six-field (`full_name`, `father_name`, `dob`, `address`,
`licence_no`, `application_no`) operator review workflow in the existing Flask
app. No OCR model, heuristic, scoring, or preprocessing changes. No Phase 4A
extraction-contract changes. No Chrome extension work (it does not exist yet).

## What changed in the review workflow

- `app/services/review.py`
  - `REVIEW_FIELDS` now lists all six keys; `MANUAL_FIELDS` (five) become
    `method='manual'` rows on save. `application_no` is persisted at case
    level instead (see below), so candidates stay evidence and the official
    value is not duplicated.
  - `FIELD_LABELS` / `FORM_KEY` map internal keys to display labels and to the
    review-form input names (`application_no` keeps the existing
    `appl_number` input/column name).
  - `father_name` and `application_no` got field-specific validation using the
    existing parsers (`parse_name`, `APPL_RE`); both optional (blank OK).
  - Conflict handling: a conflicted key still blocks an unchanged value, but
    the operator can now explicitly pick a listed candidate
    (`choice_<field_key>` radio) or type a correction. A recorded explicit
    selection (manual value, or a saved `cases.appl_number`) is honoured on
    later saves instead of re-blocking the operator's own value. A pick also
    survives a failed validation re-render (text box and checked radio).
  - New helpers: `resolved_conflict_keys`, `review_prefill`,
    `collect_form_values`, `with_sources`, `candidate_source`.
- `app/routes/cases.py` — review routes forward the `father_name` input and
  conflict selections; templates receive `prefill`, `labels`,
  `resolved_fields` and document/ source labels.
- `app/templates/review.html` — six labelled inputs with source/provenance and
  confidence (low-confidence flagged), conflict list with per-candidate
  provenance and selection radios for unresolved conflicts, and a
  non-JavaScript-safe pick handler (radio value wins server-side).
- `app/templates/case_detail.html` — shows the six keys with None-safe
  display, document/page provenance, and conflict rows with page numbers.
- `app/templates/base.html` — small styles for the conflict pick block.

No database migration was needed: `extracted_fields` already stores all six
candidate keys, and `cases.appl_number` already exists.

## Six internal field keys and displayed labels

| Internal key | Review UI label | Notes |
|---|---|---|
| `full_name` | Full name | required; validated 2–5 letters-only words |
| `father_name` | Father's / related person's name | optional |
| `dob` | Date of birth | required; ISO `YYYY-MM-DD`; ambiguous dates rejected |
| `address` | Address | optional; present/permanent candidates never combined |
| `licence_no` | Driving licence number | optional; existing `parse_licence_no` validation |
| `application_no` | Application number | optional; OCR candidate key; official value → `cases.appl_number` |

## Printed document labels vs internal field keys

Printed labels are recognition inputs only (configured in `ocr_rules.json`);
they are never review field keys. `full_name` is not renamed to
`display_name` even though the parser recognizes the printed "Display Name"
label. The `father_name` key intentionally covers more than one printed label
family and is not split into `related_person_name`.

## `father_name` and the "Son/Wife/Daughter of" rows

The Phase 4A parser emits `father_name` for both the "Father's Name" family
and the "Son/Wife/Daughter of" family. The current data model does **not**
retain which printed label matched (candidate rows store value, page, method,
and confidence only), so the review UI uses the neutral label and shows a
short hint that either printed label may apply. No relationship is inferred
from the value; the operator checks the document. This is a reported
limitation, not a silent extraction-contract change.

## `application_no` → `cases.appl_number` mapping

- OCR candidates keep `field_key='application_no'` with their provenance.
- The review input is `appl_number` (existing form/column name). It prefills
  with the saved `cases.appl_number` when present, otherwise with the best
  `application_no` candidate as an editable draft (never invented).
- On save, a valid non-empty value is written to `cases.appl_number`; an
  empty/missing OCR candidate can never erase a saved official value (the
  prefill always shows the saved value), and the value is never stored twice.
- `application_no` conflicts use the same explicit-resolution rule as other
  keys; a saved official number is the recorded resolution.
- Prepared-file naming (`build_prepared_filename`) only uses `appl_number`
  when explicitly supplied at prepare time; review saves never rename
  existing prepared files.

## Conflict and missing-value behavior

- Conflicts are detected with the existing normalization (`normalize_name`
  for name fields, lowercase otherwise) over non-manual rows.
- An unchanged conflicted field blocks Validate & Save and lists every
  candidate with source and page. Resolution paths: pick a listed candidate
  (radio) or type a correction.
- Regression fix: previously the operator could never confirm the current
  best candidate (equality could not distinguish "untouched" from "picked"),
  and after resolving a conflict every later save of the same value was
  blocked again. Both are fixed: explicit picks are recorded even when equal
  to the best candidate, and a recorded selection is not re-blocked.
- Missing optional fields stay blank, are never fabricated, and do not fail
  validation. Validation failure saves nothing and never marks the case
  reviewed. Reopening shows persisted operator values, not stale candidates.

## Commands run and results

```
python -m pytest tests/test_review.py -q                     # 14 passed
python -m pytest tests/test_review_six_fields.py -q          # 25 passed
python -m pytest tests/test_review_six_fields.py tests/test_review.py -q  # 39 passed
python -m pytest -q                                          # 242 passed
```

(Phase 4A baseline was 217 passed; this phase adds 25 tests, none removed.)

Scripted end-to-end check (synthetic page text → `extract_candidates` →
`attach_or_provisional` → review GET/POST → reconnect → reopen) is covered by
`tests/test_review_six_fields.py::test_extract_attach_review_save_end_to_end`
and a local render smoke run. Browser-level visual verification and any
Chrome-extension steps remain manual/future work.

## Remaining limitations

- The printed label family for `father_name` is not retained per candidate
  (data-model limit; would need an extraction-metadata change).
- `case_detail` lists raw internal keys (existing presentation), while the
  review form uses display labels.
- Conflict "resolved" state is per recorded selection: if new conflicting
  evidence arrives later (attach/merge), the conflict is listed again but does
  not re-block; the case is flagged `needs_attention` by the attach flow.
- No browser automation or extension exists yet (Phases 6–7 remain future).
