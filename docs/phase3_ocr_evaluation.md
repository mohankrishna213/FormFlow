# Phase 3 OCR evaluation (local, privacy-safe)

Date: 2026-10-15 · Host: Windows, Python 3.14.7 · Tesseract
(`C:\Program Files\Tesseract-OCR\tesseract.exe`), opencv-python-headless
+ numpy 2.5.4. No cloud OCR, no LLM, no real customer documents in Git.
This work is **uncommitted by design**; review before committing.

## Scope

Improve extraction of the six benchmark fields (`full_name`,
`father_name`, `dob`, `licence_no`, `application_no`, `address`) and
determine whether remaining misses are OCR-stage or parser-stage.
Everything below was measured with the existing harness and scoring
rules; scoring/normalization was not touched, so backends stay
comparable. The Flask UI, Inbox watcher, SQLite schema, case matching
and document preparation were not modified.

## What changed (all deterministic; no cloud, no LLM)

- `app/services/fields.py`: new `father_name` parser (labels taken from
  the document's printed "Father's Name" field) and `application_no`
  parser (numeric, strict 8–20 digit bounds; labels include the observed
  OCR forms of the printed "App.No." such as `AppiNo`). Name extraction
  now picks the longest name-shaped token run and trims a documented
  trailing all-lowercase photo-noise fragment (only when 2+ non-lowercase
  tokens precede it). Address segments are tidied (trailing
  punctuation/single-letter artifacts) and label matching normalizes
  whitespace and typographic apostrophes.
- `ocr_rules.json` (version "3"): label lists for `father_name` and
  `application_no`; existing fields unchanged.
- `app/services/benchmark.py`: `paddleocr` accepted as an opt-in backend
  label; provenance now records per-field `parser_missing` /
  `labels_missing`, both engine probes, and the reference hash +
  verification status (never the reference file name); unavailable
  engines are recorded per case as `failed: True` /
  `failure_kind: "unavailable"` (never as successes).
- `app/services/reference_verify.py` + `docs/reference_verification.md`:
  the reference is treated as unverified until an operator records
  confirmation (hash-bound sidecar; no values stored).
- `app/services/ocr_paddle.py`: optional PaddleOCR adapter, imported
  lazily, disabled by default.
- Tests: 160 → 191 passing (new synthetic parser, verifier and PaddleOCR
  adapter tests; no real PII).

## Verified measurements (single local sample)

Sample: the local LL document previously benchmarked,
`case-0d5b2112-*` (SHA-256 prefix `0d5b2112`), reference hash
`886c7fac11a0`, **status: unverified** (no operator confirmation on
record — every number below is conditional on the transcription).
Reports: `data/benchmark/benchmark_20261015T152238_*.json` (Tesseract)
and `..._152253_*.json` (PaddleOCR path), both gitignored.

### Tesseract variants, same document and reference

| variant       | exact (of 6) | fields exact                                   | predicted | false positives | latency |
| ------------- | ------------ | ---------------------------------------------- | --------- | --------------- | ------- |
| baseline      | 1            | application_no                                 | 2         | address         | 2.07 s  |
| opencv-gray   | 1            | application_no                                 | 2         | address         | 1.98 s  |
| opencv-thresh | 4            | full_name, father_name, dob, application_no    | 6         | licence_no, address | 2.31 s |

Latency is one whole-case run (preprocessing + OCR + parsing); a single
sample says nothing about latency distributions.

For comparison, the pre-change history report on the same sample had:
baseline 0/6, opencv-gray 0/6, opencv-thresh 1/6 (dob only), with
licence_no and address predicted-but-wrong. The improvement here comes
from the new labels/parsers, not from scoring changes — but it is **one
sample** and must not be presented as a general accuracy rate.

### Regex-level (parser) evaluation on synthetic documents

Synthetic cases cover: "Father's Name" / "Name of the Father" label
forms, the observed `AppiNo . 3542873326` merge, digit-bound edges
(7/8/20/21 digits), label lookbehind head joins, licence
trailing-slash/standalone `No ...` lines, name-run trimming and address
PIN-block stopping. All are in `tests/test_fields_phase3.py` and pass.

### OCR-stage vs parser-stage attribution (redacted diagnostics)

- baseline / opencv-gray: `label_present` is false for `full_name`,
  `father_name`, `dob`, `licence_no` — the top field block is not
  recognized at all, so no parser can recover it. Misses are OCR-stage.
- opencv-thresh: all six labels are present. The four exact fields are
  parser wins on recognized text. `licence_no` is predicted but not
  exact; earlier masked inspection indicated a digit misread (T→7).
  A parser must not "guess" the intended digit, so it stays a false
  positive. `address` is predicted with extra/incorrect content
  (trailing artifacts), which tidying reduced but did not fully clean on
  this sample.
- Redacted reports cannot prove which characters differ; the T→7 note
  comes from the earlier masked audit, not from report content.

## PaddleOCR evaluation (assessment before install; not installed)

Verified from PyPI metadata (2026-10-15) and the local environment:

- `paddleocr` 3.7.0 is a 143 KiB pure-Python package, but it depends on
  `paddlex`, which requires the `paddlepaddle` engine.
- `paddlepaddle` 3.3.1 ships Windows wheels only for CPython 3.9–3.13;
  this host runs 3.14.7 → **no compatible wheel** (`pip index versions
  paddlepaddle` fails). The engine wheel is ~100 MiB.
- `paddlex` pins `numpy<2.4` while this environment has numpy 2.5.4, so
  installing it here would downgrade/conflict — another reason to use a
  separate venv.
- First real run downloads PP-OCR model files unless a local model
  directory is configured; plan for an explicit offline procedure.

What was executed: `--backend paddleocr` ran all three variants with the
honest unavailable path — 3/3 cases `failed: True`,
`failure_kind: "unavailable"`, `backend_failure_rate: 1.0`, provenance
showing `paddleocr.available: false`. A skipped engine is never counted
as a success, and no PaddleOCR accuracy is claimed anywhere.

The adapter itself is unit-tested against fake 2.x/3.x engines
(`tests/test_ocr_paddle.py`): both result shapes, the 3.x→2.x
constructor fallback, engine caching, and the unavailable path.

### To run PaddleOCR later (optional, separate venv)

1. Install a Python 3.12/3.13 environment (conda or `py -3.12 -m venv`).
2. `pip install paddlepaddle paddleocr` (expect ~100 MiB engine download).
3. Run the same harness in that venv, pointing at the same reference:
   `python -m app.services.benchmark --input <doc> --reference <ref> --backend paddleocr --variants baseline,opencv-gray,opencv-thresh`.
4. First run downloads models; afterwards set the documented model dir
   for offline use. Never upload documents.

## Limitations

- One sample, unverified reference: no generalization, no accuracy claim.
- `father_name` / `application_no` are extracted and benchmarked but not
  displayed in the review UI (`review.py` has a fixed field list, out of
  scope here).
- Label lists and heuristics follow this document family's printed
  format; other layouts need their own evidence-based labels.
- The address parser still produces imprecise candidates on noisy
  images (recorded as false positives, not hidden).
- No candidate-level precision metric exists; precision is case-level
  (`precision_cases_clean`).
- PaddleOCR accuracy on this document is unknown; only its
  unavailability is verified on this host.

## Next steps

1. Have an operator verify the reference
   (`docs/reference_verification.md`) so benchmark numbers become
   authoritative; re-run and compare.
2. Add more synthetic pages (and, if available, more operator-redacted
   documents) to separate parser from OCR effects per field.
3. For `licence_no` on noisy images, only consider shape-aware
   post-correction with documented evidence (e.g. accepted licence
   layouts), never blind fuzzy replacement of digits.
4. Optionally run the PaddleOCR challenger in a 3.12/3.13 venv and score
   it with the identical rules; keep it opt-in.
