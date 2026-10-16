# Phase 6: Validated Fill-Packet API and Copy-Friendly Field List

## 1. Overview and Architecture

Phase 6 implements a minimal, authenticated local API that delivers a validated, service-specific field packet from a currently reviewed case, accompanied by a Flask "Ready to fill" screen with per-field Copy buttons.

This API acts as the data interface that a future Chrome Manifest V3 extension (Phase 7) will consume. No Chrome extension, browser content scripts, DOM manipulation, or automated form filling are implemented in Phase 6.

### Architectural Principles

1. **Local-First & Offline**: Flask binds strictly to `127.0.0.1`. No external cloud services, remote telemetry, or OAuth providers.
2. **Strict Review Enforcement**: Fill packets are only emitted for cases that are currently in `status == 'reviewed'` with valid review timestamps and review versions.
3. **Operator-Confirmed Values Only**: The packet contains only operator-confirmed values approved during the review step. Raw OCR candidates, document paths, confidence scores, and bounding boxes are strictly excluded.
4. **Conflict Freshness Guarantees**: Any unresolved conflicts or newly arrived contradictory evidence blocks packet issuance (`409 Conflict`).
5. **No Blind Trust of Localhost**: API routes are protected by a cryptographically strong Bearer token stored outside git, validated using constant-time comparison, with cross-origin restrictions.

---

## 2. Fill-Packet Service and Data Contract

### Module: `app/services/fill_packet.py`

The service `build_fill_packet(conn, case_id, workflow="dl_application")` constructs the packet.

### Workflow Configuration & Allowlists

Workflows define which semantic fields are eligible for a given service desk action.
The initial supported workflow is:
- **`dl_application`** (Driving Licence Application):
  - Fields: `full_name`, `father_name`, `dob`, `address`, `licence_no`, `application_no`

*Limitation*: Phase 6 defines only semantic keys and eligibility rules. Specific Sarathi portal form field IDs, DOM selectors, or state-specific portal variants are intentionally deferred to Phase 7.

### Field Eligibility Rules

A field entry has `eligible_to_fill: true` only when:
1. The case has `status == 'reviewed'` with valid `reviewed_at`.
2. The field belongs to the selected workflow allowlist.
3. An operator-confirmed value is present (for `application_no`, this is `cases.appl_number`; for other fields, this is the operator-approved value).
4. No unresolved conflicts or fresh contradictory candidates block the field.

Missing optional fields appear with `eligible_to_fill: false` and `reason: "Value not available"`. Values are **never fabricated**.

### Sanitized Fill-Packet Response Example

```json
{
  "case_id": "CASE-20261016-001",
  "workflow": "dl_application",
  "workflow_label": "Driving Licence Application",
  "review_version": 1,
  "reviewed_at": "2026-10-16T11:30:00Z",
  "fields": [
    {
      "key": "full_name",
      "label": "Full name",
      "value": "Ravi Kumar",
      "eligible_to_fill": true
    },
    {
      "key": "father_name",
      "label": "Father's / related person's name",
      "value": null,
      "eligible_to_fill": false,
      "reason": "Value not available"
    },
    {
      "key": "dob",
      "label": "Date of birth",
      "value": "1995-08-17",
      "eligible_to_fill": true
    },
    {
      "key": "address",
      "label": "Address",
      "value": "123 Main St, Bengaluru 560001",
      "eligible_to_fill": true
    },
    {
      "key": "licence_no",
      "label": "Driving licence number",
      "value": null,
      "eligible_to_fill": false,
      "reason": "Value not available"
    },
    {
      "key": "application_no",
      "label": "Application number",
      "value": "LD-2026/000123",
      "eligible_to_fill": true
    }
  ]
}
```

---

## 3. Local API Authentication & Security Design

### Module: `app/services/api_auth.py` and `app/routes/api.py`

1. **Token Generation**: On initial startup, `api_auth.ensure_token()` generates a 256-bit secure token using `secrets.token_hex(32)`.
2. **Storage Location**: Stored in `data/.api_token` (file permission `0o600` on POSIX). The `data/` directory is gitignored in `.gitignore`.
3. **No Credential Leakage**:
   - The token is never returned in HTTP response bodies, error messages, or logs.
   - The token is never included in URLs or query strings.
   - The token is never injected into Jinja template context.
4. **Header Authentication**: Requests must provide `Authorization: Bearer <token>`.
5. **Constant-Time Comparison**: Tokens are compared using `hmac.compare_digest()` to prevent timing side-channels.
6. **Origin Restriction**:
   - Cross-origin requests from non-localhost origins (e.g. untrusted websites attempting to pivot into local APIs) are rejected (`403 Forbidden`).
   - Standard browser same-origin and local tools (no Origin header) are permitted.
   - Phase 7's extension will register its explicit Chrome extension origin ID.
7. **Content-Type & Request Size**:
   - `POST` endpoints enforce `Content-Type: application/json`.
   - Request payloads are limited to 4 KB to prevent denial-of-service / memory abuse.

### Extension Pairing Mechanism for Phase 7

When the future Chrome extension is deployed:
1. The operator reads the local token from `data/.api_token` (or via an operator setup script).
2. The extension popup/options page stores this token in `chrome.storage.local`.
3. The extension includes `Authorization: Bearer <token>` in its background fetch calls.

---

## 4. API Endpoints

### 1. `GET /api/cases`
Returns a minimal list of selectable cases. Excludes OCR text, customer addresses, and applicant field values.

