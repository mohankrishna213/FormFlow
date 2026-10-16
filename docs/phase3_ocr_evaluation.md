# Phase 3 OCR evaluation (local, privacy-safe)

Date: 2026-10-15 (PaddleOCR challenger runs completed 2026-10-16) · Host: Windows, Python 3.14.7 · Tesseract
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
  lazily, disabled by default; 3.x constructor with 2.x fallback,
  `enable_mkldnn=False` (oneDNN crash workaround) and visual-line
  regrouping of per-box results so the line-based parser contract holds.
- Tests: 160 → 194 passing (new synthetic parser, verifier and PaddleOCR
  adapter tests; no real PII).

## Verified measurements (single local sample)

Sample: the local LL document previously benchmarked,
`case-0d5b2112-*` (SHA-256 prefix `0d5b2112`), reference hash
`886c7fac11a0`, **status: operator-verified** (mohan, 2026-10-15T15:54:01Z;
the sidecar binds that hash to the reviewed bytes). A verified re-run
reproduced the table below exactly (baseline 1/6, opencv-gray 1/6,
opencv-thresh 4/6), so the numbers are authoritative for this sample.
Reports: `data/benchmark/benchmark_20261015T155417_0d5b2112f93e1b94.json`
(verified Tesseract re-run), `..._152238_*.json` (pre-verification run)
and `..._152253_*.json` (PaddleOCR unlock path), all gitignored.

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

## Multi-document run (five more local documents, 2026-10-15)

Five more locally available learner-licence photos (two printed layouts,
varied photo quality) were staged under gitignored `data/staging/`, each
transcribed to a reference JSON. The operator reviewed every reference
field-by-field against its image and all five were recorded as
`operator-verified` (mohan, 2026-10-15T16:38:22Z) **before** scoring, so
these are authoritative ground truth for their documents. Template-B
documents print no application number; their references store
`application_no: null`, which is skipped in scoring (a spurious
application-number prediction would still have been flagged — none
occurred on any variant).

Reports (gitignored, redacted): `data/benchmark/benchmark_20261015T1639{04,08,20,26,33}_<sha16>.json`.

| doc (sha16)      | layout | baseline | opencv-gray | opencv-thresh | exact fields on baseline |
| ---------------- | ------ | -------- | ----------- | ------------- | ------------------------ |
| `ca0adcdd3d4f39a4` | B    | 1/6      | 1/6         | 0/6           | full_name                |
| `91c36e3c204e55e3` | A    | 0/6      | 0/6         | 1/6           | — (thresh: father_name)  |
| `8a39e189be58e357` | B    | 2/6      | 2/6         | 0/6           | dob, licence_no          |
| `40419f8357dbc408` | A    | 6/6      | 6/6         | 1/6           | all six                  |
| `65ce8c6200a4c454` | B    | 0/6      | 0/6         | 0/6           | —                        |

Per-variant latency was 1.1–4.9 s per case; a five-document sample says
nothing about latency distributions.

### Parser-stage vs OCR-stage attribution

**Parser-stage (deterministic, fixable in the rules):**

- `full_name`: template B prints a "Display Name" row, but the label list
  has no explicit "display name" entry; the generic "name" fallback
  captured the display value on one template-B document but the shorter
  "Name" row on another (one exact hit, one mismatch). An explicit
  "display name" label would make this deterministic.
- `father_name`: template B prints "Son/Wife/Daughter of"; that label is
  not in the list, so no candidate is produced on template-B documents
  even when OCR reads the row (two misses; both template-A documents
  print "Father's Name" and match when OCR is clean).
- `address`: the labeled-block reader caps at 3 following lines, so a
  5-line address comes out short (one document, candidates 43/57/60 vs.
  80 reference characters); on another it over-captured beyond the
  address block (102 vs. 77); on the clean template-A document the first
  candidate matched and a second candidate was an extra block (recorded
  as a false positive even though the field matched).
- `licence_no`: on the low-yield template-A document the standalone-"No …"
  fallback produced one candidate whose length equals the printed
  application-number length, while the application-number field got no
  candidate — consistent with the heuristic claiming the app-number line
  on noisy text.

**OCR-stage (photo/lighting; no parser change can recover these):**

- Low-yield photo (template A, ~1.1–1.3k recognized characters vs.
  ~2.2–2.6k elsewhere): baseline/opencv-gray never see the field label
  strings; opencv-thresh recovers just enough for 1 exact field.
