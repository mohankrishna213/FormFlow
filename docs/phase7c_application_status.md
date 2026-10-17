# Phase 7C: Sarathi Application Status — Operator-Assisted Autofill

## 1. Overview and Confirmed Data Contract

Phase 7C introduces operator-assisted autofill for the **Sarathi Application Status** workflow (`https://sarathi.parivahan.gov.in/sarathiservice/applViewStatus.do`), enabling operators to check application progress without manually retyping the official Application Number and Date of Birth.

### Semantic Data Contract
- **`application_no`**: Official operator-confirmed application number stored in `cases.appl_number`.
  - Raw OCR candidates in `extracted_fields` are strictly **never** exposed as fill-eligible without operator confirmation during review or case edit.
  - If official application number is not present, the field is marked unavailable (`eligible_to_fill: false`, reason: `"Value not available"`).
- **`dob`**: Operator-validated Date of Birth (ISO `YYYY-MM-DD` internally; converted to `DD-MM-YYYY` for the portal).
- **Isolation from DL Entry**:
  - `licence_no` (Learner's Licence number) is never substituted for `application_no`.
  - DL Entry and Application Status are completely isolated workflows in both the API backend and extension portal adapter.

### Backend Workflow Allowlist (`app/services/fill_packet.py`)
```python
WORKFLOWS: dict[str, dict[str, Any]] = {
    "auto": {
        "label": "Auto-Detect / Flexible",
        "fields": ("full_name", "father_name", "dob", "address",
                   "licence_no", "application_no"),
    },
    "dl_application": {
        "label": "Driving Licence Application",
        "fields": ("full_name", "father_name", "dob", "address",
                   "licence_no", "application_no"),
    },
    "app_status": {
        "label": "Application Status",
        "fields": ("application_no", "dob"),
    },
}
```

---

## 2. Portal Target Selectors & Live Inspection Results

The Application Status page was inspected directly on `https://sarathi.parivahan.gov.in/sarathiservice/applViewStatus.do`:

| Field / Control | Portal Selector | Scoping / Verification Rule | Verified on Live Portal |
|---|---|---|---|
| **Portal Host & Path** | `sarathi.parivahan.gov.in/sarathiservice/applViewStatus.do` | Must match official portal and `/sarathiservice/` path prefix. | **Yes** (live inspection) |
| **Enclosing Form** | `form#applViewStages` | Top-level form (name: `applViewStatus`, action: `/sarathiservice/applViewStages.do`). | **Yes** (live inspection) |
| **Section Container** | `#publicApplStatus` | Visible container inside `fieldset.scheduler-border`. | **Yes** (live inspection) |
| **Application Number** | `#publicApplStatus input#applNum` | Unique, visible, enabled, non-readonly text input (maxlength 16, placeholder `Application No`). | **Yes** (live inspection) |
| **Date of Birth** | `#publicApplStatus input#dateOfBirth` | Unique, visible, enabled, non-readonly text input (maxlength 10, placeholder `DD-MM-YYYY`, title `Enter Date of Birth in dd-MM-yyyy Format`). | **Yes** (live inspection) |
| **CAPTCHA Input** | `#entcaptxt` | **NEVER targeted, filled, or modified**. Must be solved manually by operator. | **Yes** (live inspection) |
| **Action Buttons** | `#submit`, `#cancel` | **NEVER clicked or automated**. Operator reviews filled fields, solves CAPTCHA, and clicks Submit manually. | **Yes** (live inspection) |

### Date Format Conversion
- FormFlow stores validated dates in ISO format: `YYYY-MM-DD`.
- Sarathi `#dateOfBirth` expects: `DD-MM-YYYY` (e.g., `25-06-1994`).
- `portal_adapter.js` verifies calendar correctness (rejecting impossible leap days, out-of-range months/days) before formatting to `DD-MM-YYYY`.

---

## 3. Human-Controlled Actions & Security Invariants

- **Zero CAPTCHA Automation**: CAPTCHA input (`#entcaptxt`), CAPTCHA image (`#capimg`), and reload controls are never touched or modified.
- **Zero Button Clicking / Submission**: Submit (`#submit`) and Cancel (`#cancel`) buttons are strictly excluded from event dispatch or clicking.
- **Strict Section Scoping**: Targets are queried inside `#publicApplStatus`. Any unrelated inputs outside the section are ignored.
- **Traceability Verification**: Command metadata requires a non-empty `caseId` and positive integer `reviewVersion`.
- **Zero Logging / Persistence of PII**: Neither the local API token nor applicant details are printed to `console.log` or saved in persistent extension storage.
- **Narrow Permissions**: Manifest V3 host permissions remain limited to `https://sarathi.parivahan.gov.in/sarathiservice/*` and `http://127.0.0.1:5000/*`.

---

## 4. Automated Test Results

### 1. Python Test Suite (Backend & API)
Command:
```bat
.venv\Scripts\pytest -v
```
Result:
- **342 passed** (0 failures).
- Covers `app_status` allowlist, missing/unconfirmed `application_no`, OCR-only candidate unavailability, unreviewed/stale case rejection, review confirmation regression tests, `auto` workflow allowlist, and `GET /api/cases/<case_id>/fill-packet?workflow=auto`.

### 2. Node.js Extension Test Suite
Commands:
```bat
node tests/test_portal_adapter.js
node tests/test_extension_api_client.js
```
Results:
- `test_portal_adapter.js`: **33 passed, 0 failed**.
  - Includes location validation, section scoping, element readiness, disabled/duplicate input rejection, event dispatch and read-back, CAPTCHA/button safety exclusions, command shape validation, DL Entry / Application Status cross-workflow isolation, flexible DOM field matching / autofill execution, hidden element rejection, intelligent semantic/regex heuristics across diverse Sarathi services (`uploadMain.do`, `printform.do`, bootstrap columns, table rows), and full lifecycle event dispatching.
- `test_extension_api_client.js`: **18 passed, 0 failed**.
  - Includes case listing with `review_version`, error responses, `app_status` and `auto` fill packet retrieval, privacy/PII zero-leakage, `fill_attempt` and `autofill_attempt` audit events, asynchronous response sequence tracking, and case selection invalidation.

---

## 5. Amendments Requested by User

### Issue 1: Application Number Visibility & Workflow Inflexibility
- **User Request**:
  > *"I cannot see application number in autofill option, make autofill flexible, a autofill option on top of preview button, if any field matches with any of the DOM, just autofill there (after fixing, add this in report- mention as amendements by user)"*
- **Root Cause**:
  1. The extension popup defaulted to the rigid `dl_application` workflow (`DL Entry — LL Number & DOB`). When operators navigated to `applViewStatus.do` (Application Status), the popup checked for `#lldetailsDisplay`, reported it was missing, and hid `application_no`.
  2. The case metadata badge showed `Reviewed vundefined` because `GET /api/cases` omitted `review_version` and `reviewed_at` from the JSON response.
- **Resolution & Implementation**:
  1. **Flexible Smart Autofill Engine**:
     - Introduced `inspectPageForAutofill(doc, loc)` and `autofillMatchedFields(doc, loc, params)` in `extension/portal_adapter.js`.
     - Scans the active document DOM for known portal inputs (`#applNum`, `#learningLicence`, `#dateOfBirth`, `#DOB`, `#fname`, `#fatherName`, `#presAddress1`).
     - Detects the active page context (`Application Status`, `DL Entry`, or general Sarathi form).
     - Compares detected DOM inputs against all operator-confirmed fields in the case packet.
     - Automatically renders matched field cards with DOM selector badges (e.g., `#applNum`, `#dateOfBirth`) in the Operator-Assisted Autofill section.
  2. **Prominent Placement Above Confirmed Data Preview**:
     - The Operator-Assisted Autofill action card and the active action button (`Autofill Matched Fields (N)`) sit directly on top of the Confirmed Data Preview section.
     - Displays live active page detection status (`Page Detected: Application Status`, `Page Detected: DL Entry`).
     - Operator can autofill all matched fields with a single click.
  3. **Backend `auto` Workflow Profile**:
     - Added `"auto"` to `WORKFLOWS` allowlist in `app/services/fill_packet.py` returning all confirmed fields for flexible matching.
  4. **Fixed `vundefined` Badge**:
     - Updated `list_cases()` in `app/routes/api.py` to return `"review_version": r["review_version"]` and `"reviewed_at": r["reviewed_at"]`.
     - Ensured case version badge displays `v1`, `v2`, etc. correctly.
  5. **Safety Invariants Maintained**:
     - CAPTCHA (`#entcaptxt`, `#capimg`) is strictly excluded.

### Issue 2: Direct Single-Button UX & Elimination of Separate Autofill Card
- **User Request**:
  > *"For application for DL page full name is not required(it is not even exists in that page), but it is showing up in 'operator assisted autfill'. Also dateofbirth isnot auto filling after clicking autofill button. our extension dont need to detect what page it is? It just needs to check DOM, if anything matches with existing fields then should autofill, we dont need to tell what are matched fields, the user just clicks autofill button, then the matching items just fill up. we dont need a seperate 'operator assisted autfill' section for that, just a autofill button top of 'confirmed data preview' section is enough"*
- **Root Causes**:
  1. **Hidden Field Contamination**:
     - On Sarathi's DL entry page (`newDL_displayDLPage.do`), hidden inputs exist: `<input type="hidden" name="fname" id="fname">` and `<input type="hidden" name="dateOfBirth" id="dateOfBirth">`.
     - `isSafeInputField` in `extension/portal_adapter.js` excluded buttons and submit controls, but did not check `type === "hidden"`.
     - `TARGET_FIELD_DEFINITIONS` listed `#fname` under `full_name`, and `#dateOfBirth` before `#DOB` under `dob`.
     - Consequently, `full_name` matched the hidden `#fname`, and `dob` matched the hidden `#dateOfBirth` instead of the visible `#DOB` inside `#lldetailsDisplay`. Setting value on the hidden element left the visible Date of Birth input on the screen completely blank.
  2. **Cluttered Card-Based UI**:
     - The separate "OPERATOR ASSISTED AUTOFILL" card with page-detection banners and matched-field lists was overly verbose.
- **Resolution & Implementation**:
  1. **Strict Hidden Input Rejection**:
     - Updated `isSafeInputField()` and `isElementVisible()` in `portal_adapter.js` to strictly reject `type === "hidden"` (via attribute and property), non-rendered elements (`offsetParent === null`), and non-interactive input types.
     - Refined `TARGET_FIELD_DEFINITIONS` to only target actual visible portal inputs (`application_no`, `licence_no`, and `dob`).
     - Prioritized `#lldetailsDisplay input#DOB` and `#DOB` for DL Entry, and `#publicApplStatus input#dateOfBirth` / `#dateOfBirth` for Application Status.
  2. **Single "Autofill Form" Button UX**:
     - Removed the separate "Operator Assisted Autofill" card, page detection banner, and matched fields list from `extension/popup.html`.
     - Positioned a clean, prominent **Autofill Form** button (`#fillDlBtn`) directly on top of the "Confirmed Data Preview" section.
     - When clicked, the extension inspects the active DOM, matches any confirmed fields against visible form inputs, fills them, formats DOB correctly, and verifies read-back.
     - Results are shown in a single feedback alert (`#fillAlert`) directly below the button.

### Issue 3: Portal Domain Scope Adjustment
- **User Request**:
  > *"make 'https://sarathi.parivahan.gov.in' as elgible portal not 'https://sarathi.parivahan.gov.in/sarathiservice/'"*
- **Resolution & Implementation**:
  - Updated `extension/manifest.json` `host_permissions` and `content_scripts[0].matches` to `"https://sarathi.parivahan.gov.in/*"`.
  - Updated `validatePortalLocation()` in `extension/portal_adapter.js` to accept any path under hostname `sarathi.parivahan.gov.in`.
  - Updated manifest unit test in `tests/test_extension_manifest.py` to assert match pattern `https://sarathi.parivahan.gov.in/*`.

### Issue 4: Application Number Detection Across Various Sarathi Services
- **User Request**:
  > *"There are various services in (https://sarathi.parivahan.gov.in/sarathiservice/sarathiHomePublic.do), which just asks for application number and dob (and captcha), I tried testing in various services I see in many of the services dob is autofilling but not application number, i dont the reason, please find and try to resolve this. (make sure the same with DOB, every feild that appears in form(editable) must autofill, maybe some intelligent feature/regex/lotf of if conditions might help in this complete feature)"*
- **Root Causes**:
  1. **Diverse Attribute Naming in Sarathi Pages**:
     - Different Sarathi service pages (e.g. Upload Document `uploadMain.do`, Print Form `printform.do`, Slot Booking, Fee Payments) use differing names and IDs:
       - *Application Status* (`applViewStatus.do`): `id="applNum"`, `name="applNum"`
       - *Upload Document* (`uploadMain.do`): `id="applicationNum"`, `name="applicationNum"`, `placeholder="Enter ApplicationNumber"`
       - *Print Form* (`printform.do`): `id="applNum"`, `name="applno"`, `placeholder="Application Number"`
       - *Other Services / Struts Forms*: `applnNo`, `appln_no`, `txtApplnNo`, `applnNum`, `applNo` (mixed casing).
     - The previous `TARGET_FIELD_DEFINITIONS` only checked exact lowercase `#applno` or `#applNum`. CSS attribute selectors are case-sensitive, so `#applno` did not match `name="applNo"` or `id="applicationNum"`.
     - In contrast, Date of Birth almost universally used `id="dateOfBirth"` or `id="DOB"`, which was in the hardcoded list, explaining why DOB autofilled while Application Number failed.
  2. **Label Context Missed Due to Container Scoping**:
     - On Sarathi forms, labels and inputs are arranged in Bootstrap columns (e.g. `<div class="col-md-2 labeled">Application Number:</div><div class="col-md-2"><input ...></div>`) or table rows (e.g. `<tr><td>Application Number:</td><td><input ...></td></tr>`).
     - There is no `<label for="...">` attribute linking them.
     - The previous `getElementContextText` evaluated `el.closest("tr, .form-group, .row, fieldset, td")`. Because `td` was in that list, `closest()` stopped at the inner `<td>` or container wrapping the input, never seeing the sibling column or table cell containing the "Application Number:" label text!
  3. **Over-Aggressive Context Exclusion**:
     - `classifyInputElement` previously excluded candidate inputs if `context` included words like `"mobile"` or `"otp"`. Because many Sarathi pages display informational notes in the same container (e.g. "OTP will be sent to your registered mobile number"), this caused the Application Number field to be falsely disqualified.
- **Resolution & Implementation**:
  1. **Universal Multi-Tier Discovery Engine**:
     - **Tier 1 (Explicit Selectors)**: Greatly expanded `TARGET_FIELD_DEFINITIONS` for `application_no` to include all observed Sarathi variations: `#applNum`, `applicationNum`, `applno`, `applNo`, `appno`, `appNo`, `applnNo`, `applnno`, `appln_no`, `applnNum`, `txtApplnNo`, `txtApplnNum`, `appln`, `appnum`, etc.
     - **Tier 2 (Intelligent Heuristic Fallback)**: If an input is not identified by Tier 1 selectors, `classifyInputElement()` evaluates any visible, safe `<input>` in the DOM.
  2. **Comprehensive Context Text Extraction (`getElementContextText`)**:
     - Extracts `id`, `name`, `placeholder`, `title`, and `aria-label`.
     - Checks `el.previousElementSibling` (for inline label tags).
     - Checks `el.parentElement && el.parentElement.previousElementSibling` (capturing preceding Bootstrap label columns `<div class="col-md-2 labeled">` and preceding table cells `<td>`).
     - Climbs to the full row/group container (`tr, .form-group, .row, fieldset`), avoiding premature termination at inner `<td>`s.
  3. **Targeted Negative Guards**:
     - Confines mobile/phone/OTP exclusions to the input's own attributes (`id`, `name`, `placeholder`, `title`), preventing outer instructions from falsely disqualifying the Application Number input.
  4. **Full Lifecycle Event Dispatching**:
     - In `autofillMatchedFields`, dispatches `focus` -> updates `value` -> `input` -> `change` -> `blur`. This ensures framework validation listeners (e.g., jQuery validation, formatting handlers) trigger properly.
  5. **Automated Test Coverage**:
     - Added Tests 30, 31, 32, and 33 in `tests/test_portal_adapter.js`, validating classification across `uploadMain.do`, `printform.do`, NIC shorthand `applnNo`, unbranded Bootstrap rows, unbranded table rows, generic Sarathi services, and normalized common case matching. All 33 tests pass.

### Issue 5: Common Case Checking & Tri-Field Normalization (Learning Licence, DOB, Application Number)
- **User Request**:
  > *"Can you do the same for learning license number and date of birth (also add more cases for application number, i add appnumber, appNumber existed before but appnumber not. maybe a common case for checking is good approach)"*
- **Root Cause**:
  - The developer manually had to add `#appnumber` because `#appNumber` was in camelCase while HTML elements on certain pages used all-lowercase `appnumber` or other casing variants (`APP_NUMBER`, `app_number`, `txtApplnNo`, `learninglicense`, `learning_licence`, `dateofbirth`, `DateOfBirth`, etc.).
  - Relying solely on exact-cased selector strings meant that any casing discrepancy between Sarathi's developers and FormFlow's selector list would cause a match failure in CSS selectors.
- **Resolution & Implementation**:
  1. **Canonical Identifier Normalization (`normalizeIdentifier`)**:
     - Added `normalizeIdentifier(str)` in `portal_adapter.js` converting any element's ID or name to lowercase and stripping all delimiters (`_`, `-`, spaces).
     - Example: `appNumber`, `appnumber`, `AppNumber`, `APP_NUMBER`, `app_number`, `app-number` all normalize to `"appnumber"`.
     - Example: `learningLicence`, `learninglicence`, `learning_licence`, `learningLicense`, `learninglicense` all normalize to `"learninglicence"` / `"learninglicense"`.
     - Example: `dateOfBirth`, `dateofbirth`, `DateOfBirth`, `date_of_birth` all normalize to `"dateofbirth"`.
  2. **Canonical Hash-Set Lookup (`CANONICAL_FIELD_IDENTIFIERS`)**:
     - Precomputed fast sets for `application_no`, `licence_no`, and `dob` containing all canonical normalized forms.
     - `classifyInputElement` checks these sets in O(1) time before falling back to regexes, providing immediate common-case checking that is 100% case-insensitive and delimiter-agnostic.
  3. **Comprehensive Selector Expansions in `TARGET_FIELD_DEFINITIONS`**:
     - Populated all camelCase, lowercase, uppercase, and snake_case variations for `application_no`, `licence_no` (learning licence number), and `dob` (date of birth).
  4. **Document OCR Label Parity ([`app/services/fields.py`](file:///c:/FormFlow/app/services/fields.py) & [`ocr_rules.json`](file:///c:/FormFlow/ocr_rules.json))**:
     - Expanded `licence_no` to include `"learning licence no"`, `"learning license no"`, `"learning licence number"`, `"learning license number"`, `"learner's licence no"`, `"learner's license no"`, `"learning licence"`, `"learning license"`, `"ll number"`.
     - Expanded `application_no` to include `"app number"`, `"appnumber"`, `"application num"`, `"appl number"`, `"appln number"`, `"appl. no"`, `"application ref no"`, `"ack no"`.
     - Expanded `dob` to include `"d.o.b"`, `"d.o.b."`, `"birthdate"`.
  5. **Automated Verification**:
     - Added Test 33 in `tests/test_portal_adapter.js` testing arbitrary casing and delimiter variants (`learningLicense`, `learning_licence`, `LEARNINGLICENCE`, `ll_number`, `txtLLNo`, `learnersLicence`, `dateofbirth`, `DateOfBirth`, `DATE_OF_BIRTH`, `applicant_dob`, `txt_dob`, `appnumber`, `appNumber`, `APP_NUMBER`, `txtApplnNo`, `appln_num`, `application_ref_no`).
     - All 33 portal adapter tests and all 342 Python tests pass.

### Issue 6: Service Field Name Edge Cases (`licNo`) & Label-to-Input Association Engine
- **Reported By**: User (Mobile Number Update Service & General Sarathi Services Testing).
- **Observations & User Questions**:
  1. User discovered `licNo` as the field name for license number in the Sarathi Mobile Number Update service (`/sarathiservice/mobileUpdate.do`), and requested adding more edge cases across all three core fields (`licence_no`, `dob`, `application_no`).
  2. User asked: *"can we utilize label name and its corresponding input field for filling, it might resolve this issue"*.
- **Architectural Analysis**:
  - In government portals like Sarathi, individual service pages use disparate Struts action forms and legacy naming conventions (`licNo`, `txtLicNo`, `applNum`, `regNo`, `struts.token`, `fld_12`).
  - However, the human-visible UI labels ("Driving Licence Number", "Application Number", "Date of Birth") are mandatory for citizen and operator use and remain consistent across services.
  - Relying solely on input IDs/names requires continuous guessing of developer tokens. Implementing a **Label-to-Input Association Engine** allows FormFlow to discover and pair inputs directly from their visual labels via DOM proximity, solving this permanently.
- **Resolution & Implementation**:
  1. **Expanded Edge Cases across Core Fields**:
     - `licence_no`: Added `licNo`, `licno`, `licNumber`, `licnumber`, `licNum`, `licnum`, `txtLicNo`, `txtlicno`, `txtLicNum`, `txtlicnum`, `txtLicNumber`, `curLicNo`, `curlicno`, `curLicNumber`, `currentLicNo`, `oldLicenceNo`, `newLicenceNo`, `dlnum`, `llnum` across `CANONICAL_FIELD_IDENTIFIERS`, `TARGET_FIELD_DEFINITIONS`, and `classifyInputElement`.
     - `application_no`: Added `applCode`, `appCode`, `regNo`, `regNum`, `registrationNo`, `transId`, `applTransId`, `fileNo`, `filenumber`, `refNo`.
     - `dob`: Added `personDob`, `candidateDob`, `holderDob`, `applicantBirthDate`, `txtBirthDate`, `birthDateVal`, `dobVal`.
  2. **Label-to-Input Association Engine (`findInputsByLabels`, `resolveInputFromLabel`)**:
     - Introduced `LABEL_FIELD_PATTERNS` defining semantic regexes for `licence_no`, `application_no`, and `dob` (with strict negative exclusions for mobile, captcha, fee, guardian, etc.).
     - Built `resolveInputFromLabel(labelEl, doc)` with a multi-strategy DOM proximity cascade:
       - **Strategy 1 (`for` attribute)**: Matches `<label for="inputId">` directly to `doc.getElementById(inputId)`.
       - **Strategy 2 (Enclosed Input)**: Matches `<label>Date of Birth: <input></label>`.
       - **Strategy 3 (Next Sibling)**: Traverses `<label>...</label><input>` or container wrappers.
       - **Strategy 4 (Bootstrap Column Pairs)**: Traverses `<div class="col-md-3 labeled">...</div><div class="col-md-5"><input></div>`.
       - **Strategy 5 (Table Row Sibling Cells)**: Traverses `<tr><td>Label</td><td><input></td></tr>`.
       - **Strategy 6 (Form-Group / Row Isolation)**: Associates a label within `.form-group` or `.row` enclosing a single safe input.
  3. **Three-Tier Page Inspection (`inspectPageForAutofill`)**:
     - **Tier 1 (Explicit Selectors)**: Fast path for known standard portal IDs (`#applNum`, `#learningLicence`, `#licNo`, etc.).
     - **Tier 2 (Label Association Engine)**: Deep structural pairing between visible human labels and their associated input fields.
     - **Tier 3 (Heuristic Context Scanner)**: Attribute & surrounding context fuzzy scoring for any non-standard remaining inputs.
  4. **Strict Safety Invariants Maintained**:
     - CAPTCHA controls (`#entcaptxt`, `captcha`, `capimg`) and action buttons are explicitly excluded and can never be associated with any label or filled.
  5. **Automated Verification**:
     - Test 34: Verifies `licNo` and expanded variants across all three fields on simulated Mobile Number Update service DOM.
     - Test 35: Verifies `findInputsByLabels` and `resolveInputFromLabel` across Bootstrap columns, table rows, and enclosed labels with completely unbranded input names.
     - All 35 Portal Adapter tests, 18 extension client tests, and 342 backend tests pass.

---

## 6. Manual Verification Checklist (Chrome)

### Phase 7B Verification (DL Entry)
1. In FormFlow (`http://127.0.0.1:5000`), select a reviewed case (e.g. `CASE-20261015-002`) with confirmed Learner's Licence Number and DOB.
2. In Chrome, navigate to the Sarathi DL Entry page:
   `https://sarathi.parivahan.gov.in/sarathiservice/newDL_displayDLPage.do`.
3. Open the FormFlow extension:
   - Case selection shows the case with review badge `v1` (or `v2`).
   - Directly on top of the Confirmed Data Preview section, the green **Autofill Form** button is enabled.
   - No separate page detection banners or matched field rows appear.
4. Click **Autofill Form**:
   - Learner's Licence Number (`#learningLicence`) is populated with the confirmed LL number.
   - Date of Birth (`#DOB`) is populated with `DD-MM-YYYY` format (e.g., `06-10-2006`).
   - Full Name is NOT populated and not shown.
   - CAPTCHA remains empty.
   - Submit is NOT clicked.
   - Success alert appears below the button.

### Phase 7C Verification (Application Status)
1. In FormFlow, review or edit a case to confirm an Application Number (e.g., `3984089026`) and DOB.
2. In Chrome, navigate to the Sarathi Application Status page:
   `https://sarathi.parivahan.gov.in/sarathiservice/applViewStatus.do`.
3. Open the FormFlow extension and click **Autofill Form**:
   - Application Number (`#applNum`) is populated.
   - Date of Birth (`#dateOfBirth`) is populated with `DD-MM-YYYY` format.
   - CAPTCHA remains untouched.
   - Submit and Cancel buttons are untouched.
   - Success alert confirms fill and reminds operator to enter CAPTCHA manually.