**Response (200 OK)**:
```json
{
  "cases": [
    {
      "case_id": "CASE-20261016-001",
      "appl_number": "LD-2026/000123",
      "status": "reviewed",
      "review_version": 1,
      "reviewed_at": "2026-10-16T11:30:00Z"
    }
  ]
}
```

### 2. `GET /api/cases/<case_id>/fill-packet?workflow=dl_application`
Returns the validated fill packet for the case.

**Error Status Codes**:
- `401 Unauthorized`: Missing or invalid Bearer token.
- `403 Forbidden`: Disallowed Origin header.
- `404 Not Found`: Case does not exist.
- `400 Bad Request`: Unknown or invalid workflow.
- `409 Conflict`: Case is not reviewed (`case_not_reviewed`), missing review metadata (`review_metadata_missing`), or has unresolved conflicts (`unresolved_conflicts`).

### 3. `POST /api/cases/<case_id>/events`
Allows recording operator/extension events (such as copy attempts or fill tracking) to `case_events`.

**Payload**:
```json
{
  "kind": "copy_field",
  "review_version": 1,
  "detail": "Operator copied full_name"
}
```

**Validation**:
- Rejects unknown event kinds (`supported: copy_field, autofill_attempt, autofill_restore`).
- Rejects stale `review_version` (prevents recording actions against outdated case data).
- Rejects non-JSON or oversize payloads (>4 KB).

---

## 5. Review Lifecycle & Freshness Protection

Phase 5.1.1 introduced `conflict_acks` to track acknowledged evidence. Phase 6 tightened the review lifecycle:

### Gap Identified & Fixed in Phase 6:
- **Prior State**: In `match.py`, when new document evidence attached to an already reviewed case without detecting a conflict (agreeing candidates), the case silently remained in `reviewed` status.
- **Phase 6 Fix**: In `app/services/match.py`, whenever candidate fields attach to a case that is currently `reviewed`, the reviewed state is immediately invalidated (`status = 'needs_attention'`, `reviewed_at = NULL`, `review_version = review_version + 1`). New evidence always requires operator re-review.
- Regression test added in `tests/test_fill_packet.py::test_new_evidence_on_reviewed_case_requires_rereview`.

---

## 6. Ready to Fill View (Copy Fallback)

### Route: `GET /cases/<case_id>/fill-ready`
- Accessible via the **Ready to Fill** button on the Case Detail page for reviewed cases.
- Displays a clean table of workflow fields with operator-confirmed values and "Ready" badges.
- Per-field **Copy** button uses the modern asynchronous Clipboard API (`navigator.clipboard.writeText`), with an automatic hidden-textarea fallback for older browser environments.
- Missing or ineligible fields display "Unavailable" and disable the Copy action.
- Does not expose tokens, raw OCR text, or bounding box coordinates.
- Contains no applicant data in query parameters or browser storage.

---

## 7. Automated Test Suite and Verification

Full test suite passes 296 items with 0 failures:

```powershell
.venv\Scripts\python.exe -m pytest
```

### Breakdown of Test Coverage:
- `tests/test_fill_packet.py` (14 tests):
  - Valid packet generation and structure
  - Operator edits override OCR candidates
  - Missing optional fields marked ineligible
  - Separation between `application_no` and `licence_no`
  - Workflow allowlist scoping
  - Absence of internal OCR text, paths, or hashes
  - Rejection of unreviewed cases, missing cases, and invalid workflows
  - Rejection of stale review version after manual edits
  - Rejection when fresh conflicting evidence arrives
  - Invalidation when new agreeing evidence attaches
  - Verification of `review_version` and timestamp inclusion
- `tests/test_api.py` (23 tests):
  - Missing / invalid Bearer token rejection (401)
  - Valid Bearer token acceptance (200)
  - No token exposure in error responses
  - Localhost Origin accepted, external Origin rejected (403)
  - Minimal case listing on `/api/cases`
  - Fill packet endpoint status codes and payload structure
  - Event recording, validation, stale review version rejection (409)
  - Content-Type enforcement (415) and size limits
  - Token file permissions and gitignore verification
- `tests/test_fill_ready_ui.py` (5 tests):
  - Rendering of reviewed case with Copy buttons
  - Display of unreviewed case rejection error
  - Handling of missing optional fields
  - "Ready to Fill" button visibility on Case Detail only for reviewed cases
  - 404 behavior for nonexistent cases
- Regression suites (254 existing tests):
  - Phase 1–5 intake, OCR, pre-processing, matching, and review freshness all pass intact.

---

## 8. Manual Verification Checklist

When running the application locally:
1. Start the server: `run.bat` or `python run.py`.
2. Navigate to `http://127.0.0.1:5000/cases`.
3. Pick a case, complete review, and click "Validate & Save".
4. On the Case Detail page, click the new **Ready to Fill** button.
5. Click **Copy** next to "Full name" or "Address"; verify that the text copies to your clipboard.
6. Test API authentication via PowerShell:
   ```powershell
   $token = Get-Content data\.api_token
   Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/cases" -Headers @{ Authorization = "Bearer $token" }
   ```

---

## 9. Scope Restrictions and Deferred Work (Phase 7)

Deliberately deferred to Phase 7:
- Chrome extension Manifest V3 configuration (`manifest.json`).
- Extension background service worker and content scripts.
- Portal-specific DOM selectors, form input targeting, and iframe navigation.
- Automatic fill/restore actions on external websites.
- CAPTCHA, OTP, login, and official payment interactions (remain strictly manual).
