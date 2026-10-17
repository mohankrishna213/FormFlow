# Phase 7A: Chrome Extension Shell, API Client, and Workflow-Aware Case Picker

## 1. Overview and Architecture

Phase 7A introduces the **FormFlow Companion** Chrome Manifest V3 extension (`extension/`), connecting a service desk operator's browser securely to the local FormFlow service (`http://127.0.0.1:5000`).

The extension allows the operator to:
1. Connect securely to FormFlow using the local Bearer token generated at `data/.api_token`.
2. List available cases (`GET /api/cases`) with case ID, status, and reviewed state.
3. Select a specific case and inspect case metadata (version, reviewed timestamp).
4. Select a workflow from 5 operational Sarathi service desk flows:
   - **DL Entry** (`dl_application`): Currently implemented and supported in backend.
   - **LL Application** (`ll_application`): Planned for Phase 7B.
   - **Application Status** (`app_status`): Planned for Phase 7B.
   - **Print Form** (`print_form`): Planned for Phase 7B.
   - **Payment Status** (`payment_status`): Planned for Phase 7B.
5. Preview operator-confirmed field values from the fill-packet API (`GET /api/cases/<case_id>/fill-packet?workflow=dl_application`). Fields marked with `eligible_to_fill: false` are clearly displayed as unavailable with the authoritative backend reason.

### Strict Scope Boundaries (What Phase 7A Deferrs)

To ensure safety and respect portal security, **Phase 7A contains zero portal DOM manipulation**:
- **No Content Scripts**: The extension has no content scripts declared in `manifest.json`.
- **No Host Permissions for External Sites**: Permissions are strictly limited to local loopback (`http://127.0.0.1:5000/*` and `http://localhost:5000/*`). No `<all_urls>` or `*.gov.in` permissions.
- **No Automated Form Filling, Clickers, or Restores**: The extension only previews confirmed data in its popup.
- **No CAPTCHA, OTP, Login, or Payment Automation**: Sarathi portal verification, OTP challenges, and payment flows remain completely in operator hands.

---

## 2. Chrome Extension Installation (Unpacked)

1. Open Google Chrome (or Chromium-based browser: Edge, Brave).
2. Navigate to `chrome://extensions/`.
3. Enable **Developer mode** toggle in the top-right corner.
4. Click **Load unpacked** in the top-left toolbar.
5. Select the `extension/` directory inside your FormFlow repository (`<path-to-repo>\extension`).
6. Confirm that **FormFlow - Driving Licence Assistant** appears in the extensions list with version `1.0.0` and no manifest errors.
7. Note the generated **Extension ID** (a 32-character lowercase string such as `abcdefghijklmnopabcdefghijklmnop`).

---

## 3. Configuring Local Bearer Token & API URL

### Where the Token Lives
FormFlow generates a cryptographically strong 256-bit token stored in `data/.api_token` on first startup. This file is gitignored and permissions-restricted.

### Configuring the Extension
1. Click the FormFlow icon in the Chrome toolbar to open the extension popup.
2. If no token is configured, the popup automatically opens the **Connection Settings** drawer.
3. Review the **API Base URL** (defaults to `http://127.0.0.1:5000`).
4. Open `<repo>/data/.api_token` in a text editor, copy the 64-character hex token string, and paste it into the **API Bearer Token** field.
5. Click **Save Settings**.
6. The extension securely persists these settings to `chrome.storage.local` within the extension's isolated storage sandbox.
7. Click **Test Connection** to verify immediate connectivity against the local FormFlow service.

### Security Invariants
- **Never placed in URLs**: The token is never passed as a query parameter.
- **Never committed**: The token is stored only in the operator's local `chrome.storage.local` and local `data/.api_token`.
- **Never logged**: The extension's `FormFlowApiClient` explicitly filters out authorization headers and token contents from `console.log`, `console.warn`, and `console.error`.
- **Masked in UI**: The token input field is rendered with `type="password"`.

---

## 4. Origin Policy & CORS Configuration

### Chromium Manifest V3 Extension Origins
When an extension makes an `xmlhttprequest` or `fetch()` from its extension pages (`popup.html`), Chromium automatically attaches an origin header in the format:
```http
Origin: chrome-extension://<32-character-extension-id>
```
Because requests include the `Authorization: Bearer <token>` header, the browser initiates a CORS preflight (`OPTIONS` request) before sending the `GET` or `POST` request.

