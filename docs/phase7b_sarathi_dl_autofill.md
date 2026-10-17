# Phase 7B: Sarathi DL Entry — Learner's Licence Number Autofill

## 1. Overview and Semantic Clarification

Phase 7B enables operator-assisted autofill for the **Sarathi DL Entry** step, where an applicant holding a Learner's Licence applies for a Driving Licence on `https://sarathi.parivahan.gov.in/sarathiservice/*`.

### Semantic Correction: `licence_no` is the Learner's Licence Number
- The existing FormFlow field key `licence_no` represents the **Learner's Licence (LL) number** (not driving licence number).
- For backward compatibility and architectural stability, `licence_no` is preserved as the internal database field key across `extracted_fields`, `fill_packet.py`, and API endpoints. No duplicate column or duplicate key (such as `learning_licence_no`) is introduced.
- User-facing labels in `FIELD_LABELS`, `review.html`, `fill_ready.html`, and `case_detail.html` have been updated to **Learner's Licence Number**.
- `application_no` remains strictly distinct, stored in `cases.appl_number`, and is never used as an LL number.

### Explicit Operator Confirmation for Unchanged OCR Candidates
In FormFlow's data contract:
1. Required fields (`full_name`, `dob`) are validated by the operator during "Validate & Save" on a reviewed case.
2. Optional fields (`licence_no`, `father_name`, `address`) require explicit operator confirmation (`method == 'manual'`) before they are eligible in the fill packet. Raw OCR candidates are never exposed as fill-eligible.
3. In Phase 7B, the review interface provides a direct confirmation checkbox for unconfirmed `licence_no` OCR candidates:
   ```html
   <input type="checkbox" id="f_lic_confirm" name="confirm_licence_no" value="1">
   Confirm this Learner's Licence number as verified from document
   ```
   When checked, `save_review()` inserts a `manual` row for `licence_no` with the verified value, establishing operator confirmation without forcing the operator to alter the text.

---

## 2. Portal Target Selectors & Verified Conditions

Based on `sarathidrivinglicense.html` and portal inspection, the DL Entry form is structured under `newLLApplForm`:

| Field / Control | Portal Selector | Scoping / Verification Rule |
|---|---|---|
| **Mode Radio** | `input[name="llHolder"][value="1"]` (`#llHolder1`) | Must be checked ("Holding Learners Licence" mode). |
| **Section Container** | `#lldetailsDisplay` | Must exist and be visible (`display !== 'none'`). |
| **Learner's Licence No** | `#lldetailsDisplay input#learningLicence` | Exactly 1 visible, enabled, non-readonly input inside `#lldetailsDisplay`. |
| **Date of Birth** | `#lldetailsDisplay input#DOB` | Exactly 1 visible, enabled, non-readonly input inside `#lldetailsDisplay`. (Unrelated `#DOB` inputs elsewhere in personal-details sections are ignored). |
| **CAPTCHA** | `#entcaptxt` | **NEVER targeted, filled, or modified**. Must be solved manually by operator. |
| **Form Action Buttons** | `#ok`, `#can1` | **NEVER clicked or automated**. Operator reviews filled data and clicks OK manually. |

### Date Format Conversion
- FormFlow stores validated dates in ISO format: `YYYY-MM-DD`.
- Sarathi `#DOB` requires: `DD-MM-YYYY` (e.g., `25-06-1994`).
- `portal_adapter.js` validates calendar correctness (rejecting leap-year errors, invalid days/months, or ambiguous dates) before converting to `DD-MM-YYYY`.

---

## 3. Extension Installation & Extension ID Pinning

### Loading the Unpacked Extension
1. Open Chrome and navigate to `chrome://extensions/`.
2. Enable **Developer mode** (top-right toggle).
3. Click **Load unpacked** and select `c:\FormFlow\extension`.
4. Confirm **FormFlow - Driving Licence Assistant** (v1.1.0) appears without manifest errors.
5. Copy the generated 32-character Extension ID (e.g. `abcdefghijklmnopabcdefghijklmnop`).

### Pinning the Extension ID in FormFlow (`FORMFLOW_EXTENSION_ID`)
To enforce that only your authorized extension can access the local FormFlow API:
1. In `config.local.json`:
   ```json
   {
     "extension_id": "<your-32-character-extension-id>"
   }
   ```
2. Or in your environment:
   ```bat
   set FORMFLOW_EXTENSION_ID=<your-32-character-extension-id>
   ```
When configured, `app/services/api_auth.py` rejects any other extension ID with `403 Forbidden`.

---

## 4. Manual Testing Instructions (Synthetic Data)

1. **Start FormFlow**:
   ```bat
   python run.py
   ```
2. **Review a Test Case**:
   - Ingest a synthetic document or use an existing test case in `http://127.0.0.1:5000`.
   - Open Review: verify Full Name, Date of Birth, and Learner's Licence Number.
   - Check the **Confirm this Learner's Licence number as verified from document** checkbox.
   - Click **Validate & Save**. Case status becomes `reviewed`.
3. **Open Sarathi Portal**:
   - Navigate to the Sarathi DL Entry page:
     `https://sarathi.parivahan.gov.in/sarathiservice/newDL_displayPage.do` (or local mock/staging).
   - Ensure "Holding Learners Licence" radio is selected.
4. **Trigger Autofill from Extension**:
   - Click the FormFlow extension icon.
   - Confirm status shows **DL Entry Ready**.
   - Select your reviewed case.
   - Preview box displays:
     - `Learner's Licence Number`: e.g. `TS09/0001234/2021`
     - `Date of Birth`: e.g. `25-06-1994`
   - Click **Fill LL Number and DOB**.
5. **Verify Portal Behavior**:
   - `#learningLicence` contains the exact licence number.
   - `#DOB` contains `DD-MM-YYYY`.
   - Captcha `#entcaptxt` is untouched.
   - No buttons were clicked; form was not submitted.
   - Enter CAPTCHA manually and proceed.

---

## 5. Security & Freshness Invariants

- **Freshness Binding**: The popup re-fetches the fill packet immediately before dispatching the fill command, ensuring the case is currently `reviewed` and has not been invalidated.
- **Zero Invalidation Bypass**: If a new document attaches to the case or the case is edited, `status` drops to `needs_attention`, and packet fetch fails with `409 Conflict: case_not_reviewed`.
- **Zero PII & Token Logging**: Applicant values and API Bearer tokens are never logged to `console.log` or persisted in `chrome.storage`.
- **Zero Portal Automation**: No CAPTCHA solving, OTP reading, button clicking, or declaration automation.