- Dark photo (template B): ~2.2k characters recognized but nothing
  label-shaped — zero candidates in all three variants. Best candidate
  for the optional PaddleOCR challenger (below) or a
  brightness-normalizing variant.
- Overlapped sheet (template B): the "Date of Birth" and "Licence Number"
  label lead-ins are physically hidden on the photo, so `dob` and
  `licence_no` get no candidates while `full_name` still matched.
- opencv-thresh degrades already-crisp photos (one document drops 6/6 →
  1/6) while sometimes helping dim ones — variant choice must stay
  per-document, never a global switch.

## PaddleOCR challenger (measured, stock pipeline)

The optional challenger was executed for real in a separate Python 3.11
venv — `paddleocr` 3.7.0 / `paddlepaddle` 3.3.1 / `paddlex` 3.7.2 /
Python 3.11.3 (no `paddlepaddle` wheel exists for this host's 3.14, and
`paddlex` pins `numpy<2.4`; hence the separate venv). The stock default
pipeline was kept: PP-LCNet_x1_0_doc_ori orientation + UVDoc unwarp +
PP-OCRv6_medium det/rec. All six documents × three variants were scored
with the identical rules and references; engine versions are recorded
in every report.

Setup (reproducible; the first run needs network only for the model
files):

    py -3.11 -m venv <venv>
    <venv>\Scripts\python -m pip install paddlepaddle==3.3.1 paddleocr==3.7.0 pillow pytesseract flask
    <venv>\Scripts\python -m app.services.benchmark --input <doc> ^
        --reference <ref> --backend paddleocr --variants baseline,opencv-gray,opencv-thresh

(`pytesseract`/`flask` because the harness probes both engines and
`app/__init__.py` imports Flask.) The first run downloads the models
into the per-user `paddlex` cache; afterwards runs are offline. Nothing
is uploaded anywhere.

### Adapter fixes the real engine required

- `enable_mkldnn=False`: with the oneDNN path enabled, paddlepaddle
  3.3.1 aborts at model load (`NotImplementedError …
  pir::ArrayAttribute<pir::DoubleAttribute>`); the adapter disables it
  in both constructor branches.
- **Visual-line regrouping**: the 3.x pipeline returns per-text-box
  results and one printed row often arrives as several boxes (label and
  value separate). The adapter regroups boxes into visual lines
  (vertical overlap, then horizontal order) so `fields.py`'s line-based
  label contract sees the row intact; without it labeled extraction
  fails wholesale. Only geometry is grouped — the recognized text is
  not rewritten.
- Still covered by `tests/test_ocr_paddle.py` (fake 2.x/3.x engines:
  result shapes, 3.x→2.x constructor fallback, engine caching, the
  unavailable path, the mkldnn flag and line regrouping).

### Results (same six documents and references)

| doc (sha16)        | layout | Tesseract baseline | Paddle baseline | Paddle gray | Paddle thresh | Paddle baseline exact fields                     |
| ------------------ | ------ | ------------------ | --------------- | ----------- | ------------- | ------------------------------------------------ |
| `ca0adcdd3d4f39a4` | B      | 1/6                | 0/6             | 0/6         | 0/6           | —                                                |
| `91c36e3c204e55e3` | A      | 0/6                | 4/6             | 5/6         | 1/6           | full_name, father_name, dob, application_no      |
| `8a39e189be58e357` | B      | 2/6                | 2/6             | 2/6         | 2/6           | dob, licence_no                                  |
| `40419f8357dbc408` | A      | 6/6                | 5/6             | 5/6         | 5/6           | full_name, father_name, dob, licence_no, address |
| `65ce8c6200a4c454` | B      | 0/6                | 2/6             | 2/6         | 0/6           | dob, licence_no                                  |
| `0d5b2112f93e1b94` | A      | 1/6                | 4/6             | 4/6         | 4/6           | full_name, father_name, dob, licence_no          |

Latency (one whole case = preprocessing + OCR + parsing on this host):

- PaddleOCR: baseline 123.0–350.5 s, gray 142.6–1038.6 s (the 1038.6 s
  case is an outlier on the clean document), thresh 84.7–389.4 s.
  Tesseract on the same host: 1.1–4.9 s. So the challenger is roughly
  **60–300× slower** (~2–6 min per case); it stays opt-in and disabled
  by default.