### Backend Origin Hardening in `app/services/api_auth.py`
FormFlow enforces strict origin validation:
1. Local loopback origins (`http://127.0.0.1:5000`, `http://localhost:5000`) are accepted.
2. Extension origins matching `^chrome-extension://[a-z0-9]{32}$` are accepted.
3. Any external web origin (e.g. `http://evil.com`), null origin (`Origin: null`), or malformed extension origin is rejected with `403 Forbidden`.
4. Wildcard CORS (`Access-Control-Allow-Origin: *`) is **never used**.

### Optional Extension ID Pinning (`FORMFLOW_EXTENSION_ID`)
For enhanced security in shared environments, the administrator can pin the exact Chrome Extension ID in FormFlow:
- In `config.local.json`:
  ```json
  {
    "extension_id": "<your-32-char-extension-id>"
  }
  ```
- Or via environment variable:
  ```bat
  set FORMFLOW_EXTENSION_ID=<your-32-char-extension-id>
  ```
When set, FormFlow will reject requests from any other extension ID, even if running locally.

---

## 5. Testing Against Synthetic FormFlow Cases

### Step-by-Step Manual Verification in Chrome

1. **Start FormFlow**:
   ```bat
   python run.py
   ```
2. **Ensure at least one reviewed case exists**:
   - Ingest synthetic applicant documents into `Inbox/`.
   - Go to `http://127.0.0.1:5000`, open the case, verify extracted values, and click **Validate & Save** so the case achieves `status == 'reviewed'`.
3. **Open Extension Popup**:
   - Click the FormFlow extension icon.
   - Status badge should show `Connected`.
4. **Select Case**:
   - Select the reviewed case from the case dropdown.
   - Case metadata banner displays case ID, status (`reviewed`), and review version.
5. **Select Workflow**:
   - Default is **DL Entry** (`dl_application`).
   - The preview table displays all operator-confirmed fields:
     - `Full Name`: Confirmed value with green `Eligible` badge.
     - `Date of Birth`: Confirmed value with green `Eligible` badge.
     - Any unconfirmed or missing optional fields display an `Unavailable` badge with the authoritative reason.
6. **Test Planned Workflows**:
   - Select **Application Status**, **LL Application**, **Print Form**, or **Payment Status**.
   - Notice the UI immediately switches to an informative alert:
     > **Workflow Planned for Phase 7B**: *<Workflow Label>* is scheduled for Phase 7B. Backend fill packets are currently implemented for DL Entry (dl_application).
   - No request is dispatched to the backend, and no fake or synthetic packet is fabricated.
7. **Test Error Scenarios**:
   - **Invalid Token**: Temporarily alter a character in the settings drawer and click Save. The UI shows `401 Unauthorized: Invalid or expired API token`.
   - **Stopped Service**: Stop the Flask server (`Ctrl+C`). Refresh cases. The UI shows `Cannot connect to FormFlow at http://127.0.0.1:5000. Ensure python run.py is running.`
   - **Unreviewed Case**: Select a case that has not yet completed review. The UI reports `409 Conflict: Case has not completed operator review`.

---

## 6. Automated Test Suites

### 1. Pytest Python Suite (Backend + Manifest Integrity)
```bat
.venv\Scripts\python.exe -m pytest -v
```
Covers:
- `tests/test_extension_manifest.py`: Manifest V3 compliance, minimal permissions (`storage` only), file path integrity, absence of content scripts, and zero hardcoded secrets.
- `tests/test_api_extension_origin.py`: Origin header parsing, CORS preflight (`OPTIONS`), rejection of malicious origins, rejection of malformed IDs, and `FORMFLOW_EXTENSION_ID` pinning.
- All 329 regression tests across FormFlow services.

### 2. Node.js Extension API Client Test Suite
```bat
node tests/test_extension_api_client.js
```
Covers:
- Successful case listing.
- Empty case list handling.
- Missing and invalid token responses (401).
- Server offline / timeout handling.
- Malformed JSON responses.
- Unreviewed case (409) and stale review version handling.
- Field packet eligibility preservation (no raw OCR fallback).
- Planned workflow segregation.
- Console privacy interceptor (verifying zero token/PII leakage).
