# Phase 4A extraction fixes (local, privacy-safe)

Date: 2026-10-16 · Host: Windows, Python 3.14.7 (Tesseract) and the
Python 3.11 PaddleOCR challenger venv. This work is **uncommitted by
design**; review before committing. No document images, OCR text,
references, sidecars or personal values are stored in Git — every number
below is a redacted metric (lengths, counts, booleans).

## Context

Phase 3 evaluated six local learner-licence documents (all references
`operator-verified`) and identified four evidence-backed rule follow-ups
([phase3_ocr_evaluation.md](phase3_ocr_evaluation.md), *Next steps*
item 2). Phase 4A implements and re-measures exactly those four; the
earlier report-only decision is superseded for this scope. No OCR
engine, UI, database, matching or preprocessing behavior changed.

## What changed (deterministic; no cloud, no LLM, no new dependencies)

- `app/services/fields.py`:
  - Label matching treats whitespace *inside* multi-word labels as
    optional, because the audited OCR fuses label words
    (`PresentAddress`, `DisplayName`). Slash-spaced labels
    (`son / wife / daughter`) therefore also match hugging forms.
  - `full_name`: `display name` is now a specific label. When any
    *specific* name label yields a candidate, bare `Name` rows are not
    emitted for that document — template-B documents print both rows
    and the bare row is a shorter, unrelated one.
  - `father_name`: template-B form labels `son / wife / daughter of`
    and `son / wife / daughter` added (OCR often drops or fuses the
    `of`). The label is anchored to the line start; mid-line prose is
    never matched.
  - `address`: labeled blocks stop at any configured field label,
    including `application_no`; identical blocks emit one candidate; a
    block may extend **one line past `max_lines` only through its
    closing 6-digit PIN line** (the audited family prints five-line,
    PIN-terminated addresses). The PIN is the only signal allowed to
    extend the window — without it the extra line is not address
    content.
  - standalone `No <digits>` licence fallback: rejected when the digits
    appear in a recognized application-number area (same digits on an
    application-labeled line, or an adjacent application label whose own
    number is missing — the OCR split that row). Otherwise the candidate
    is **kept at confidence 0.55** — visibly low-confidence, never
    guessed, never silently discarded.
- `ocr_rules.json` version `"3"` → `"4"`: label lists above;
  `_labels_note` updated.
- `tests/test_fields_phase4a.py`: 23 new synthetic regression tests
  covering all four areas (fixtures only, no document data).

Explicitly unchanged: Flask UI, review workflow, SQLite schema, case
matching, Inbox watcher, document preparation, preprocessing algorithms,
OCR engines (`PaddleOCR` stays opt-in / off by default), scoring and
normalization. No OCR variant or engine became a global default — six
documents cannot select one.

## Evaluation protocol

- Historical reports were preserved untouched; every new run wrote a
  new timestamped report.
- Before scoring, each of the six references was re-checked: sidecar
  status `operator-verified` **and** the current reference hash equal to
  the hash recorded in the historical reports of **both** engines. All
  six passed; document bytes were unchanged (report stems still match
  the current file hashes).
- All results are **full end-to-end runs** (preprocess + OCR + parse).
  No parser-only numbers were produced: the redacted reports contain no
  raw OCR text by design, and no trustworthy stored OCR outputs existed
  for these variants.
- Scoring/normalization were not modified, so before/after numbers come
  from identical harness semantics (`ocr_rules` version recorded in
  provenance; before = v3, after = v4).

## Tesseract results (before → after)

New reports: `benchmark_20261016T0404{16,21,34,41,49,56}_<sha16>.json`
(one per document, three variants each). Expected-field counts per
document follow the reference (template-B references have no
`application_no`; five of six fields are scorable there).