Across the six documents (same references; a convenience sample, not a
generalization): Tesseract 10/10/6 exact fields on
baseline/gray/thresh vs PaddleOCR 17/18/12 of 33 expected.

### Findings

- PaddleOCR wins exactly where Tesseract was blind: the low-yield
  template-A photo 0/6 → 4/6 (gray 5/6), the dark template-B photo
  0/6 → 2/6, the sample 1/6 → 4/6; on the clean template-A document it
  stays within one field (5/6 vs 6/6).
- Regression: on the clean document `application_no` is exact under
  Tesseract but gets zero PaddleOCR candidates (its merged `AppI No`
  label row is not detected — OCR-stage for this engine); on
  `ca0adcdd` nothing label-shaped is recovered at all (only a 143-char
  address candidate) where Tesseract still matched `full_name`.
- Rule-layer limits repeat across engines — they are parser-stage, not
  OCR-stage: `address` yields a wrong or over-long candidate on every
  document (`ca0adcdd` 143 vs 80 chars; `8a39e189` [30,54,55] vs 77;
  `65ce8c62` [10,54,91] vs 77; sample [62,111] vs 50); the template-B
  "Display Name" / "Son/Wife/Daughter of" rows never produce exact
  name/father values; `dob` day/month ambiguity persists (low
  confidence on two documents). The four suggested rule follow-ups in
  Next steps remain the candidate fixes — not applied here.
- Variant fragility repeats: gray adds `licence_no` on the low-yield
  photo (4/6 → 5/6), but thresh collapses to ≤2 fields on four of six
  documents; no global variant switch is safe.
- No accuracy claim: one convenience sample per quality class.

Reports (gitignored, redacted; all `reference: operator-verified`):
`data/benchmark/benchmark_20261015T181250_40419f8357dbc408.json`,
`…T182215_65ce8c6200a4c454.json`, `…T183124_91c36e3c204e55e3.json`,
`…T184938_8a39e189be58e357.json`, `…T190324_ca0adcdd3d4f39a4.json`,
`…T191005_0d5b2112f93e1b94.json`.

## Limitations

- Convenience sample (six local documents, two layouts, all references
  operator-verified): usable for per-field parser/OCR attribution only;
  no generalization, no accuracy claim.
- `father_name` / `application_no` are extracted and benchmarked but not
  displayed in the review UI (`review.py` has a fixed field list, out of
  scope here).
- Label lists and heuristics follow this document family's printed
  format; other layouts need their own evidence-based labels.
- The address parser still produces imprecise candidates on noisy
  images (recorded as false positives, not hidden).
- No candidate-level precision metric exists; precision is case-level
  (`precision_cases_clean`).
- PaddleOCR is now measured on the same six documents (stock pipeline,
  separate venv — see the challenger section); it stays opt-in and
  disabled by default, and six documents still support no accuracy
  claim.

## Next steps

1. Operator verification — done: all six references are
   operator-verified and the verified re-runs reproduced the recorded
   numbers.
2. Suggested rule follow-ups from the multi-document evidence — **not
   applied** (report-only decision for now; re-run all six documents if
   any is adopted — rule changes can move numbers in both directions):
   - add an explicit "display name" label for `full_name` (template B);
   - add "Son/Wife/Daughter of" family labels for `father_name`
     (template B);
   - widen/guard the address labeled block (5-line addresses; stop at
     the "Permanent Address" heading or next label; avoid the extra
     block candidate);
   - keep the standalone-"No …" `licence_no` fallback from claiming
     application-number lines on noisy text.

   Status update (2026-10-16): all four follow-ups were implemented
   deterministically and re-measured end-to-end on the same six
   documents (both engines) in **Phase 4A** — see
   [phase4a_extraction_fixes.md](phase4a_extraction_fixes.md). The
   single-sample and multi-document tables above remain the historical
   Phase 3 record and were not modified.
3. For `licence_no` on noisy images, only consider shape-aware
   post-correction with documented evidence (e.g. accepted licence
   layouts), never blind fuzzy replacement of digits.
4. PaddleOCR challenger — done: run on all six documents in a separate
   Python 3.11 venv and scored with the identical rules (see the
   challenger section); keep it opt-in and disabled by default.
