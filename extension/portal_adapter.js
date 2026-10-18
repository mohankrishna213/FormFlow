/**
 * FormFlow Sarathi Portal Adapter (Phase 7B)
 *
 * Implements strict, section-scoped targeting and verification for Sarathi DL Entry:
 * - Host: sarathi.parivahan.gov.in
 * - Path: /sarathiservice/*
 * - Mode: "Holding Learners Licence" (llHolder1)
 * - Section: #lldetailsDisplay
 * - Targets: #learningLicence and #DOB strictly within #lldetailsDisplay
 *
 * Invariants:
 * - Never touches CAPTCHA (#entcaptxt, #capimg)
 * - Never clicks buttons (#ok, #can1) or submits forms
 * - Never guesses ambiguous dates
 * - Reads back values to confirm exact match
 * - Zero logging of applicant values or tokens
 */

(function () {
  "use strict";

  /**
   * Validate that the active URL is on the official Sarathi portal (https://sarathi.parivahan.gov.in).
   */
  function validatePortalLocation(loc) {
    if (!loc) return false;
    const hostname = (loc.hostname || "").toLowerCase();
    return hostname === "sarathi.parivahan.gov.in";
  }

  /**
   * Convert validated ISO date (YYYY-MM-DD) to portal format (DD-MM-YYYY).
   * Rejects invalid, out-of-range, or ambiguous dates.
   */
  function formatDobForPortal(isoStr) {
    if (!isoStr || typeof isoStr !== "string") {
      throw new Error("Date of birth is missing or invalid");
    }
    const trimmed = isoStr.trim();
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed);
    if (!match) {
      throw new Error(`Invalid ISO date format: '${trimmed}'. Expected YYYY-MM-DD.`);
    }

    const year = parseInt(match[1], 10);
    const month = parseInt(match[2], 10);
    const day = parseInt(match[3], 10);

    if (year < 1900 || year > 2100) {
      throw new Error(`Year ${year} is outside valid range (1900-2100).`);
    }
    if (month < 1 || month > 12) {
      throw new Error(`Month ${month} is outside valid range (1-12).`);
    }

    // Verify calendar date correctness (e.g. leap year, 30 vs 31 days)
    const dateObj = new Date(year, month - 1, day);
    if (
      dateObj.getFullYear() !== year ||
      dateObj.getMonth() !== month - 1 ||
      dateObj.getDate() !== day
    ) {
      throw new Error(`Date '${trimmed}' is not a valid calendar day.`);
    }

    const dd = String(day).padStart(2, "0");
    const mm = String(month).padStart(2, "0");
    const yyyy = String(year);

    return `${dd}-${mm}-${yyyy}`;
  }

  /**
   * Check element visibility across real DOM and headless test environments.
   */
  function isElementVisible(el) {
    if (!el) return false;
    if (el.hidden) return false;
    if (el.style && (el.style.display === "none" || el.style.visibility === "hidden")) {
      return false;
    }
    // In browser with layout engine
    if (typeof window !== "undefined" && window.getComputedStyle) {
      try {
        const style = window.getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") {
          return false;
        }
      } catch (e) {
        // Fallback for detached nodes
      }
    }
    return true;
  }

  /**
   * Inspect current document and location to verify all DL entry prerequisites.
   */
  function inspectDlPage(doc, loc) {
    if (!validatePortalLocation(loc)) {
      return {
        ready: false,
        reason: "Active tab is not on the Sarathi portal (https://sarathi.parivahan.gov.in).",
      };
    }

    if (!doc) {
      return { ready: false, reason: "No document context available." };
    }

    // 1. Verify "Holding Learners Licence" mode
    const llHolder1 = doc.getElementById("llHolder1");
    const allLlHolders = doc.querySelectorAll('input[type="radio"][name="llHolder"]');

    if (allLlHolders && allLlHolders.length > 0) {
      const checkedHolder = doc.querySelector('input[type="radio"][name="llHolder"]:checked');
      if (!checkedHolder) {
        return {
          ready: false,
          reason: "No choice selected under 'Select appropriate choice' (Holding Learners Licence).",
        };
      }
      if (checkedHolder.value !== "1" && checkedHolder.id !== "llHolder1") {
        return {
          ready: false,
          reason: "Current mode is not 'Holding Learners Licence' (different choice selected).",
        };
      }
    } else if (llHolder1 && !llHolder1.checked) {
      return {
        ready: false,
        reason: "'Holding Learners Licence' radio option is not checked.",
      };
    }

    // 2. Verify section container #lldetailsDisplay
    const section = doc.getElementById("lldetailsDisplay");
    if (!section) {
      return {
        ready: false,
        reason: "Learner's Licence details section (#lldetailsDisplay) was not found in page DOM.",
      };
    }

    if (!isElementVisible(section)) {
      return {
        ready: false,
        reason: "Learner's Licence details section (#lldetailsDisplay) is currently hidden.",
      };
    }

    // 3. Find unique, visible, enabled #learningLicence within section
    const llInputs = section.querySelectorAll("#learningLicence");
    if (llInputs.length === 0) {
      return {
        ready: false,
        reason: "Learner's Licence input (#learningLicence) not found inside #lldetailsDisplay.",
      };
    }
    if (llInputs.length > 1) {
      return {
        ready: false,
        reason: `Ambiguous DOM: found ${llInputs.length} #learningLicence inputs in section.`,
      };
    }
    const llInput = llInputs[0];
    if (llInput.disabled || llInput.readOnly) {
      return {
        ready: false,
        reason: "Learner's Licence input is disabled or read-only.",
      };
    }
    if (!isElementVisible(llInput)) {
      return {
        ready: false,
        reason: "Learner's Licence input is hidden.",
      };
    }

    // 4. Find unique, visible, enabled #DOB within section
    const dobInputs = section.querySelectorAll("#DOB");
    if (dobInputs.length === 0) {
      return {
        ready: false,
        reason: "Date of Birth input (#DOB) not found inside #lldetailsDisplay.",
      };
    }
    if (dobInputs.length > 1) {
      return {
        ready: false,
        reason: `Ambiguous DOM: found ${dobInputs.length} #DOB inputs in section.`,
      };
    }
    const dobInput = dobInputs[0];
    if (dobInput.disabled || dobInput.readOnly) {
      return {
        ready: false,
        reason: "Date of Birth input is disabled or read-only.",
      };
    }
    if (!isElementVisible(dobInput)) {
      return {
        ready: false,
        reason: "Date of Birth input is hidden.",
      };
    }

    return {
      ready: true,
      reason: null,
      section,
      llInput,
      dobInput,
    };
  }

  /**
   * Validate fill command payload shape for traceability and correctness.
   */
  function validateFillCommand(params) {
    if (!params || typeof params !== "object") {
      return { valid: false, error: "Fill command payload must be an object." };
    }
    const caseId = typeof params.caseId === "string" ? params.caseId.trim() : "";
    if (!caseId) {
      return { valid: false, error: "Invalid command: non-empty caseId is required for traceability." };
    }
    const reviewVersion = params.reviewVersion;
    if (typeof reviewVersion !== "number" || !Number.isInteger(reviewVersion) || reviewVersion <= 0) {
      return { valid: false, error: "Invalid command: positive integer reviewVersion is required." };
    }
    const licenceNo = typeof params.licenceNo === "string" ? params.licenceNo.trim() : "";
    if (!licenceNo) {
      return { valid: false, error: "Invalid command: non-empty Learner's Licence Number is required." };
    }
    const dobRaw = typeof params.dob === "string" ? params.dob.trim() : "";
    if (!dobRaw) {
      return { valid: false, error: "Invalid command: Date of Birth is required." };
    }
    try {
      formatDobForPortal(dobRaw);
    } catch (err) {
      return { valid: false, error: "Invalid command: " + err.message };
    }
    return { valid: true, error: null };
  }

  /**
   * Explicitly fill #learningLicence and #DOB with framework event dispatch
   * and read-back verification.
   */
  function fillDlFields(doc, loc, params) {
    if (params && (params.caseId !== undefined || params.reviewVersion !== undefined)) {
      const cmdCheck = validateFillCommand(params);
      if (!cmdCheck.valid) {
        return {
          ok: false,
          partial: false,
          error: cmdCheck.error,
        };
      }
    }

    const inspection = inspectDlPage(doc, loc);
    if (!inspection.ready) {
      return {
        ok: false,
        partial: false,
        error: inspection.reason,
      };
    }

    const { llInput, dobInput } = inspection;

    // Validate inputs
    const licenceNo = (params && params.licenceNo ? String(params.licenceNo) : "").trim();
    const dobRaw = params && params.dob ? String(params.dob) : "";

    if (!licenceNo) {
      return {
        ok: false,
        partial: false,
        error: "Learner's Licence number is missing or empty.",
      };
    }

    let formattedDob = "";
    try {
      formattedDob = formatDobForPortal(dobRaw);
    } catch (err) {
      return {
        ok: false,
        partial: false,
        error: "Date of Birth conversion error: " + err.message,
      };
    }

    // Set #learningLicence
    llInput.value = licenceNo;
    llInput.dispatchEvent(new Event("input", { bubbles: true }));
    llInput.dispatchEvent(new Event("change", { bubbles: true }));

    // Set #DOB
    dobInput.value = formattedDob;
    dobInput.dispatchEvent(new Event("input", { bubbles: true }));
    dobInput.dispatchEvent(new Event("change", { bubbles: true }));

    // Read back verification
    const readLl = (llInput.value || "").trim();
    const readDob = (dobInput.value || "").trim();

    const llMatched = readLl === licenceNo;
    const dobMatched = readDob === formattedDob;

    if (llMatched && dobMatched) {
      return {
        ok: true,
        partial: false,
        filled: { licenceNo: true, dob: true },
        message: "Filled Learner's Licence Number and DOB successfully. Complete CAPTCHA manually.",
      };
    }

    if (llMatched && !dobMatched) {
      return {
        ok: false,
        partial: true,
        filled: { licenceNo: true, dob: false },
        error: "Learner's Licence Number was filled, but Date of Birth verification failed.",
      };
    }

    if (!llMatched && dobMatched) {
      return {
        ok: false,
        partial: true,
        filled: { licenceNo: false, dob: true },
        error: "Date of Birth was filled, but Learner's Licence Number verification failed.",
      };
    }

    return {
      ok: false,
      partial: false,
      filled: { licenceNo: false, dob: false },
      error: "Both fields failed verification after fill attempt.",
    };
  }

  /**
   * Inspect document and location for Application Status prerequisites (Phase 7C).
   * Verified against live Sarathi page: /sarathiservice/applViewStatus.do
   * - Enclosing container: #publicApplStatus (inside form#applViewStages)
   * - Application Number: #publicApplStatus input#applNum
   * - Date of Birth: #publicApplStatus input#dateOfBirth
   * - Excluded: CAPTCHA (#entcaptxt) and action buttons (#submit, #cancel)
   */
  function inspectAppStatusPage(doc, loc) {
    if (!validatePortalLocation(loc)) {
      return {
        ready: false,
        reason: "Active tab is not on the Sarathi portal (https://sarathi.parivahan.gov.in).",
      };
    }

    if (!doc) {
      return { ready: false, reason: "No document context available." };
    }

    // 1. Verify container #publicApplStatus or form #applViewStages
    const section = doc.getElementById("publicApplStatus") || doc.getElementById("applViewStages");
    if (!section) {
      return {
        ready: false,
        reason: "Application Status section (#publicApplStatus) was not found in page DOM.",
      };
    }

    if (!isElementVisible(section)) {
      return {
        ready: false,
        reason: "Application Status section (#publicApplStatus) is currently hidden.",
      };
    }

    // 2. Find unique, visible, enabled #applNum within section
    const applNumInputs = section.querySelectorAll("#applNum");
    if (applNumInputs.length === 0) {
      return {
        ready: false,
        reason: "Application Number input (#applNum) not found inside #publicApplStatus.",
      };
    }
    if (applNumInputs.length > 1) {
      return {
        ready: false,
        reason: `Ambiguous DOM: found ${applNumInputs.length} #applNum inputs in section.`,
      };
    }
    const applNumInput = applNumInputs[0];
    if (applNumInput.disabled || applNumInput.readOnly) {
      return {
        ready: false,
        reason: "Application Number input is disabled or read-only.",
      };
    }
    if (!isElementVisible(applNumInput)) {
      return {
        ready: false,
        reason: "Application Number input is hidden.",
      };
    }

    // 3. Find unique, visible, enabled #dateOfBirth within section
    const dobInputs = section.querySelectorAll("#dateOfBirth");
    if (dobInputs.length === 0) {
      return {
        ready: false,
        reason: "Date of Birth input (#dateOfBirth) not found inside #publicApplStatus.",
      };
    }
    if (dobInputs.length > 1) {
      return {
        ready: false,
        reason: `Ambiguous DOM: found ${dobInputs.length} #dateOfBirth inputs in section.`,
      };
    }
    const dobInput = dobInputs[0];
    if (dobInput.disabled || dobInput.readOnly) {
      return {
        ready: false,
        reason: "Date of Birth input is disabled or read-only.",
      };
    }
    if (!isElementVisible(dobInput)) {
      return {
        ready: false,
        reason: "Date of Birth input is hidden.",
      };
    }

    return {
      ready: true,
      reason: null,
      section,
      applNumInput,
      dobInput,
    };
  }

  /**
   * Validate command metadata for Application Status autofill (Phase 7C).
   */
  function validateAppStatusCommand(params) {
    if (!params || typeof params !== "object") {
      return { valid: false, error: "Fill command payload must be an object." };
    }
    const caseId = typeof params.caseId === "string" ? params.caseId.trim() : "";
    if (!caseId) {
      return { valid: false, error: "Invalid command: non-empty caseId is required for traceability." };
    }
    const reviewVersion = params.reviewVersion;
    if (typeof reviewVersion !== "number" || !Number.isInteger(reviewVersion) || reviewVersion <= 0) {
      return { valid: false, error: "Invalid command: positive integer reviewVersion is required." };
    }
    const applicationNo = typeof params.applicationNo === "string" ? params.applicationNo.trim() : "";
    if (!applicationNo) {
      return { valid: false, error: "Invalid command: non-empty Application Number is required." };
    }
    const dobRaw = typeof params.dob === "string" ? params.dob.trim() : "";
    if (!dobRaw) {
      return { valid: false, error: "Invalid command: Date of Birth is required." };
    }
    try {
      formatDobForPortal(dobRaw);
    } catch (err) {
      return { valid: false, error: "Invalid command: " + err.message };
    }
    return { valid: true, error: null };
  }

  /**
   * Explicitly fill Application Status fields (#applNum and #dateOfBirth) with
   * framework event dispatch and read-back verification (Phase 7C).
   */
  function fillAppStatusFields(doc, loc, params) {
    if (params && (params.caseId !== undefined || params.reviewVersion !== undefined)) {
      const cmdCheck = validateAppStatusCommand(params);
      if (!cmdCheck.valid) {
        return {
          ok: false,
          partial: false,
          error: cmdCheck.error,
        };
      }
    }

    const inspection = inspectAppStatusPage(doc, loc);
    if (!inspection.ready) {
      return {
        ok: false,
        partial: false,
        error: inspection.reason,
      };
    }

    const { applNumInput, dobInput } = inspection;

    const applicationNo = (params && params.applicationNo ? String(params.applicationNo) : "").trim();
    const dobRaw = params && params.dob ? String(params.dob) : "";

    if (!applicationNo) {
      return {
        ok: false,
        partial: false,
        error: "Application Number is missing or empty.",
      };
    }

    let formattedDob = "";
    try {
      formattedDob = formatDobForPortal(dobRaw);
    } catch (err) {
      return {
        ok: false,
        partial: false,
        error: "Date of Birth conversion error: " + err.message,
      };
    }

    // Set #applNum
    applNumInput.value = applicationNo;
    applNumInput.dispatchEvent(new Event("input", { bubbles: true }));
    applNumInput.dispatchEvent(new Event("change", { bubbles: true }));

    // Set #dateOfBirth
    dobInput.value = formattedDob;
    dobInput.dispatchEvent(new Event("input", { bubbles: true }));
    dobInput.dispatchEvent(new Event("change", { bubbles: true }));

    // Read back verification
    const readApplNum = (applNumInput.value || "").trim();
    const readDob = (dobInput.value || "").trim();

    const applMatched = readApplNum === applicationNo;
    const dobMatched = readDob === formattedDob;

    if (applMatched && dobMatched) {
      return {
        ok: true,
        partial: false,
        filled: { applicationNo: true, dob: true },
        message: "Filled Application Number and DOB successfully. Complete CAPTCHA manually.",
      };
    }

    if (applMatched && !dobMatched) {
      return {
        ok: false,
        partial: true,
        filled: { applicationNo: true, dob: false },
        error: "Application Number was filled, but Date of Birth verification failed.",
      };
    }

    if (!applMatched && dobMatched) {
      return {
        ok: false,
        partial: true,
        filled: { applicationNo: false, dob: true },
        error: "Date of Birth was filled, but Application Number verification failed.",
      };
    }

    return {
      ok: false,
      partial: false,
      filled: { applicationNo: false, dob: false },
      error: "Both fields failed verification after fill attempt.",
    };
  }

  // -------------------------------------------------------------------------
  // Flexible Smart Autofill (Amendment Requested by User)
  // Scans active DOM for any matching Sarathi fields and autofills them safely.
  // -------------------------------------------------------------------------

  /**
   * Normalize an identifier by converting to lowercase and stripping delimiters (hyphens, underscores, spaces).
   * Ensures casing differences (e.g. appNumber vs appnumber vs APP_NUMBER) match uniformly.
   */
  function normalizeIdentifier(str) {
    if (!str || typeof str !== "string") return "";
    return str.toLowerCase().replace(/[-_\s]/g, "");
  }

  const CANONICAL_FIELD_IDENTIFIERS = {
    application_no: new Set([
      "appnumber", "appno", "applno", "applnno", "applicationno",
      "appnum", "applnum", "applicationnum", "applnnum",
      "applicationnumber", "applnnumber", "appln", "appl", "application",
      "txtappno", "txtapplno", "txtapplicationno", "txtappnumber",
      "txtapplicationnumber", "txtapplnno", "txtapplnnum", "txtappln",
      "txtappl", "txtapp", "applid", "appid", "applicationid",
      "applrefno", "apprefno", "applicationrefno", "refno",
      "ackno", "acknum", "acknowledgementno", "fileno", "filenumber",
      "regno", "regnum", "regnumber", "registrationno", "transid", "appltransid",
      "applcode", "appcode", "applseqno",
    ]),
    licence_no: new Set([
      "learninglicence", "learninglicense", "licenceno", "licenseno",
      "licencenumber", "licensenumber", "llno", "llnumber",
      "txtllno", "txtllnumber", "txtdlno", "txtdlnumber",
      "dlno", "dlnumber", "drivinglicenceno", "drivinglicenseno",
      "drivinglicence", "drivinglicense", "learnerslicence", "learnerslicense",
      "learnerlicence", "learnerlicense", "licence", "license",
      "licno", "licnumber", "licnum", "lic", "txtlicno", "txtlicnum", "txtlic",
      "txtlicnumber", "dlnum", "llnum", "dl_number", "ll_number", "lic_number", "lic_no", "lic_num",
      "learninglicenceno", "learninglicenseno", "learnerslicenceno", "learnerslicenseno",
      "curlicno", "curlicnumber", "curlicenceno", "currentlicno", "curlicenseno",
      "oldlicenceno", "oldlicenseno", "oldlicno", "newlicenceno", "newlicenseno", "newlicno",
    ]),
    dob: new Set([
      "dob", "dateofbirth", "birthdate", "appldob", "applicantdob",
      "appldateofbirth", "applicantdateofbirth", "txtdob", "txtdateofbirth",
      "dobdate", "birthdate", "datebirth", "persondob", "candidatedob", "holderdob",
      "applicantbirthdate", "applicant_birth_date", "applicant_dob", "birth_date",
      "date_of_birth", "d_o_b", "txtbirthdate", "txt_birth_date",
      "birthdateval", "dobval",
    ]),
  };

  const TARGET_FIELD_DEFINITIONS = [
    {
      key: "application_no",
      label: "Application Number",
      selectors: [
        "#publicApplStatus input#applNum",
        "#publicApplStatus #applNum",
        "#publicApplStatus input#applnum",
        "#publicApplStatus #applnum",
        "#applNum",
        "input[name='applNum']",
        "#applnum",
        "input[name='applnum']",
        "#applicationNum",
        "input[name='applicationNum']",
        "#applicationnum",
        "input[name='applicationnum']",
        "#applno",
        "input[name='applno']",
        "#applNo",
        "input[name='applNo']",
        "#appno",
        "input[name='appno']",
        "#appNo",
        "input[name='appNo']",
        "#applicationNumber",
        "input[name='applicationNumber']",
        "#applicationnumber",
        "input[name='applicationnumber']",
        "#applicationNo",
        "input[name='applicationNo']",
        "#applicationno",
        "input[name='applicationno']",
        "#appNumber",
        "input[name='appNumber']",
        "#appnumber",
        "input[name='appnumber']",
        "#applnNo",
        "input[name='applnNo']",
        "#applnno",
        "input[name='applnno']",
        "#appln_no",
        "input[name='appln_no']",
        "#applnNum",
        "input[name='applnNum']",
        "#applnnum",
        "input[name='applnnum']",
        "#applnNumber",
        "input[name='applnNumber']",
        "#applnnumber",
        "input[name='applnnumber']",
        "#txtApplnNo",
        "input[name='txtApplnNo']",
        "#txtapplnno",
        "input[name='txtapplnno']",
        "#txtApplnNum",
        "input[name='txtApplnNum']",
        "#txtapplnnum",
        "input[name='txtapplnnum']",
        "#appln",
        "input[name='appln']",
        "#appnum",
        "input[name='appnum']",
        "#appl_no",
        "input[name='appl_no']",
        "#application_no",
        "input[name='application_no']",
        "#appl_number",
        "input[name='appl_number']",
        "#application_num",
        "input[name='application_num']",
        "#txtApplNum",
        "input[name='txtApplNum']",
        "#txtapplnum",
        "input[name='txtapplnum']",
        "#txtApplNo",
        "input[name='txtApplNo']",
        "#txtapplno",
        "input[name='txtapplno']",
        "#txtAppNo",
        "input[name='txtAppNo']",
        "#txtappno",
        "input[name='txtappno']",
        "#txtApplicationNo",
        "input[name='txtApplicationNo']",
        "#txtapplicationno",
        "input[name='txtapplicationno']",
        "#txtApplicationNumber",
        "input[name='txtApplicationNumber']",
        "#txtapplicationnumber",
        "input[name='txtapplicationnumber']",
        "#txtAppNumber",
        "input[name='txtAppNumber']",
        "#txtappnumber",
        "input[name='txtappnumber']",
        "#applId",
        "input[name='applId']",
        "#applid",
        "input[name='applid']",
        "#appId",
        "input[name='appId']",
        "#appid",
        "input[name='appid']",
        "#app_id",
        "input[name='app_id']",
        "#applRefNo",
        "input[name='applRefNo']",
        "#applrefno",
        "input[name='applrefno']",
        "#appRefNo",
        "input[name='appRefNo']",
        "#apprefno",
        "input[name='apprefno']",
        "#applicationRefNo",
        "input[name='applicationRefNo']",
        "#applicationrefno",
        "input[name='applicationrefno']",
        "#ackNo",
        "input[name='ackNo']",
        "#ackno",
        "input[name='ackno']",
        "#acknowledgementNo",
        "input[name='acknowledgementNo']",
        "#acknowledgementno",
        "input[name='acknowledgementno']",
        "#applCode",
        "input[name='applCode']",
        "#applcode",
        "input[name='applcode']",
        "#appCode",
        "input[name='appCode']",
        "#appcode",
        "input[name='appcode']",
        "#regNo",
        "input[name='regNo']",
        "#regno",
        "input[name='regno']",
        "#regNum",
        "input[name='regNum']",
        "#regnum",
        "input[name='regnum']",
        "#registrationNo",
        "input[name='registrationNo']",
        "#registrationno",
        "input[name='registrationno']",
        "#transId",
        "input[name='transId']",
        "#transid",
        "input[name='transid']",
        "#applTransId",
        "input[name='applTransId']",
        "#appltransid",
        "input[name='appltransid']",
        "#fileNo",
        "input[name='fileNo']",
        "#fileno",
        "input[name='fileno']",
        "#fileNumber",
        "input[name='fileNumber']",
        "#filenumber",
        "input[name='filenumber']",
        "#refNo",
        "input[name='refNo']",
        "#refno",
        "input[name='refno']",
      ],
    },
    {
      key: "licence_no",
      label: "Learner's Licence Number",
      selectors: [
        "#lldetailsDisplay input#learningLicence",
        "#lldetailsDisplay #learningLicence",
        "#lldetailsDisplay input#learninglicence",
        "#lldetailsDisplay #learninglicence",
        "#lldetailsDisplay input#learningLicense",
        "#lldetailsDisplay #learningLicense",
        "#lldetailsDisplay input#learninglicense",
        "#lldetailsDisplay #learninglicense",
        "#learningLicence",
        "input[name='learningLicence']",
        "#learninglicence",
        "input[name='learninglicence']",
        "#learningLicense",
        "input[name='learningLicense']",
        "#learninglicense",
        "input[name='learninglicense']",
        "#learning_licence",
        "input[name='learning_licence']",
        "#learning_license",
        "input[name='learning_license']",
        "#licNo",
        "input[name='licNo']",
        "#licno",
        "input[name='licno']",
        "#licNumber",
        "input[name='licNumber']",
        "#licnumber",
        "input[name='licnumber']",
        "#licNum",
        "input[name='licNum']",
        "#licnum",
        "input[name='licnum']",
        "#licenceNo",
        "input[name='licenceNo']",
        "#licenceno",
        "input[name='licenceno']",
        "#licenseNo",
        "input[name='licenseNo']",
        "#licenseno",
        "input[name='licenseno']",
        "#licenceNumber",
        "input[name='licenceNumber']",
        "#licencenumber",
        "input[name='licencenumber']",
        "#licenseNumber",
        "input[name='licenseNumber']",
        "#licensenumber",
        "input[name='licensenumber']",
        "#txtLicNo",
        "input[name='txtLicNo']",
        "#txtlicno",
        "input[name='txtlicno']",
        "#txtLicNum",
        "input[name='txtLicNum']",
        "#txtlicnum",
        "input[name='txtlicnum']",
        "#txtLicNumber",
        "input[name='txtLicNumber']",
        "#txtlicnumber",
        "input[name='txtlicnumber']",
        "#llNo",
        "input[name='llNo']",
        "#llno",
        "input[name='llno']",
        "#ll_no",
        "input[name='ll_no']",
        "#llNumber",
        "input[name='llNumber']",
        "#llnumber",
        "input[name='llnumber']",
        "#ll_number",
        "input[name='ll_number']",
        "#txtLLNo",
        "input[name='txtLLNo']",
        "#txtllno",
        "input[name='txtllno']",
        "#txtLLNumber",
        "input[name='txtLLNumber']",
        "#txtllnumber",
        "input[name='txtllnumber']",
        "#dlno",
        "input[name='dlno']",
        "#dlNo",
        "input[name='dlNo']",
        "#dl_no",
        "input[name='dl_no']",
        "#dlNumber",
        "input[name='dlNumber']",
        "#dlnumber",
        "input[name='dlnumber']",
        "#dl_number",
        "input[name='dl_number']",
        "#dlnum",
        "input[name='dlnum']",
        "#dlNum",
        "input[name='dlNum']",
        "#llnum",
        "input[name='llnum']",
        "#llNum",
        "input[name='llNum']",
        "#txtDLNo",
        "input[name='txtDLNo']",
        "#txtdlno",
        "input[name='txtdlno']",
        "#txtDLNumber",
        "input[name='txtDLNumber']",
        "#txtdlnumber",
        "input[name='txtdlnumber']",
        "#drivingLicenceNo",
        "input[name='drivingLicenceNo']",
        "#drivinglicenceno",
        "input[name='drivinglicenceno']",
        "#drivingLicenseNo",
        "input[name='drivingLicenseNo']",
        "#drivinglicenseno",
        "input[name='drivinglicenseno']",
        "#driving_licence_no",
        "input[name='driving_licence_no']",
        "#driving_license_no",
        "input[name='driving_license_no']",
        "#licence_no",
        "input[name='licence_no']",
        "#license_no",
        "input[name='license_no']",
        "#learnersLicence",
        "input[name='learnersLicence']",
        "#learnerslicence",
        "input[name='learnerslicence']",
        "#learners_licence",
        "input[name='learners_licence']",
        "#learnersLicense",
        "input[name='learnersLicense']",
        "#learnerslicense",
        "input[name='learnerslicense']",
        "#learners_license",
        "input[name='learners_license']",
        "#learnerLicence",
        "input[name='learnerLicence']",
        "#learnerlicence",
        "input[name='learnerlicence']",
        "#learnerLicense",
        "input[name='learnerLicense']",
        "#learnerlicense",
        "input[name='learnerlicense']",
        "#curLicNo",
        "input[name='curLicNo']",
        "#curlicno",
        "input[name='curlicno']",
        "#curLicNumber",
        "input[name='curLicNumber']",
        "#curlicnumber",
        "input[name='curlicnumber']",
        "#currentLicNo",
        "input[name='currentLicNo']",
        "#currentlicno",
        "input[name='currentlicno']",
        "#oldLicenceNo",
        "input[name='oldLicenceNo']",
        "#oldlicenceno",
        "input[name='oldlicenceno']",
        "#newLicenceNo",
        "input[name='newLicenceNo']",
        "#newlicenceno",
        "input[name='newlicenceno']",
      ],
    },
    {
      key: "dob",
      label: "Date of Birth",
      isDob: true,
      selectors: [
        "#lldetailsDisplay input#DOB",
        "#lldetailsDisplay #DOB",
        "#lldetailsDisplay input#dob",
        "#lldetailsDisplay #dob",
        "#publicApplStatus input#dateOfBirth",
        "#publicApplStatus #dateOfBirth",
        "#publicApplStatus input#dateofbirth",
        "#publicApplStatus #dateofbirth",
        "#dateOfBirth",
        "input[name='dateOfBirth']",
        "#dateofbirth",
        "input[name='dateofbirth']",
        "#DateOfBirth",
        "input[name='DateOfBirth']",
        "#DOB",
        "input[name='DOB']",
        "#dob",
        "input[name='dob']",
        "#appldob",
        "input[name='appldob']",
        "#applDob",
        "input[name='applDob']",
        "#appldateofbirth",
        "input[name='appldateofbirth']",
        "#applDateOfBirth",
        "input[name='applDateOfBirth']",
        "#applicantDob",
        "input[name='applicantDob']",
        "#applicantdob",
        "input[name='applicantdob']",
        "#applicantDateOfBirth",
        "input[name='applicantDateOfBirth']",
        "#applicantdateofbirth",
        "input[name='applicantdateofbirth']",
        "#personDob",
        "input[name='personDob']",
        "#persondob",
        "input[name='persondob']",
        "#candidateDob",
        "input[name='candidateDob']",
        "#candidatedob",
        "input[name='candidatedob']",
        "#holderDob",
        "input[name='holderDob']",
        "#holderdob",
        "input[name='holderdob']",
        "#applicantBirthDate",
        "input[name='applicantBirthDate']",
        "#applicantbirthdate",
        "input[name='applicantbirthdate']",
        "#applicant_birth_date",
        "input[name='applicant_birth_date']",
        "#txtDateOfBirth",
        "input[name='txtDateOfBirth']",
        "#txtdateofbirth",
        "input[name='txtdateofbirth']",
        "#txtDOB",
        "input[name='txtDOB']",
        "#txtdob",
        "input[name='txtdob']",
        "#txtBirthDate",
        "input[name='txtBirthDate']",
        "#txtbirthdate",
        "input[name='txtbirthdate']",
        "#txt_birth_date",
        "input[name='txt_birth_date']",
        "#birthDate",
        "input[name='birthDate']",
        "#birthdate",
        "input[name='birthdate']",
        "#BirthDate",
        "input[name='BirthDate']",
        "#birthDateVal",
        "input[name='birthDateVal']",
        "#birthdateval",
        "input[name='birthdateval']",
        "#dobVal",
        "input[name='dobVal']",
        "#dobval",
        "input[name='dobval']",
        "#date_of_birth",
        "input[name='date_of_birth']",
        "#birth_date",
        "input[name='birth_date']",
        "#appl_dob",
        "input[name='appl_dob']",
        "#appl_date_of_birth",
        "input[name='appl_date_of_birth']",
        "#applicant_dob",
        "input[name='applicant_dob']",
        "#dobDate",
        "input[name='dobDate']",
        "#dobdate",
        "input[name='dobdate']",
      ],
    },
  ];

  /**
   * Check element visibility across real DOM and headless test environments.
   */
  function isElementVisible(el) {
    if (!el) return false;
    if (el.hidden) return false;
    const type = (el.type || (el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
    if (type === "hidden") return false;
    if (el.style && (el.style.display === "none" || el.style.visibility === "hidden")) {
      return false;
    }
    // In browser with layout engine
    if (typeof window !== "undefined" && window.getComputedStyle) {
      try {
        const style = window.getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") {
          return false;
        }
      } catch (e) {
        // Fallback for detached nodes
      }
    }
    // In real browser, check offsetParent unless it's body or position fixed
    if (
      typeof el.offsetParent !== "undefined" &&
      el.offsetParent === null &&
      (el.tagName || "").toUpperCase() !== "BODY"
    ) {
      if (typeof window !== "undefined" && window.getComputedStyle) {
        try {
          const style = window.getComputedStyle(el);
          if (style.position !== "fixed") {
            return false;
          }
        } catch (e) { }
      } else {
        return false;
      }
    }
    return true;
  }

  /**
   * Check if a candidate input element is safe to interact with (never CAPTCHA, buttons, or hidden).
   */
  function isSafeInputField(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toUpperCase();
    if (tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") return false;
    const type = (el.type || (el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
    if (
      type === "hidden" ||
      type === "submit" ||
      type === "button" ||
      type === "reset" ||
      type === "image" ||
      type === "file" ||
      type === "checkbox" ||
      type === "radio" ||
      type === "password"
    ) {
      return false;
    }
    const id = (el.id || "").toLowerCase();
    const name = (el.name || "").toLowerCase();
    const placeholder = (el.placeholder || (el.getAttribute && el.getAttribute("placeholder")) || "").toLowerCase();
    const title = (el.title || (el.getAttribute && el.getAttribute("title")) || "").toLowerCase();
    // Exclude CAPTCHA strictly
    if (
      id === "entcaptxt" ||
      name === "entcaptxt" ||
      id.includes("captcha") ||
      name.includes("captcha") ||
      placeholder.includes("captcha") ||
      title.includes("captcha") ||
      id.includes("capimg")
    ) {
      return false;
    }
    // Exclude action buttons
    if (id === "submit" || id === "cancel" || id === "ok" || id === "can1") {
      return false;
    }
    if (el.disabled || el.readOnly) return false;
    if (!isElementVisible(el)) return false;
    return true;
  }

  /**
   * Collect textual metadata for an input element (id, name, placeholder, title, aria labels, and surrounding <label>s).
   */
  function getElementContextText(el, doc) {
    if (!el) return "";
    const parts = [];

    if (el.id) parts.push(el.id);
    if (el.name) parts.push(el.name);
    if (el.placeholder) parts.push(el.placeholder);
    if (el.title) parts.push(el.title);

    const attrPlaceholder = el.getAttribute && el.getAttribute("placeholder");
    if (attrPlaceholder && attrPlaceholder !== el.placeholder) parts.push(attrPlaceholder);
    const attrTitle = el.getAttribute && el.getAttribute("title");
    if (attrTitle && attrTitle !== el.title) parts.push(attrTitle);
    const ariaLabel = el.getAttribute && el.getAttribute("aria-label");
    if (ariaLabel) parts.push(ariaLabel);

    // Direct previous element sibling (e.g. <label> placed directly before <input>)
    try {
      if (el.previousElementSibling && el.previousElementSibling.textContent) {
        parts.push(el.previousElementSibling.textContent);
      }
    } catch (e) { }

    // Associated <label> elements
    if (el.labels && el.labels.length > 0) {
      for (let i = 0; i < el.labels.length; i++) {
        if (el.labels[i].textContent) parts.push(el.labels[i].textContent);
      }
    } else if (el.id && doc && doc.querySelector) {
      try {
        const lbl = doc.querySelector(`label[for="${el.id}"]`);
        if (lbl && lbl.textContent) parts.push(lbl.textContent);
      } catch (e) { }
    }

    // Enclosing <label>
    try {
      if (typeof el.closest === "function") {
        const parentLabel = el.closest("label");
        if (parentLabel && parentLabel.textContent) parts.push(parentLabel.textContent);
      }
    } catch (e) { }

    // Preceding column or table cell:
    // e.g. <div class="col-md-2 labeled">Application Number:</div><div class="col-md-2"><input id="applNum"></div>
    // e.g. <td>Application Number</td><td><input name="applno"></td>
    try {
      if (el.parentElement && el.parentElement.previousElementSibling) {
        const prevSiblingText = el.parentElement.previousElementSibling.textContent;
        if (prevSiblingText && prevSiblingText.length < 120) {
          parts.push(prevSiblingText);
        }
      }
    } catch (e) { }

    // Surrounding form group or table row labels
    try {
      if (typeof el.closest === "function") {
        const container = el.closest("tr, .form-group, .row, fieldset");
        if (container) {
          const textNodes = container.querySelectorAll("label, th, td, span, div");
          for (let i = 0; i < Math.min(textNodes.length, 10); i++) {
            const node = textNodes[i];
            if (node !== el && !node.contains(el) && node.textContent && node.textContent.length < 100) {
              parts.push(node.textContent);
            }
          }
        }
      }
    } catch (e) { }

    return parts.join(" ").toLowerCase();
  }

  /**
   * Determine licence target type: 'll' (Learner's Licence), 'dl' (Driving Licence), or 'generic'.
   * Discovered from available target evidence: explicit selectors, element IDs/names, labels, or context.
   */
  function getLicenceTargetType(selector, element, doc, labelText) {
    const sel = (selector || "").toLowerCase();
    const id = ((element && element.id) || "").toLowerCase();
    const name = ((element && element.name) || "").toLowerCase();
    const lbl = (labelText || "").toLowerCase();

    let ctx = lbl;
    if (!ctx && element) {
      ctx = getElementContextText(element, doc);
    } else {
      ctx = ctx.toLowerCase();
    }

    // Explicit LL evidence: section, selector, ID, name, or label
    const isExplicitLL =
      sel.includes("lldetailsdisplay") ||
      (element && typeof element.closest === "function" && element.closest("#lldetailsDisplay")) ||
      /(?:learning[\s._-]*licen[cs]e|learner['’]?s?[\s._-]*licen[cs]e|\bll[\s._-]*(?:no|num|number)?\b|txtllno|txtllnumber|llnum)/i.test(sel) ||
      /(?:learning[\s._-]*licen[cs]e|learner['’]?s?[\s._-]*licen[cs]e|\bll[\s._-]*(?:no|num|number)?\b|txtllno|txtllnumber|llnum)/i.test(id) ||
      /(?:learning[\s._-]*licen[cs]e|learner['’]?s?[\s._-]*licen[cs]e|\bll[\s._-]*(?:no|num|number)?\b|txtllno|txtllnumber|llnum)/i.test(name) ||
      /(?:learner['’]?s?\s*licen[cs]e|learning\s*licen[cs]e|\bll\s*(?:no|num|number)?\b)/i.test(ctx);

    if (isExplicitLL) {
      return "ll";
    }

    // Explicit DL evidence: section, selector, ID, name, or label
    const isExplicitDL =
      sel.includes("dldetailsdisplay") ||
      (element && typeof element.closest === "function" && element.closest("#dldetailsDisplay")) ||
      /(?:driving[\s._-]*licen[cs]e|\bdl[\s._-]*(?:no|num|number)?\b|txtdlno|txtdlnumber|dlnum)/i.test(sel) ||
      /(?:driving[\s._-]*licen[cs]e|\bdl[\s._-]*(?:no|num|number)?\b|txtdlno|txtdlnumber|dlnum)/i.test(id) ||
      /(?:driving[\s._-]*licen[cs]e|\bdl[\s._-]*(?:no|num|number)?\b|txtdlno|txtdlnumber|dlnum)/i.test(name) ||
      /(?:driving\s*licen[cs]e|\bdl\s*(?:no|num|number)?\b)/i.test(ctx);

    if (isExplicitDL) {
      return "dl";
    }

    return "generic";
  }

  // -------------------------------------------------------------------------
  // Label-to-Input Association Engine
  // Resolves form controls by pairing visible human labels (<label>, th, td,
  // .labeled, .control-label) with their corresponding input fields via
  // DOM proximity, 'for' attributes, Bootstrap column grids, and table cells.
  // -------------------------------------------------------------------------

  const LABEL_FIELD_PATTERNS = [
    {
      key: "licence_no",
      label: "Learner's Licence Number",
      pattern: /(?:learner['’]?s?\s*licen[cs]e|learning\s*licen[cs]e|driving\s*licen[cs]e|licen[cs]e\s*(?:no|num|number)?|\bll\s*(?:no|num|number)?\b|\bdl\s*(?:no|num|number)?\b|\blic\s*(?:no|num|number)?\b)/i,
      negative: /(?:father|mother|parent|guardian|spouse|mobile|phone|otp|aadhaar|uid|fee|amount|chassis|engine|vehicle)/i,
    },
    {
      key: "application_no",
      label: "Application Number",
      pattern: /(?:application\s*(?:no|num|number|id)?|appl(?:\.|\b)\s*(?:no|num|number)?|appln\s*(?:no|num|number)?|reference\s*(?:no|number)|acknowledg?ment\s*no)/i,
      negative: /(?:mobile|phone|otp|aadhaar|uid|fee|amount|date|dob)/i,
    },
    {
      key: "dob",
      label: "Date of Birth",
      isDob: true,
      pattern: /(?:date\s*of\s*birth|birth\s*date|\bdob\b)/i,
      negative: /(?:father|mother|parent|guardian|spouse)/i,
    },
  ];

  /**
   * Resolves the corresponding editable input control associated with a label element.
   * Uses a multi-strategy DOM traversal heuristic:
   * 1. 'for' attribute mapping to element ID
   * 2. Direct descendant/enclosed inputs (<label>Text <input></label>)
   * 3. Sibling element proximity (e.g. <label>...<input>)
   * 4. Multi-column/Bootstrap grid sibling columns (e.g. .col-md-2.labeled + .col-md-4)
   * 5. Sibling cells inside table rows (<tr><td>Label</td><td><input></td></tr>)
   * 6. Form group / row isolation where a single safe input is enclosed
   */
  function resolveInputFromLabel(labelEl, doc) {
    if (!labelEl) return null;

    // Strategy 1: Explicit 'for' attribute
    const forAttr = (labelEl.getAttribute && labelEl.getAttribute("for")) || labelEl.htmlFor;
    if (forAttr && doc) {
      let target = null;
      if (typeof doc.getElementById === "function") {
        target = doc.getElementById(forAttr);
      }
      if (!target && typeof doc.querySelector === "function") {
        try {
          target = doc.querySelector(`#${forAttr}`);
        } catch (e) { }
      }
      if (target && isSafeInputField(target)) {
        return target;
      }
    }

    // Strategy 2: Enclosed child input (<label>Label Text <input ...></label>)
    if (typeof labelEl.querySelectorAll === "function") {
      try {
        const enclosed = Array.from(labelEl.querySelectorAll("input, textarea, select")).filter(isSafeInputField);
        if (enclosed.length > 0) return enclosed[0];
      } catch (e) { }
    }

    // Strategy 3: Next direct element sibling
    let next = labelEl.nextElementSibling;
    while (next) {
      if (isSafeInputField(next)) return next;
      if (typeof next.querySelectorAll === "function") {
        try {
          const inside = Array.from(next.querySelectorAll("input, textarea, select")).filter(isSafeInputField);
          if (inside.length > 0) return inside[0];
        } catch (e) { }
      }
      const tag = (next.tagName || "").toLowerCase();
      // If we encounter another distinct label element, don't overshoot
      if (tag === "label" || tag === "th" || (next.className && /labeled|control-label/.test(next.className))) {
        break;
      }
      next = next.nextElementSibling;
    }

    // Strategy 4: Parent element's next sibling (Bootstrap col-md-* layout)
    const parent = labelEl.parentElement;
    if (parent) {
      let parentNext = parent.nextElementSibling;
      while (parentNext) {
        if (isSafeInputField(parentNext)) return parentNext;
        if (typeof parentNext.querySelectorAll === "function") {
          try {
            const inside = Array.from(parentNext.querySelectorAll("input, textarea, select")).filter(isSafeInputField);
            if (inside.length > 0) return inside[0];
          } catch (e) { }
        }
        const pTag = (parentNext.tagName || "").toLowerCase();
        if (pTag === "th" || (parentNext.className && /labeled/.test(parentNext.className))) {
          break;
        }
        parentNext = parentNext.nextElementSibling;
      }
    }

    // Strategy 5: Sibling cells inside a table row (<tr><td>Label</td><td><input></td></tr>)
    try {
      let tr = null;
      if (typeof labelEl.closest === "function") {
        tr = labelEl.closest("tr");
      }
      if (!tr && parent && (parent.tagName || "").toUpperCase() === "TR") {
        tr = parent;
      }
      if (!tr && parent && parent.parentElement && (parent.parentElement.tagName || "").toUpperCase() === "TR") {
        tr = parent.parentElement;
      }
      if (tr && typeof tr.querySelectorAll === "function") {
        const trInputs = Array.from(tr.querySelectorAll("input, textarea, select")).filter(isSafeInputField);
        if (trInputs.length === 1) return trInputs[0];
        if (trInputs.length > 1) {
          // If labelEl is inside a td, find inputs in following sibling td cells
          let td = (labelEl.tagName || "").toUpperCase() === "TD" ? labelEl : (labelEl.closest ? labelEl.closest("td") : parent);
          if (td && td.nextElementSibling) {
            let nextTd = td.nextElementSibling;
            while (nextTd) {
              const tdInputs = Array.from(nextTd.querySelectorAll("input, textarea, select")).filter(isSafeInputField);
              if (tdInputs.length > 0) return tdInputs[0];
              nextTd = nextTd.nextElementSibling;
            }
          }
          return trInputs[0];
        }
      }
    } catch (e) { }

    // Strategy 6: Form group or row container with single safe input
    try {
      let container = null;
      if (typeof labelEl.closest === "function") {
        container = labelEl.closest(".form-group, .row, fieldset");
      }
      if (!container && parent && parent.className && /form-group|row/.test(parent.className)) {
        container = parent;
      }
      if (container && typeof container.querySelectorAll === "function") {
        const inputs = Array.from(container.querySelectorAll("input, textarea, select")).filter(isSafeInputField);
        if (inputs.length === 1) return inputs[0];
        const textInputs = inputs.filter((i) => (i.tagName || "").toUpperCase() === "INPUT");
        if (textInputs.length === 1) return textInputs[0];
      }
    } catch (e) { }

    return null;
  }

  /**
   * Find form inputs by visible human label names.
   * Allows autofill to discover targets even when input IDs/names are dynamic,
   * unbranded, or Struts-generated tokens.
   */
  function findInputsByLabels(doc) {
    if (!doc) return [];
    let labelNodes = [];
    try {
      if (typeof doc.querySelectorAll === "function") {
        labelNodes = Array.from(doc.querySelectorAll("label, .labeled, .control-label, th, td"));
      }
    } catch (e) {
      labelNodes = [];
    }

    const results = [];
    const claimedInputs = new Set();
    const matchedKeys = new Set();

    for (const patternDef of LABEL_FIELD_PATTERNS) {
      if (matchedKeys.has(patternDef.key)) continue;

      for (const node of labelNodes) {
        if (!node) continue;
        const tag = (node.tagName || "").toUpperCase();
        if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") continue;

        const text = (node.textContent || "").trim();
        if (!text || text.length > 100) continue;

        if (patternDef.negative && patternDef.negative.test(text)) continue;

        if (patternDef.pattern.test(text)) {
          const input = resolveInputFromLabel(node, doc);
          if (input && !claimedInputs.has(input) && isSafeInputField(input)) {
            claimedInputs.add(input);
            matchedKeys.add(patternDef.key);
            let targetType = null;
            let label = patternDef.label;
            if (patternDef.key === "licence_no") {
              targetType = getLicenceTargetType(input.id ? `#${input.id}` : (input.name ? `input[name='${input.name}']` : "input"), input, doc, text);
              label = targetType === "dl" ? "Driving Licence Number" : (targetType === "generic" ? "Licence Number" : "Learner's Licence Number");
            }
            results.push({
              key: patternDef.key,
              label: label,
              selector: input.id ? `#${input.id}` : (input.name ? `input[name='${input.name}']` : "input"),
              id: input.id || input.name || "",
              element: input,
              isDob: Boolean(patternDef.isDob),
              matchedBy: "label_association",
              labelText: text,
              targetType: targetType,
            });
            break;
          }
        }
      }
    }

    return results;
  }

  /**
   * Intelligent field classifier: Uses regex and semantic heuristics to identify what
   * FormFlow field key a visible, editable form control corresponds to.
   */
  function classifyInputElement(el, doc) {
    if (!isSafeInputField(el)) return null;

    const id = (el.id || "").toLowerCase();
    const name = (el.name || "").toLowerCase();
    const normId = normalizeIdentifier(el.id);
    const normName = normalizeIdentifier(el.name);
    const placeholder = (el.placeholder || (el.getAttribute && el.getAttribute("placeholder")) || "").toLowerCase();
    const title = (el.title || (el.getAttribute && el.getAttribute("title")) || "").toLowerCase();
    const context = getElementContextText(el, doc);

    // Strictly exclude CAPTCHA, OTP, and security tokens
    if (
      context.includes("captcha") ||
      context.includes("entcaptxt") ||
      context.includes("capimg") ||
      id.includes("captcha") ||
      name.includes("captcha")
    ) {
      return null;
    }

    // 1. DATE OF BIRTH (dob)
    const isDobCanonical =
      CANONICAL_FIELD_IDENTIFIERS.dob.has(normId) ||
      CANONICAL_FIELD_IDENTIFIERS.dob.has(normName);

    const isDobIdOrName =
      /^(dob|dateofbirth|txtdateofbirth|txtdob|appldob|appldateofbirth|birthdate|date_of_birth|birth_date|applicantdob|persondob|candidatedob|holderdob|applicantbirthdate|txtbirthdate|birthdateval|dobval)$/i.test(id) ||
      /^(dob|dateofbirth|txtdateofbirth|txtdob|appldob|appldateofbirth|birthdate|date_of_birth|birth_date|applicantdob|persondob|candidatedob|holderdob|applicantbirthdate|txtbirthdate|birthdateval|dobval)$/i.test(name) ||
      /(?:date[\s._-]*of[\s._-]*birth|birth[\s._-]*date|\bdob\b|appldob|persondob)/i.test(id) ||
      /(?:date[\s._-]*of[\s._-]*birth|birth[\s._-]*date|\bdob\b|appldob|persondob)/i.test(name);

    const isDobPlaceholderOrTitle =
      /(?:date[\s._-]*of[\s._-]*birth|birth[\s._-]*date|\bdob\b|dd[\/-]mm[\/-]yyyy)/i.test(placeholder + " " + title);

    const isDobContext =
      /(?:date\s*of\s*birth|birth\s*date|\bdob\b|dd[\/-]mm[\/-]yyyy)/i.test(context);

    if (isDobCanonical || isDobIdOrName || isDobPlaceholderOrTitle || isDobContext) {
      // Exclude parent/guardian DOB if applicant DOB is sought
      if (
        !/(?:parent|father|mother|guardian)[\s._-]*dob/i.test(id) &&
        !/(?:parent|father|mother|guardian)[\s._-]*dob/i.test(name)
      ) {
        return "dob";
      }
    }

    // 2. APPLICATION NUMBER (application_no)
    // Only exclude if the control itself or direct placeholder is an unrelated number
    const isExcludedForAppl =
      /(?:mobile|phone|otp|aadhaar|uid|amount|vehicle|chassis|engine)/i.test(id) ||
      /(?:mobile|phone|otp|aadhaar|uid|amount|vehicle|chassis|engine)/i.test(name) ||
      /^(?:mobile|phone|enter\s*otp|aadhaar|amount\s*to\s*pay)$/i.test(placeholder) ||
      /^(?:mobile|phone|enter\s*otp|aadhaar|amount\s*to\s*pay)$/i.test(title);

    if (!isExcludedForAppl) {
      const isApplCanonical =
        CANONICAL_FIELD_IDENTIFIERS.application_no.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.application_no.has(normName);

      const isApplIdOrName =
        /^(appl(ication)?[\s._-]*(num(ber)?|no|id)?|app[\s._-]*(num(ber)?|no|id)?|appln[\s._-]*(num(ber)?|no|id)?|reg(no|num(ber)?|istrationno)?|transid|applcode|fileno)$/i.test(id) ||
        /^(appl(ication)?[\s._-]*(num(ber)?|no|id)?|app[\s._-]*(num(ber)?|no|id)?|appln[\s._-]*(num(ber)?|no|id)?|reg(no|num(ber)?|istrationno)?|transid|applcode|fileno)$/i.test(name) ||
        /^(txtappl(ication)?(num|no|number)?|txtapp(num|no|number)?|txtappln(num|no|number)?)$/i.test(id) ||
        /^(txtappl(ication)?(num|no|number)?|txtapp(num|no|number)?|txtappln(num|no|number)?)$/i.test(name) ||
        /(?:appl(ication)?[\s._-]*(?:num(ber)?|no)|app[\s._-]*(?:num(ber)?|no)|appln[\s._-]*(?:num(ber)?|no)|\breg[\s._-]*no\b)/i.test(id) ||
        /(?:appl(ication)?[\s._-]*(?:num(ber)?|no)|app[\s._-]*(?:num(ber)?|no)|appln[\s._-]*(?:num(ber)?|no)|\breg[\s._-]*no\b)/i.test(name);

      const isApplPlaceholderOrTitle =
        /(?:application\s*(?:number|no|num|id)?|appl\s*(?:number|no|num|id)|appln\s*(?:number|no|num|id)|enter\s*application)/i.test(placeholder + " " + title);

      const isApplContext =
        /(?:application\s*(?:number|no|num|id)?|appl\s*(?:number|no|num|id)|appln\s*(?:number|no|num|id)|enter\s*application\s*(?:number|no))/i.test(context);

      if (isApplCanonical || isApplIdOrName || isApplPlaceholderOrTitle || isApplContext) {
        return "application_no";
      }
    }

    // 3. LICENCE NUMBER (licence_no - Learner's Licence or Driving Licence)
    const isExcludedForLicence = /(?:mobile|phone|otp|aadhaar|application)/i.test(id + " " + name);
    if (!isExcludedForLicence) {
      const isLicenceCanonical =
        CANONICAL_FIELD_IDENTIFIERS.licence_no.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.licence_no.has(normName);

      const isLicenceIdOrName =
        /^(learninglicen[cs]e|licen[cs]eno|licen[cs]enumber|llno|llnumber|txtllno|txtllnumber|dlno|dlnumber|txtdlno|txtdlnumber|drivinglicen[cs]eno|licen[cs]e_no|ll_no|dl_no|learners?licen[cs]e|licno|licnumber|licnum|txtlicno|txtlicnum|txtlicnumber|dlnum|llnum|curlicno|curlicnumber|oldlicenceno|newlicenceno)$/i.test(id) ||
        /^(learninglicen[cs]e|licen[cs]eno|licen[cs]enumber|llno|llnumber|txtllno|txtllnumber|dlno|dlnumber|txtdlno|txtdlnumber|drivinglicen[cs]eno|licen[cs]e_no|ll_no|dl_no|learners?licen[cs]e|licno|licnumber|licnum|txtlicno|txtlicnum|txtlicnumber|dlnum|llnum|curlicno|curlicnumber|oldlicenceno|newlicenceno)$/i.test(name) ||
        /(?:learning[\s._-]*licen[cs]e|learner['’]?s?[\s._-]*licen[cs]e|\bll[\s._-]*no\b|\bdl[\s._-]*no\b|\blic[\s._-]*no\b|\blic[\s._-]*num\b|driving[\s._-]*licen[cs]e)/i.test(id + " " + name);

      const isLicenceContext =
        /(?:learner['’]?s?\s*licen[cs]e|learning\s*licen[cs]e|\bll\s*no\b|\bdl\s*no\b|\blic\s*no\b|\blic\s*number\b|\blic\s*num\b|driving\s*licen[cs]e|licen[cs]e\s*no|licen[cs]e\s*num)/i.test(context);

      if (isLicenceCanonical || isLicenceIdOrName || isLicenceContext) {
        return "licence_no";
      }
    }

    return null;
  }

  /**
   * Flexible page inspector: detects which Sarathi form fields are present in the DOM.
   * Uses two-tier discovery:
   * 1. Section-aware explicit selector targeting
   * 2. Intelligent semantic/regex heuristics across all visible form inputs
   */
  function inspectPageForAutofill(doc, loc) {
    if (!validatePortalLocation(loc)) {
      return {
        ready: false,
        pageType: "unknown",
        pageName: "Non-Sarathi Page",
        detectedFields: [],
        reason: "Active tab is not on the Sarathi portal (https://sarathi.parivahan.gov.in).",
      };
    }

    if (!doc) {
      return {
        ready: false,
        pageType: "unknown",
        pageName: "Unknown",
        detectedFields: [],
        reason: "No document context available.",
      };
    }

    let pageType = "general";
    let pageName = "Sarathi Portal Page";

    const path = ((loc && loc.pathname) || "").toLowerCase();
    const isAppStatusSection =
      doc.getElementById("publicApplStatus") ||
      doc.getElementById("applViewStages") ||
      path.includes("applviewstatus");
    const isDlSection =
      doc.getElementById("lldetailsDisplay") ||
      path.includes("displaydlpage");

    if (isAppStatusSection) {
      pageType = "app_status";
      pageName = "Application Status";
    } else if (isDlSection) {
      pageType = "dl_entry";
      pageName = "DL Entry (Learner's Licence)";
    }

    const detectedFields = [];
    const seenKeys = new Set();
    const matchedElements = new Set();

    // TIER 1: Targeted Selector Matching (with section scoping when available)
    for (const def of TARGET_FIELD_DEFINITIONS) {
      for (const selector of def.selectors) {
        if (seenKeys.has(def.key)) break;
        let candidates = [];
        try {
          candidates = Array.from(doc.querySelectorAll(selector));
        } catch (e) {
          candidates = [];
        }

        // Prioritize section scoped elements if in a known section
        if (pageType === "app_status") {
          const section = doc.getElementById("publicApplStatus") || doc.getElementById("applViewStages");
          if (section) {
            const sectionCandidates = candidates.filter((el) =>
              typeof section.contains === "function" ? section.contains(el) : true
            );
            if (sectionCandidates.length > 0) candidates = sectionCandidates;
          }
        } else if (pageType === "dl_entry") {
          const section = doc.getElementById("lldetailsDisplay");
          if (section) {
            const sectionCandidates = candidates.filter((el) =>
              typeof section.contains === "function" ? section.contains(el) : true
            );
            if (sectionCandidates.length > 0) candidates = sectionCandidates;
          }
        }

        const validCandidates = candidates
          .filter(isSafeInputField)
          .filter((el) => !matchedElements.has(el));

        if (validCandidates.length > 0) {
          const chosen = validCandidates[0];
          seenKeys.add(def.key);
          matchedElements.add(chosen);
          let targetType = null;
          let label = def.label;
          if (def.key === "licence_no") {
            targetType = getLicenceTargetType(selector, chosen, doc, "");
            label = targetType === "dl" ? "Driving Licence Number" : (targetType === "generic" ? "Licence Number" : "Learner's Licence Number");
          }
          detectedFields.push({
            key: def.key,
            label: label,
            selector: selector,
            id: chosen.id || chosen.name || selector,
            element: chosen,
            isDob: Boolean(def.isDob),
            targetType: targetType,
          });
          break;
        }
      }
    }

    // TIER 2: Label-to-Input Association Engine
    // If essential fields are not yet discovered, resolve via visible human labels in DOM
    if (!seenKeys.has("application_no") || !seenKeys.has("dob") || !seenKeys.has("licence_no")) {
      const labelMatches = findInputsByLabels(doc);
      for (const match of labelMatches) {
        if (!seenKeys.has(match.key) && !matchedElements.has(match.element)) {
          seenKeys.add(match.key);
          matchedElements.add(match.element);
          detectedFields.push(match);
        }
      }
    }

    // TIER 3: Intelligent Heuristic Scanner
    // If essential fields are still not discovered, scan all safe inputs in the DOM
    if (!seenKeys.has("application_no") || !seenKeys.has("dob") || !seenKeys.has("licence_no")) {
      let allInputs = [];
      try {
        allInputs = Array.from(doc.querySelectorAll("input, textarea, select"));
      } catch (e) {
        allInputs = [];
      }

      for (const input of allInputs) {
        if (matchedElements.has(input)) continue;
        if (!isSafeInputField(input)) continue;

        const detectedKey = classifyInputElement(input, doc);
        if (detectedKey && !seenKeys.has(detectedKey)) {
          const def = TARGET_FIELD_DEFINITIONS.find((d) => d.key === detectedKey);
          let label = def ? def.label : (detectedKey === "dob" ? "Date of Birth" : "Application Number");
          const isDob = def ? Boolean(def.isDob) : detectedKey === "dob";
          let targetType = null;
          if (detectedKey === "licence_no") {
            targetType = getLicenceTargetType(input.id ? `#${input.id}` : (input.name ? `input[name='${input.name}']` : "input"), input, doc, "");
            label = targetType === "dl" ? "Driving Licence Number" : (targetType === "generic" ? "Licence Number" : "Learner's Licence Number");
          }

          seenKeys.add(detectedKey);
          matchedElements.add(input);
          detectedFields.push({
            key: detectedKey,
            label: label,
            selector: input.id ? `#${input.id}` : (input.name ? `input[name='${input.name}']` : "input"),
            id: input.id || input.name || "",
            element: input,
            isDob: isDob,
            targetType: targetType,
          });
        }
      }
    }

    return {
      ready: detectedFields.length > 0,
      pageType,
      pageName,
      detectedFields,
      reason: detectedFields.length > 0 ? null : "No matching Sarathi form fields detected in page DOM.",
    };
  }

  /**
   * Flexible autofill execution: matches confirmed values against detected DOM inputs,
   * dispatches input & change events, and confirms with read-back verification.
   */
  function autofillMatchedFields(doc, loc, params) {
    if (!params || typeof params !== "object") {
      return { ok: false, partial: false, error: "Fill command payload must be an object." };
    }
    const caseId = typeof params.caseId === "string" ? params.caseId.trim() : "";
    if (!caseId) {
      return { ok: false, partial: false, error: "Invalid command: non-empty caseId is required for traceability." };
    }
    const reviewVersion = params.reviewVersion;
    if (typeof reviewVersion !== "number" || !Number.isInteger(reviewVersion) || reviewVersion <= 0) {
      return { ok: false, partial: false, error: "Invalid command: positive integer reviewVersion is required." };
    }

    const inspection = inspectPageForAutofill(doc, loc);
    if (!inspection.ready) {
      return { ok: false, partial: false, error: inspection.reason || "No matching DOM fields found to autofill." };
    }

    const fieldValues = params.fieldValues || params.fields || {};
    const filled = [];
    const errors = [];

    for (const detected of inspection.detectedFields) {
      let rawVal = fieldValues[detected.key];
      // Support alternate key alias if licence_no vs learningLicence or appl_number vs application_no
      if (rawVal === undefined && detected.key === "application_no") {
        rawVal = fieldValues["appl_number"] || params.applicationNo;
      }
      if (rawVal === undefined && detected.key === "licence_no") {
        rawVal = fieldValues["licence_number"] || params.licenceNo;
      }
      if (rawVal === undefined && detected.key === "dob") {
        rawVal = params.dob;
      }

      if (rawVal === undefined || rawVal === null) {
        continue;
      }

      const strVal = String(rawVal).trim();
      if (!strVal) {
        continue;
      }

      // Check DL vs LL licence semantics (Phase 7C)
      if (detected.key === "licence_no") {
        const targetType = detected.targetType || getLicenceTargetType(detected.selector, detected.element, doc, detected.labelText);
        const sourceType = fieldValues.licence_type || fieldValues.licence_no_type || params.licenceType;

        if (sourceType === "dl" && targetType === "ll") {
          errors.push({
            key: detected.key,
            label: detected.label,
            error: "Cannot fill Driving Licence number into a Learner's Licence field.",
          });
          continue;
        }

        if (sourceType === "ll" && targetType === "dl") {
          errors.push({
            key: detected.key,
            label: detected.label,
            error: "Cannot fill Learner's Licence number into a Driving Licence field.",
          });
          continue;
        }

        if (sourceType === "unknown" && (targetType === "ll" || targetType === "dl")) {
          const typeName = targetType === "ll" ? "Learner's Licence" : "Driving Licence";
          errors.push({
            key: detected.key,
            label: detected.label,
            error: `Cannot fill licence number with unknown source type into a ${typeName} field.`,
          });
          continue;
        }
      }

      let fillVal = strVal;
      if (detected.isDob) {
        try {
          fillVal = formatDobForPortal(strVal);
        } catch (err) {
          errors.push({
            key: detected.key,
            label: detected.label,
            error: "Date conversion error: " + err.message,
          });
          continue;
        }
      }

      // Find the input element (prefer direct safe element from inspection, fallback to id/selector)
      let el = null;
      try {
        if (detected.element && isSafeInputField(detected.element)) {
          el = detected.element;
        } else if (detected.id && doc.getElementById(detected.id) && isSafeInputField(doc.getElementById(detected.id))) {
          el = doc.getElementById(detected.id);
        } else if (detected.selector) {
          const cands = Array.from(doc.querySelectorAll(detected.selector)).filter(isSafeInputField);
          if (cands.length > 0) {
            el = cands[0];
          }
        }
      } catch (e) {
        el = null;
      }

      if (!el || !isSafeInputField(el)) {
        errors.push({
          key: detected.key,
          label: detected.label,
          error: `DOM element '${detected.selector}' became unavailable or unsafe.`,
        });
        continue;
      }

      // Apply value & dispatch lifecycle events (focus -> input -> change -> blur)
      try {
        if (typeof el.focus === "function") el.focus();
      } catch (e) { }
      el.value = fillVal;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      try {
        if (typeof el.blur === "function") el.blur();
      } catch (e) { }

      // Read back verification
      const readBack = (el.value || "").trim();
      if (readBack === fillVal) {
        filled.push({
          key: detected.key,
          label: detected.label,
          selector: detected.selector,
          verified: true,
        });
      } else {
        errors.push({
          key: detected.key,
          label: detected.label,
          error: "Read-back verification failed after fill.",
        });
      }
    }

    if (filled.length === 0 && errors.length === 0) {
      return {
        ok: false,
        partial: false,
        filled: [],
        errors: [],
        error: "None of the detected DOM fields had confirmed values in the case packet.",
      };
    }

    const allOk = errors.length === 0 && filled.length > 0;
    const isPartial = errors.length > 0 && filled.length > 0;

    return {
      ok: allOk,
      partial: isPartial,
      pageType: inspection.pageType,
      filled,
      errors,
      message: allOk
        ? `Successfully filled ${filled.length} matched field(s) (${filled.map((f) => f.label).join(", ")}). Complete CAPTCHA manually.`
        : isPartial
          ? `Partially filled ${filled.length} field(s); ${errors.length} failed verification.`
          : `Autofill failed: ${errors.map((e) => e.label + ' (' + e.error + ')').join("; ")}`,
    };
  }

  // Export
  const adapter = {
    validatePortalLocation,
    formatDobForPortal,
    inspectDlPage,
    fillDlFields,
    validateFillCommand,
    inspectAppStatusPage,
    fillAppStatusFields,
    validateAppStatusCommand,
    inspectPageForAutofill,
    autofillMatchedFields,
    isSafeInputField,
    classifyInputElement,
    getElementContextText,
    normalizeIdentifier,
    CANONICAL_FIELD_IDENTIFIERS,
    TARGET_FIELD_DEFINITIONS,
    LABEL_FIELD_PATTERNS,
    resolveInputFromLabel,
    findInputsByLabels,
    getLicenceTargetType,
  };

  const root = typeof window !== "undefined" ? window : globalThis;
  root.FormFlowPortalAdapter = adapter;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = adapter;
  }
})();