| doc (sha16)        | variant       | exp | match b→a | fp b→a | after: matched fields / fp |
| ------------------ | ------------- | --- | --------- | ------ | -------------------------- |
| `ca0adcdd3d4f39a4` | baseline      | 5   | 1→1       | 1→1    | full_name / address        |
| `ca0adcdd3d4f39a4` | opencv-gray   | 5   | 1→1       | 1→1    | full_name / address        |
| `ca0adcdd3d4f39a4` | opencv-thresh | 5   | 0→0       | 1→1    | — / address                |
| `91c36e3c204e55e3` | baseline      | 6   | 0→0       | 1→1    | — / licence_no (now low-confidence) |
| `91c36e3c204e55e3` | opencv-gray   | 6   | 0→0       | 1→1    | — / licence_no (now low-confidence) |
| `91c36e3c204e55e3` | opencv-thresh | 6   | 1→1       | 2→2    | father_name / licence_no, address |
| `8a39e189be58e357` | baseline      | 5   | 2→4       | 2→1    | full_name, father_name, dob, licence_no / address |
| `8a39e189be58e357` | opencv-gray   | 5   | 2→5       | 2→1    | full_name, father_name, dob, licence_no, address / address (extra section candidates) |
| `8a39e189be58e357` | opencv-thresh | 5   | 0→1       | 1→1    | father_name / full_name    |
| `40419f8357dbc408` | baseline      | 6   | 6→6       | 1→1    | all six / address (extra section candidate) |
| `40419f8357dbc408` | opencv-gray   | 6   | 6→6       | 1→1    | all six / address (extra section candidate) |
| `40419f8357dbc408` | opencv-thresh | 6   | 1→1       | 2→2    | father_name / full_name, address |
| `65ce8c6200a4c454` | baseline      | 5   | 0→0       | 0→0    | — / —                      |
| `65ce8c6200a4c454` | opencv-gray   | 5   | 0→0       | 0→0    | — / —                      |
| `65ce8c6200a4c454` | opencv-thresh | 5   | 0→0       | 0→0    | — / —                      |
| `0d5b2112f93e1b94` | baseline      | 6   | 1→1       | 1→1    | application_no / address   |
| `0d5b2112f93e1b94` | opencv-gray   | 6   | 1→1       | 1→1    | application_no / address   |
| `0d5b2112f93e1b94` | opencv-thresh | 6   | 4→4       | 2→2    | full_name, father_name, dob, application_no / licence_no, address |

**Totals: exact-match 26/99 → 32/99 (+6); false-positive flags 20 → 18
(−2); no field became worse.** Per-variant matches after: baseline
12/33, opencv-gray 13/33, opencv-thresh 7/33.

### What the four targeted fixes changed (Tesseract)

1. **Template-B applicant name (`display name` + precedence).**
   `8a39e189`: baseline and gray now extract the correct 21-char name
   (was a wrong 10-char bare-`Name` candidate); opencv-thresh still
   misses (15 chars — OCR truncated the name row; no guessing allowed).
2. **Template-B father row (`son / wife / daughter [of]`).**
   `8a39e189`: 0 → 19-char exact on **all three variants**. On
   `ca0adcdd` the row is physically hidden on the photo (no OCR line),
   so it stays a miss — OCR-stage, not parser-stage.
3. **Address blocks.** The 102-char over-capture on `8a39e189` is gone
   (bounded to its three real sections; gray's present block is now
   **77-char exact**). `ca0adcdd` present block no longer truncates:
   57 → 81 (baseline) / 79 (gray) against an 80-char reference — a
   residual ±1-character OCR punctuation difference, honestly reported
   as a miss. `ca0adcdd` thresh still loses the PIN formatting in OCR
   (57, miss). Fused `PresentAddress`/`PermanentAddress` OCR lines now
   both end a block and start their own (no merging).
4. **Standalone `No <digits>` licence fallback.** On `91c36e3c` the
   ambiguous 10-digit candidate (equal to the printed application-number
   length) is no longer silent: it is kept at confidence 0.55 and
   surfaces as `low_confidence` (baseline and gray). It is not
   suppressed because the document's OCR has no application-number label
   anywhere — the attribution is genuinely ambiguous, and the same
   pattern is a legitimate licence-only case elsewhere.

## PaddleOCR results (before → after)

Same six documents, same three variants, full end-to-end runs in the
separate Python 3.11 venv (stock pipeline and adapter unchanged). New
reports: `benchmark_20261016T04{1232,1618,2318,2810,3345,3824}_<sha16>.json`.

| doc (sha16)        | variant       | exp | match b→a | fp b→a | after: matched fields / fp |
| ------------------ | ------------- | --- | --------- | ------ | -------------------------- |
| `ca0adcdd3d4f39a4` | baseline      | 5   | 0→0       | 1→1    | — / address                |
| `ca0adcdd3d4f39a4` | opencv-gray   | 5   | 0→0       | 1→1    | — / address                |
| `ca0adcdd3d4f39a4` | opencv-thresh | 5   | 0→0       | 1→1    | — / address                |
| `91c36e3c204e55e3` | baseline      | 6   | 4→4       | 1→1    | full_name, father_name, dob, application_no / address |
| `91c36e3c204e55e3` | opencv-gray   | 6   | 5→5       | 1→1    | full_name, father_name, dob, licence_no, application_no / address |
| `91c36e3c204e55e3` | opencv-thresh | 6   | 1→1       | 1→1    | father_name / address      |
| `8a39e189be58e357` | baseline      | 5   | 2→4       | 1→2    | father_name, dob, licence_no, address / full_name, address |
| `8a39e189be58e357` | opencv-gray   | 5   | 2→5       | 2→1    | all five / address (extra section candidates) |
| `8a39e189be58e357` | opencv-thresh | 5   | 2→4       | 2→1    | full_name, father_name, dob, licence_no / address |
| `40419f8357dbc408` | baseline      | 6   | 5→6       | 1→1    | all six / address (extra section candidate) |
| `40419f8357dbc408` | opencv-gray   | 6   | 5→6       | 1→1    | all six / address (extra section candidate) |
| `40419f8357dbc408` | opencv-thresh | 6   | 5→6       | 1→1    | all six / address (extra section candidate) |
| `65ce8c6200a4c454` | baseline      | 5   | 2→2       | 1→2    | dob, licence_no / full_name, address |
| `65ce8c6200a4c454` | opencv-gray   | 5   | 2→3       | 1→2    | father_name, dob, licence_no / full_name, address |
| `65ce8c6200a4c454` | opencv-thresh | 5   | 0→0       | 1→1    | — / address                |
| `0d5b2112f93e1b94` | baseline      | 6   | 4→5       | 1→1    | full_name, father_name, dob, licence_no, application_no / address |
| `0d5b2112f93e1b94` | opencv-gray   | 6   | 4→5       | 1→1    | full_name, father_name, dob, licence_no, application_no / address |
| `0d5b2112f93e1b94` | opencv-thresh | 6   | 4→5       | 1→1    | full_name, father_name, dob, licence_no, application_no / address |

