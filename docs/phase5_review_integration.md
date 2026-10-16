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
  - New helpers: `review_prefill`, `collect_form_values`, `with_sources`,
    `candidate_source` (conflict-freshness helpers arrived in Phase 5.1.1,
    see the addendum below).
- `app/routes/cases.py` — review routes forward the `father_name` input and
  conflict selections; templates receive `prefill`, `labels`, conflict
  status and document/source labels.
- `app/templates/review.html` — six labelled inputs with source/provenance and
  confidence (low-confidence flagged), conflict list with per-candidate
  provenance and selection radios for unresolved conflicts, and a
  non-JavaScript-safe pick handler (radio value wins server-side).
- `app/templates/case_detail.html` — shows the six fields with the shared
  `FIELD_LABELS` display labels (Phase 5.1.1), None-safe display,
  document/page provenance, and conflict rows with page numbers.
- `app/templates/base.html` — small styles for the conflict pick block.

No database migration was needed in Phase 5.1: `extracted_fields` already
stores all six candidate keys, and `cases.appl_number` already exists.
Phase 5.1.1 adds one lightweight table (see its addendum).

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
  to the best candidate, and a recorded selection is not re-blocked (Phase
  5.1.1 sharpened this: it holds until contradictory newer evidence arrives —
  see the addendum).
- Missing optional fields stay blank, are never fabricated, and do not fail
  validation. Validation failure saves nothing and never marks the case
  reviewed. Reopening shows persisted operator values, not stale candidates.

## Phase 5.1.1 addendum — conflict-resolution freshness

Original limitation: "resolved" was time-blind (any manual row, or any saved
`appl_number`, permanently suppressed the block), so contradictory evidence
arriving later (attach/merge) was listed but never re-required review.

- New table `conflict_acks` (append-only): one small row per acknowledged
  evidence set — `case_id`, `field_key`, `upto_id` (newest candidate row id
  the operator acknowledged), `value_norm` (the normalized value they settled
  on), `created_at`. OCR/Tesseract candidate rows in `extracted_fields` are
  never deleted or rewritten as evidence.
- Freshness rule: a conflicted key re-blocks an untouched save when candidate
  rows newer than `upto_id` disagree with `value_norm`. New *agreeing*
  evidence never re-opens a resolved conflict; one later contradicting row
  does — also for `application_no`, whose confirmed value stays in
  `cases.appl_number` and is never treated as permanent proof.
- Anchors are written transactionally on each successful review save for
  every conflicted key (only when the `(upto_id, value_norm)` pair changes).
  Keys resolved before the table existed fall back to the row id of their
  latest manual value, so upgrading alone does not force a re-resolve. A
  legacy `application_no` (no manual row) is re-confirmed once before acting
  as an anchor — honest, since there is no historical record of when it was
  chosen.
- UI: a conflicted key that needs action offers the candidate radios plus a
  "Keep your saved value" radio (or "Keep it blank" when the settled value is
  empty; radio value `__keep__`, because empty radio values are
  indistinguishable from "not picked" in form data). Picks survive
  failed-validation re-renders. The untouched-value block compares against
  what the form showed (prefill); for `application_no` that is the saved
  official value, since the old comparison against the newest OCR candidate
  could silently accept an untouched resubmit.
- Merge drops the source case's acknowledgements (they covered the source's
  own evidence only); merged rows keep their ids, so contradictions they
  bring in count as newer evidence and re-require review. Assignment marks
  `needs_attention` as before and leaves anchors intact.
- `case_detail.html` now renders the shared `FIELD_LABELS` instead of raw
  internal keys (single source of display labels, values/provenance intact).
- Migration: `CREATE TABLE IF NOT EXISTS conflict_acks` (+ index) runs inside
  the regular `init_db` path on every app start — non-destructive and
  idempotent, consistent with the existing lightweight migration conventions
  (`_migrate` column adds; tables via `SCHEMA`).

## Commands run and results

```
python -m pytest tests/test_review.py -q                     # 14 passed
python -m pytest tests/test_review_six_fields.py -q          # 25 passed
python -m pytest tests/test_review_six_fields.py tests/test_review.py -q  # 39 passed
python -m pytest -q                                          # 242 passed
```

(Phase 4A baseline was 217 passed; Phase 5.1 added 25 tests, none removed.)

Phase 5.1.1 (freshness fix, on the same tree):

```
python -m pytest tests/test_review.py tests/test_review_six_fields.py -q  # 39 passed
python -m pytest tests/test_review_freshness.py -q           # 11 passed
python -m pytest tests/test_db_cases.py -q                   # 8 passed
python -m pytest -q                                          # 254 passed
```

(+12 tests on the 242 baseline: 11 freshness regressions
(`tests/test_review_freshness.py`) and 1 non-destructive migration test
(`tests/test_db_cases.py::test_reinit_adds_conflict_acks_without_data_loss`).)

Scripted end-to-end check (synthetic page text → `extract_candidates` →
`attach_or_provisional` → review GET/POST → reconnect → reopen) is covered by
`tests/test_review_six_fields.py::test_extract_attach_review_save_end_to_end`
and a local render smoke run. Browser-level visual verification and any
Chrome-extension steps remain manual/future work.

## Remaining limitations

- The printed label family for `father_name` is not retained per candidate
  (data-model limit; would need an extraction-metadata change).
- Freshness ordering uses `extracted_fields.id` arrival order (merge keeps
  ids). A pre-5.1.1 case whose `application_no` was confirmed before any
  anchor existed is re-confirmed once before its saved number acts as an
  anchor; non-empty legacy manual values anchor immediately via their row id.
- No browser automation or extension exists yet (Phases 6–7 remain future).
