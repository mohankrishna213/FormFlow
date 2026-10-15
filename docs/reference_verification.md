# Benchmark reference verification (Phase 3)

The benchmark harness scores extracted fields against a local reference
JSON (for example `data/benchmark/ref_ll.json`). That reference may have
been transcribed by an agent from a document image, so **it must not be
treated as ground truth until a human confirms it**. This document
describes the verification tooling and the status contract that
benchmark reports record.

Tool: `app/services/reference_verify.py` (CLI below). It never prints
applicant values, never writes values to disk, and never uploads
anything.

## Status model

Every reference file can be in one of these states, as reported by the
tool and recorded in benchmark provenance:

| status              | meaning                                                        |
| ------------------- | -------------------------------------------------------------- |
| `unverified`        | no sidecar exists (the default; assumed for every new file)     |
| `operator-verified` | a sidecar matches the current file hash and records an operator |
| `stale`             | a sidecar exists but the reference changed after verification   |
| `reference-missing` | reported by the benchmark when the reference path is not a file |

A verification is bound to the exact reference bytes: the sidecar stores
the reference SHA-256, so any edit invalidates it (`stale`). Skipped or
unreadable sidecars degrade to `unverified`; a missing verification is
never silently upgraded.

## What the tool checks (structure) and what it cannot

`verify_reference` reports, per field: presence, normalized length,
value *shape* (for example `iso-date`, `name-words:2`, `digit-chars:10`)
and a `structure_ok` flag driven by the same parsers the extraction
pipeline uses. It also reports schema issues (missing/unknown keys,
wrong value types) and duplicate normalized values across fields.

It **cannot** confirm that a value is correct against the document.
Only the operator can do that, locally, by comparing each value with the
original. Structural checks merely catch transcription mistakes that
break the expected format.

## CLI usage

```pwsh
# Redacted structural report (no values; exit 2 when unusable)
python -m app.services.reference_verify --reference data\benchmark\ref_ll.json

# Current verification status only
python -m app.services.reference_verify --reference data\benchmark\ref_ll.json --status

# Local inspection of value hashes (opt-in; sensitive, never share)
python -m app.services.reference_verify --reference data\benchmark\ref_ll.json --show-hashes

# After YOU compared every value against the original document:
python -m app.services.reference_verify --reference data\benchmark\ref_ll.json --confirm-verified --operator YOUR-NAME
```

`--confirm-verified` requires `--operator NAME` and records the sidecar
`<reference>.verification.json` next to the reference (same gitignored
directory). The sidecar contains the operator name, UTC timestamp, the
reference SHA-256 and per-field *shapes* — never values.

Writes inside the repository are only allowed under the gitignored
`data/` tree; anywhere else inside the repo is refused. Paths outside
the repository (for example a scratch dir) are allowed.

## How benchmark reports use it

- `collect_provenance(..., reference_path=...)` and
  `compare_variants(..., reference_path=...)` record a `reference` /
  `reference_info` block: `sha256_12` plus the verification status.
  The **reference file name is deliberately omitted** (it could identify
  the applicant).
- The CLI (`python -m app.services.benchmark ...`) prints a warning to
  stderr whenever the reference is not `operator-verified`, and every
  report is stored with that status attached.
- Metrics remain "conditional on the transcribed reference" until a
  sidecar says otherwise.

## Privacy rules (unchanged from the harness)

- Normal reports contain no values, no raw text and no filenames.
- The verifier's default output contains no values either; hashes are
  opt-in and still sensitive for short fields.
- Reference files and sidecars live outside Git (`data/` is ignored) and
  must never be copied into tests, docs or commits.