**Totals: exact-match 47/99 → 61/99 (+14); false-positive flags 20 → 21
(+1).** Per-variant matches after: baseline 21/33, gray 24/33,
opencv-thresh 16/33 (was 17/18/12).

### What changed here

- The spacing-tolerant label matching also recovered the merged
  application-number rows this engine emits: fused `ApplNo`-family
  forms had no literal label before, and `application_no` is now exact
  on `40419f8357dbc408` and `0d5b2112f93e1b94` — all three variants
  each — turning the historical PaddleOCR `application_no` regression
  into +6 exact matches (OCR output unchanged; parser-only effect; the
  fused forms are the same class of evidence as `PresentAddress`).
- Template-B rows: `8a39e189` gray and thresh now extract the exact
  21-char name, **all three variants** the exact 19-char father name,
  and the present-address block is exact on baseline and gray (77)
  where it was previously three wrong candidates;
  `65ce8c6200a4c454` gray gains the exact 16-char father name.
- `40419f8357dbc408` is now 6/6 on all three variants (was 5/6), with
  the documented multi-section `address` false-positive flag remaining.
- `91c36e3c` is unchanged under this engine (4/5/1 matches as before;
  its standalone-line ambiguity does not arise here — this engine reads
  that line differently, so no `licence_no` candidate exists to flag).
- Address candidates that remain misses improved in length
  (`ca0adcdd` thresh 56/58 → 58/78; `65ce8c6200a4c454` 54 → 76) but
  are still not exact.

### New false positives introduced (honest, not hidden)

- `8a39e189` baseline `full_name` (32 chars vs 21) and
  `65ce8c6200a4c454` baseline/gray `full_name` (23 vs 24): the
  display-name row is now recognized, but on these noisy OCR passes its
  value merges extra photo tokens. These are genuine label-row
  readings, flagged as false positives for operator attention — no
  trimming was invented to hide them. Net false-positive flags: +1
  (3 new, 2 previously-wrong candidates resolved).


## Remaining ambiguities (documented, deliberately not "fixed")

- **Two annotated address sections.** The scorer counts every
  non-matching address candidate as a false positive even when another
  candidate matches, so documents whose references annotate only one
  section (`40419f8357dbc408`, `8a39e189` gray) keep an `address`
  false-positive flag while matching. A present-vs-permanent selection
  rule was **rejected**: the reference evidence is mixed (one reference
  matches its permanent block, one matches its present block, one
  matches the fixed present block), so any rule would overfit. Both
  sections stay visible for the operator.
- **Near-misses are not guessed.** `ca0adcdd` 81/79 vs 80 and
  `8a39e189` thresh name 15 vs 21 are OCR character-level variance;
  no digit/character correction was added.
- **`91c36e3c` standalone licence candidate** remains a (low-confidence)
  false positive against the reference under Tesseract: no evidence
  exists in its OCR to attribute the digits either way; silently
  dropping it would break the legitimate no-label case covered by
  tests. (Under PaddleOCR the same line never matches the standalone
  pattern, so the ambiguity does not arise there.)
- Noisy OCR display-name rows can surface wrong candidates (PaddleOCR:
  23 vs 24 and 32 vs 21 chars): the row is real, the value merges photo
  tokens; these are flagged false positives, deliberately not trimmed
  or corrected.
- `65ce8c6200a4c454` (dark photo): zero Tesseract candidates on all
  variants; PaddleOCR produces real-but-imprecise candidates (one is
  now exact: `father_name` on gray). Remaining misses are OCR-stage.
- Six-document convenience sample: before/after deltas are evidence for
  *these* documents and their layouts only; no accuracy claim, and no
  variant/engine default is selected from six samples.
