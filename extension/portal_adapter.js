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
   * Deterministically splits a full name string into first, middle, and last names.
   * Handles single-token, two-token, three-token, and multi-word names without guessing.
   */
  function splitFullName(nameStr) {
    if (!nameStr || typeof nameStr !== "string") {
      return { firstName: "", middleName: "", lastName: "" };
    }
    const tokens = nameStr.trim().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) {
      return { firstName: "", middleName: "", lastName: "" };
    }
    if (tokens.length === 1) {
      return { firstName: tokens[0], middleName: "", lastName: "" };
    }
    if (tokens.length === 2) {
      return { firstName: tokens[0], middleName: "", lastName: tokens[1] };
    }
    if (tokens.length === 3) {
      return { firstName: tokens[0], middleName: tokens[1], lastName: tokens[2] };
    }
    // 4+ words: first is tokens[0], middle is tokens[1..n-2], last is tokens[n-1]
    return {
      firstName: tokens[0],
      middleName: tokens.slice(1, -1).join(" "),
      lastName: tokens[tokens.length - 1],
    };
  }

  const INDIAN_STATES_LIST = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
    "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Delhi", "Jammu and Kashmir", "Ladakh", "Puducherry", "Chandigarh"
  ];

  /**
   * Deterministically parses a postal address into structured components:
   * house/door/flat number, street/locality, location/sub-district, and 6-digit PIN code.
   * Uses hierarchical reverse-decomposition tailored for Indian ID cards (Aadhaar, DL, Voter ID).
   */
  function splitAddress(addressStr) {
    if (!addressStr || typeof addressStr !== "string") {
      return { houseNo: "", street: "", location: "", mandal: "", district: "", state: "", pincode: "" };
    }

    let raw = addressStr.trim();
    let pincode = "";
    let state = "";
    let district = "";
    let mandal = "";
    let houseNo = "";

    // 1. Extract 6-digit PIN code if present
    const pinMatch = /\b([1-9]\d{5})\b/.exec(raw);
    if (pinMatch) {
      pincode = pinMatch[1];
      raw = raw.replace(/\b[1-9]\d{5}\b/, "").replace(/pin\s*(?:code)?\s*[:.-]?\s*/i, "").trim();
    }

    // 2. State
    for (const st of INDIAN_STATES_LIST) {
      const re = new RegExp("\\b" + st.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b", "i");
      if (re.test(raw)) {
        state = st;
        raw = raw.replace(re, "").trim();
        break;
      }
    }
    if (!state) {
      const codeMatch = /\b(TG|TS|AP|MH|KA|TN|KL|DL|GJ|RJ|UP|MP|WB)\b/i.exec(raw);
      if (codeMatch) {
        const cMap = { TG: "Telangana", TS: "Telangana", AP: "Andhra Pradesh", MH: "Maharashtra", KA: "Karnataka", TN: "Tamil Nadu" };
        state = cMap[codeMatch[1].toUpperCase()] || codeMatch[1].toUpperCase();
        raw = raw.replace(new RegExp("\\b" + codeMatch[1] + "\\b", "i"), "").trim();
      }
    }

    // 3. Strip Care-of / Guardian relation e.g. "S/O: Katta Somi Reddy"
    raw = raw.replace(/\b(?:s\/o|c\/o|d\/o|w\/o)\s*[:\-]?\s*[A-Za-z\s]+?(?=,\s*|\d|address|dist|po|pin|near|\n|$)/gi, "").trim();

    // 4. District: tagged e.g. "DIST: Nalgonda"
    const distMatch = /\b(?:dist(?:rict)?)\s*[:\-]?\s*([A-Za-z\s]+?)(?=,\s*|\d|po|pin|state|\n|$)/i.exec(raw);
    if (distMatch) {
      district = distMatch[1].trim();
      raw = raw.slice(0, distMatch.index) + raw.slice(distMatch.index + distMatch[0].length);
    }

    // 5. Mandal / Taluk: tagged e.g. "Mandal: ..."
    const mdlMatch = /\b(?:mandal|mdl|taluk|tehsil|tahsil)\s*[:\-]?\s*([A-Za-z\s]+?)(?=,\s*|\d|dist|po|pin|\n|$)/i.exec(raw);
    if (mdlMatch) {
      mandal = mdlMatch[1].trim();
      raw = raw.slice(0, mdlMatch.index) + raw.slice(mdlMatch.index + mdlMatch[0].length);
    }

    // 6. House / Door / Flat No
    const hMatch = /\b(?:h\.?\s*no\.?|d\.?\s*no\.?|door\s*no\.?|flat\s*no\.?|plot\s*no\.?|qtr\s*no\.?|house\s*no\.?)[\s.:#\-]*([0-9A-Za-z\-/]+)/i.exec(raw);
    if (hMatch) {
      houseNo = hMatch[0].trim();
      raw = raw.slice(0, hMatch.index) + raw.slice(hMatch.index + hMatch[0].length);
    } else {
      // Unprefixed door pattern: 30-95, 4-12/1 (not 19xx/20xx year)
      const doorMatch = /\b(\d{1,4}[A-Za-z]?(?:\s*[-/]\s*\d{1,4}[A-Za-z]?)+)\b/.exec(raw);
      if (doorMatch && !/\b(19\d\d|20\d\d)\b/.test(doorMatch[1])) {
        houseNo = doorMatch[1].replace(/\s+/g, "");
        raw = raw.slice(0, doorMatch.index) + raw.slice(doorMatch.index + doorMatch[0].length);
      }
    }

    // Remove noise labels like "Address:", "Det on:"
    raw = raw.replace(/\baddress\s*[:\-]?/gi, "").replace(/\bdet\s*on\s*[:\-]?/gi, "");

    const _NOISE_WORDS = new Set([
      "address", "det on", "dist", "district", "state", "pin", "pincode",
      "mandal", "mdl", "taluk", "tehsil", "po", "p.o", "post office",
    ]);

    const tokens = raw.split(/[\r\n,]+/)
      .map((p) => p.replace(/^[\s,;:-]+|[\s,;:-]+$/g, "").trim())
      .filter((p) => Boolean(p) && !_NOISE_WORDS.has(p.toLowerCase()));

    const landmarks = [];
    const streets = [];
    const locations = [];

    for (const t of tokens) {
      if (/\b(?:po|p\.o|post\s*office)\b/i.test(t)) {
        locations.push(t);
      } else if (/\b(?:near|opp|behind|beside)\b/i.test(t)) {
        landmarks.push(t);
      } else if (/\b(?:colony|nagar|street|road|lane|gudem|wada|enclave|layout|bazaar|cross|gali)\b/i.test(t)) {
        streets.push(t);
      } else if (!district && tokens.length >= 2 && t === tokens[tokens.length - 1]) {
        district = t;
      } else if (!mandal && tokens.length >= 3 && t === tokens[tokens.length - 2]) {
        mandal = t;
        locations.push(t);
      } else {
        if (streets.length === 0) {
          streets.push(t);
        } else {
          locations.push(t);
        }
      }
    }

    // If houseNo was not detected by pattern, fallback to first token if not already in street
    if (!houseNo && tokens.length > 0 && streets[0] === tokens[0]) {
      houseNo = tokens[0];
      streets.shift();
    }

    const streetCombined = Array.from(new Set(streets)).join(", ").trim();
    let locationCombined = Array.from(new Set([...landmarks, ...locations, mandal, district].filter(Boolean))).join(", ").trim();

    return {
      houseNo: houseNo.trim(),
      street: streetCombined,
      location: locationCombined,
      mandal: mandal.trim(),
      district: district.trim(),
      state: state.trim(),
      pincode: pincode.trim(),
    };
  }

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

  function findUniqueVisibleInput(section, selector, label, sectionSelector) {
    const inputs = section.querySelectorAll(selector);
    if (inputs.length === 0) {
      return { ok: false, reason: `${label} input (${selector}) not found inside ${sectionSelector}.` };
    }
    if (inputs.length > 1) {
      return { ok: false, reason: `Ambiguous DOM: found ${inputs.length} ${selector} inputs in section.` };
    }
    const input = inputs[0];
    if (input.disabled || input.readOnly) {
      return { ok: false, reason: `${label} input is disabled or read-only.` };
    }
    if (!isElementVisible(input)) {
      return { ok: false, reason: `${label} input is hidden.` };
    }
    return { ok: true, input };
  }

  function validateTwoFieldCommand(params, primaryKey, primaryLabel) {
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
    const primaryVal = typeof params[primaryKey] === "string" ? params[primaryKey].trim() : "";
    if (!primaryVal) {
      return { valid: false, error: `Invalid command: non-empty ${primaryLabel} is required.` };
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

  function setInputValueAndDispatch(input, value) {
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return (input.value || "").trim() === value;
  }

  function fillTwoFieldForm({ inspectFn, validateFn, primaryKey, primaryLabel, inputKey, missingPrimaryMsg }, doc, loc, params) {
    if (params && (params.caseId !== undefined || params.reviewVersion !== undefined)) {
      const cmdCheck = validateFn(params);
      if (!cmdCheck.valid) {
        return { ok: false, partial: false, error: cmdCheck.error };
      }
    }
    const inspection = inspectFn(doc, loc);
    if (!inspection.ready) {
      return { ok: false, partial: false, error: inspection.reason };
    }
    const primaryInput = inspection[inputKey];
    const dobInput = inspection.dobInput;
    const primaryVal = (params && params[primaryKey] ? String(params[primaryKey]) : "").trim();
    const dobRaw = params && params.dob ? String(params.dob) : "";
    if (!primaryVal) {
      return { ok: false, partial: false, error: missingPrimaryMsg };
    }
    let formattedDob = "";
    try {
      formattedDob = formatDobForPortal(dobRaw);
    } catch (err) {
      return { ok: false, partial: false, error: "Date of Birth conversion error: " + err.message };
    }

    const primaryMatched = setInputValueAndDispatch(primaryInput, primaryVal);
    const dobMatched = setInputValueAndDispatch(dobInput, formattedDob);
    const filled = { [primaryKey]: primaryMatched, dob: dobMatched };

    if (primaryMatched && dobMatched) {
      return {
        ok: true,
        partial: false,
        filled,
        message: `Filled ${primaryLabel} and DOB successfully. Complete CAPTCHA manually.`,
      };
    }
    if (primaryMatched && !dobMatched) {
      return {
        ok: false,
        partial: true,
        filled,
        error: `${primaryLabel} was filled, but Date of Birth verification failed.`,
      };
    }
    if (!primaryMatched && dobMatched) {
      return {
        ok: false,
        partial: true,
        filled,
        error: `Date of Birth was filled, but ${primaryLabel} verification failed.`,
      };
    }
    return {
      ok: false,
      partial: false,
      filled,
      error: "Both fields failed verification after fill attempt.",
    };
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

    const llHolder1 = doc.getElementById("llHolder1");
    const allLlHolders = doc.querySelectorAll('input[type="radio"][name="llHolder"]');
    if (allLlHolders && allLlHolders.length > 0) {
      const checkedHolder = doc.querySelector('input[type="radio"][name="llHolder"]:checked');
      if (!checkedHolder) {
        return { ready: false, reason: "No choice selected under 'Select appropriate choice' (Holding Learners Licence)." };
      }
      if (checkedHolder.value !== "1" && checkedHolder.id !== "llHolder1") {
        return { ready: false, reason: "Current mode is not 'Holding Learners Licence' (different choice selected)." };
      }
    } else if (llHolder1 && !llHolder1.checked) {
      return { ready: false, reason: "'Holding Learners Licence' radio option is not checked." };
    }

    const section = doc.getElementById("lldetailsDisplay");
    if (!section) {
      return { ready: false, reason: "Learner's Licence details section (#lldetailsDisplay) was not found in page DOM." };
    }
    if (!isElementVisible(section)) {
      return { ready: false, reason: "Learner's Licence details section (#lldetailsDisplay) is currently hidden." };
    }

    const llCheck = findUniqueVisibleInput(section, "#learningLicence", "Learner's Licence", "#lldetailsDisplay");
    if (!llCheck.ok) return { ready: false, reason: llCheck.reason };
    const dobCheck = findUniqueVisibleInput(section, "#DOB", "Date of Birth", "#lldetailsDisplay");
    if (!dobCheck.ok) return { ready: false, reason: dobCheck.reason };

    return { ready: true, reason: null, section, llInput: llCheck.input, dobInput: dobCheck.input };
  }

  function validateFillCommand(params) {
    return validateTwoFieldCommand(params, "licenceNo", "Learner's Licence Number");
  }

  function fillDlFields(doc, loc, params) {
    return fillTwoFieldForm({
      inspectFn: inspectDlPage,
      validateFn: validateFillCommand,
      primaryKey: "licenceNo",
      primaryLabel: "Learner's Licence Number",
      inputKey: "llInput",
      missingPrimaryMsg: "Learner's Licence number is missing or empty.",
    }, doc, loc, params);
  }

  /**
   * Inspect document and location for Application Status prerequisites
   * (/sarathiservice/applViewStatus.do -> #publicApplStatus, #applNum, #dateOfBirth).
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

    const section = doc.getElementById("publicApplStatus") || doc.getElementById("applViewStages");
    if (!section) {
      return { ready: false, reason: "Application Status section (#publicApplStatus) was not found in page DOM." };
    }
    if (!isElementVisible(section)) {
      return { ready: false, reason: "Application Status section (#publicApplStatus) is currently hidden." };
    }

    const applCheck = findUniqueVisibleInput(section, "#applNum", "Application Number", "#publicApplStatus");
    if (!applCheck.ok) return { ready: false, reason: applCheck.reason };
    const dobCheck = findUniqueVisibleInput(section, "#dateOfBirth", "Date of Birth", "#publicApplStatus");
    if (!dobCheck.ok) return { ready: false, reason: dobCheck.reason };

    return { ready: true, reason: null, section, applNumInput: applCheck.input, dobInput: dobCheck.input };
  }

  function validateAppStatusCommand(params) {
    return validateTwoFieldCommand(params, "applicationNo", "Application Number");
  }

  function fillAppStatusFields(doc, loc, params) {
    return fillTwoFieldForm({
      inspectFn: inspectAppStatusPage,
      validateFn: validateAppStatusCommand,
      primaryKey: "applicationNo",
      primaryLabel: "Application Number",
      inputKey: "applNumInput",
      missingPrimaryMsg: "Application Number is missing or empty.",
    }, doc, loc, params);
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
    first_name: new Set([
      "fname", "applfname", "applicantfname", "txtfname", "applicantfirstname",
      "firstname", "first_name", "txtfirstname", "candidatename",
    ]),
    middle_name: new Set([
      "mname", "applmname", "applicantmname", "txtmname", "applicantmiddlename",
      "middlename", "middle_name", "txtmiddlename",
    ]),
    last_name: new Set([
      "lname", "appllname", "applicantlname", "txtlname", "applicantlastname",
      "lastname", "last_name", "surname", "txtlastname",
    ]),
    full_name: new Set([
      "newfullname", "fullname", "applfullname", "applicantname", "applname",
      "nameasperrecords", "holdername", "displayname",
    ]),
    father_first_name: new Set([
      "swdfname", "swd_fname", "rfname", "fatherfname", "relfname",
      "guardianfirstname", "fatherfirstname",
    ]),
    father_middle_name: new Set([
      "swdmname", "swd_mname", "rmname", "fathermname", "relmname",
      "guardianmiddlename", "fathermiddlename",
    ]),
    father_last_name: new Set([
      "swdlname", "swd_lname", "rlname", "fatherlname", "rellname",
      "guardianlastname", "fatherlastname",
    ]),
    father_name: new Set([
      "fathername", "swdname", "guardianname", "relationname", "relname",
      "fathersname", "husbandname",
    ]),
    address_house_no: new Set([
      "preshouseno", "houseno", "presdoorno", "doorno", "flatno", "presflatno",
      "house_no", "door_no", "flat_no",
    ]),
    address_street: new Set([
      "presstreet", "streetname", "street", "locality", "preslocality",
      "street_name",
    ]),
    address_location: new Set([
      "preslocation", "location", "landmark", "preslandmark", "subdistrict",
    ]),
    address_pincode: new Set([
      "prespincode", "prespin", "pincode", "pincodeval",
    ]),
    perm_address_house_no: new Set([
      "permhouseno", "permdoorno", "permflatno",
    ]),
    perm_address_street: new Set([
      "permstreet", "permlocality",
    ]),
    perm_address_location: new Set([
      "permlocation", "permlandmark",
    ]),
    perm_address_pincode: new Set([
      "permpincode", "permpin",
    ]),
    address: new Set([
      "address", "presentaddress", "permaddress",
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
    {
      key: "first_name",
      label: "First Name",
      selectors: [
        "#fname",
        "input[name='fname']",
        "#applFname",
        "input[name='applFname']",
        "#txtFname",
        "input[name='txtFname']",
        "#applicantFname",
        "input[name='applicantFname']",
        "#applicantFirstName",
        "input[name='applicantFirstName']",
      ],
    },
    {
      key: "middle_name",
      label: "Middle Name",
      selectors: [
        "#mname",
        "input[name='mname']",
        "#applMname",
        "input[name='applMname']",
        "#txtMname",
        "input[name='txtMname']",
        "#applicantMname",
        "input[name='applicantMname']",
        "#applicantMiddleName",
        "input[name='applicantMiddleName']",
      ],
    },
    {
      key: "last_name",
      label: "Last Name",
      selectors: [
        "#lname",
        "input[name='lname']",
        "#applLname",
        "input[name='applLname']",
        "#txtLname",
        "input[name='txtLname']",
        "#applicantLname",
        "input[name='applicantLname']",
        "#applicantLastName",
        "input[name='applicantLastName']",
      ],
    },
    {
      key: "full_name",
      label: "Full Name as per Records",
      selectors: [
        "#newFullName",
        "input[name='newFullName']",
        "#fullName",
        "input[name='fullName']",
        "#applFullName",
        "input[name='applFullName']",
        "#applicantName",
        "input[name='applicantName']",
        "#nameAsPerRecords",
        "input[name='nameAsPerRecords']",
      ],
    },
    {
      key: "father_first_name",
      label: "Father's / Relative's First Name",
      selectors: [
        "#swdFname",
        "input[name='swdFname']",
        "#swd_fname",
        "input[name='swd_fname']",
        "#rfname",
        "input[name='rfname']",
        "#fatherFname",
        "input[name='fatherFname']",
        "#relFname",
        "input[name='relFname']",
        "#guardianFirstName",
        "input[name='guardianFirstName']",
      ],
    },
    {
      key: "father_middle_name",
      label: "Father's / Relative's Middle Name",
      selectors: [
        "#swdMname",
        "input[name='swdMname']",
        "#swd_mname",
        "input[name='swd_mname']",
        "#rmname",
        "input[name='rmname']",
        "#fatherMname",
        "input[name='fatherMname']",
        "#relMname",
        "input[name='relMname']",
        "#guardianMiddleName",
        "input[name='guardianMiddleName']",
      ],
    },
    {
      key: "father_last_name",
      label: "Father's / Relative's Last Name",
      selectors: [
        "#swdLname",
        "input[name='swdLname']",
        "#swd_lname",
        "input[name='swd_lname']",
        "#rlname",
        "input[name='rlname']",
        "#fatherLname",
        "input[name='fatherLname']",
        "#relLname",
        "input[name='relLname']",
        "#guardianLastName",
        "input[name='guardianLastName']",
      ],
    },
    {
      key: "father_name",
      label: "Father's / Relative's Name",
      selectors: [
        "#fatherName",
        "input[name='fatherName']",
        "#swdName",
        "input[name='swdName']",
        "#guardianName",
        "input[name='guardianName']",
        "#relationName",
        "input[name='relationName']",
      ],
    },
    {
      key: "address_house_no",
      label: "Present House/Door/Flat No",
      selectors: [
        "#presHouseNo",
        "input[name='presHouseNo']",
        "#houseNo",
        "input[name='houseNo']",
        "#presDoorNo",
        "input[name='presDoorNo']",
        "#doorNo",
        "input[name='doorNo']",
        "#flatNo",
        "input[name='flatNo']",
      ],
    },
    {
      key: "address_street",
      label: "Present Street/Locality",
      selectors: [
        "#presStreet",
        "input[name='presStreet']",
        "#streetName",
        "input[name='streetName']",
        "#street",
        "input[name='street']",
        "#locality",
        "input[name='locality']",
        "#presLocality",
        "input[name='presLocality']",
      ],
    },
    {
      key: "address_location",
      label: "Present Location/Landmark",
      selectors: [
        "#presLocation",
        "input[name='presLocation']",
        "#location",
        "input[name='location']",
        "#landmark",
        "input[name='landmark']",
        "#presLandmark",
        "input[name='presLandmark']",
      ],
    },
    {
      key: "address_pincode",
      label: "Present Pincode",
      selectors: [
        "#presPinCode",
        "input[name='presPinCode']",
        "#presPincode",
        "input[name='presPincode']",
        "#presPin",
        "input[name='presPin']",
      ],
    },
    {
      key: "perm_address_house_no",
      label: "Permanent House/Door/Flat No",
      selectors: [
        "#permHouseNo",
        "input[name='permHouseNo']",
        "#permDoorNo",
        "input[name='permDoorNo']",
        "#permFlatNo",
        "input[name='permFlatNo']",
      ],
    },
    {
      key: "perm_address_street",
      label: "Permanent Street/Locality",
      selectors: [
        "#permStreet",
        "input[name='permStreet']",
        "#permLocality",
        "input[name='permLocality']",
      ],
    },
    {
      key: "perm_address_location",
      label: "Permanent Location/Landmark",
      selectors: [
        "#permLocation",
        "input[name='permLocation']",
        "#permLandmark",
        "input[name='permLandmark']",
      ],
    },
    {
      key: "perm_address_pincode",
      label: "Permanent Pincode",
      selectors: [
        "#permPinCode",
        "input[name='permPinCode']",
        "#permPincode",
        "input[name='permPincode']",
        "#permPin",
        "input[name='permPin']",
      ],
    },
    {
      key: "address",
      label: "Address",
      selectors: [
        "#presentAddress",
        "textarea[name='presentAddress']",
        "input[name='presentAddress']",
        "#address",
        "textarea[name='address']",
        "input[name='address']",
      ],
    },
  ];

  /**
   * Check if a candidate input element is safe to interact with (never CAPTCHA, buttons, or hidden).
   */
  function isSafeInputField(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toUpperCase();
    if (tag !== "INPUT" && tag !== "TEXTAREA") return false;
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
    {
      key: "full_name",
      label: "Full Name as per Records",
      pattern: /(?:full\s*name\s*as\s*per\s*records|new\s*full\s*name|applicant['’]?s?\s*full\s*name)/i,
      negative: /(?:father|mother|parent|guardian|spouse)/i,
    },
    {
      key: "first_name",
      label: "First Name",
      pattern: /(?:name\s*of\s*the\s*applicant|applicant['’]?s?\s*name|^first\s*name)/i,
      negative: /(?:father|mother|parent|guardian|spouse|relation|swd)/i,
    },
    {
      key: "middle_name",
      label: "Middle Name",
      pattern: /(?:^middle\s*name)/i,
      negative: /(?:father|mother|parent|guardian|spouse|relation|swd)/i,
    },
    {
      key: "last_name",
      label: "Last Name",
      pattern: /(?:^last\s*name|^surname)/i,
      negative: /(?:father|mother|parent|guardian|spouse|relation|swd)/i,
    },
    {
      key: "father_first_name",
      label: "Father's / Relative's First Name",
      pattern: /(?:relation|father|guardian|spouse|swd).*first\s*name/i,
    },
    {
      key: "father_middle_name",
      label: "Father's / Relative's Middle Name",
      pattern: /(?:relation|father|guardian|spouse|swd).*middle\s*name/i,
    },
    {
      key: "father_last_name",
      label: "Father's / Relative's Last Name",
      pattern: /(?:relation|father|guardian|spouse|swd).*last\s*name/i,
    },
    {
      key: "father_name",
      label: "Father's / Relative's Name",
      pattern: /(?:father['’]?s?\s*(?:full\s*)?name|guardian['’]?s?\s*name|spouse['’]?s?\s*name)/i,
    },
    {
      key: "address_house_no",
      label: "Present House/Door/Flat No",
      pattern: /(?:house[\s\/-]*door[\s\/-]*flat\s*no|house\s*no|door\s*no|flat\s*no|h\.?no)/i,
      negative: /(?:permanent|perm)/i,
    },
    {
      key: "address_street",
      label: "Present Street/Locality",
      pattern: /(?:street[\s\/-]*locality[\s\/-]*police\s*station|street\s*name|locality)/i,
      negative: /(?:permanent|perm)/i,
    },
    {
      key: "address_location",
      label: "Present Location/Landmark",
      pattern: /(?:location[\s\/-]*landmark|sub\s*district|landmark)/i,
      negative: /(?:permanent|perm)/i,
    },
    {
      key: "address_pincode",
      label: "Present Pincode",
      pattern: /(?:enter\s*pincode|pin\s*code|pincode)/i,
      negative: /(?:rto|permanent|perm)/i,
    },
    {
      key: "perm_address_house_no",
      label: "Permanent House/Door/Flat No",
      pattern: /(?:permanent|perm).*(?:house[\s\/-]*door[\s\/-]*flat\s*no|house\s*no|door\s*no|flat\s*no)/i,
    },
    {
      key: "perm_address_street",
      label: "Permanent Street/Locality",
      pattern: /(?:permanent|perm).*(?:street[\s\/-]*locality|street|locality)/i,
    },
    {
      key: "perm_address_location",
      label: "Permanent Location/Landmark",
      pattern: /(?:permanent|perm).*(?:location|landmark)/i,
    },
    {
      key: "perm_address_pincode",
      label: "Permanent Pincode",
      pattern: /(?:permanent|perm).*(?:pin\s*code|pincode)/i,
    },
    {
      key: "address",
      label: "Address",
      pattern: /(?:present\s*address|residential\s*address|^address)/i,
      negative: /(?:email|web|ip)/i,
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
        const enclosed = Array.from(labelEl.querySelectorAll("input, textarea")).filter(isSafeInputField);
        if (enclosed.length > 0) return enclosed[0];
      } catch (e) { }
    }

    // Strategy 3: Next direct element sibling
    let next = labelEl.nextElementSibling;
    while (next) {
      if (isSafeInputField(next)) return next;
      if (typeof next.querySelectorAll === "function") {
        try {
          const inside = Array.from(next.querySelectorAll("input, textarea")).filter(isSafeInputField);
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
            const inside = Array.from(parentNext.querySelectorAll("input, textarea")).filter(isSafeInputField);
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
        const trInputs = Array.from(tr.querySelectorAll("input, textarea")).filter(isSafeInputField);
        if (trInputs.length === 1) return trInputs[0];
        if (trInputs.length > 1) {
          // If labelEl is inside a td, find inputs in following sibling td cells
          let td = (labelEl.tagName || "").toUpperCase() === "TD" ? labelEl : (labelEl.closest ? labelEl.closest("td") : parent);
          if (td && td.nextElementSibling) {
            let nextTd = td.nextElementSibling;
            while (nextTd) {
              const tdInputs = Array.from(nextTd.querySelectorAll("input, textarea")).filter(isSafeInputField);
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
        const inputs = Array.from(container.querySelectorAll("input, textarea")).filter(isSafeInputField);
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

    // 4. RELATION / FATHER NAME (father_first_name, father_middle_name, father_last_name, father_name)
    const isRelationContext =
      /(?:relation|father|mother|parent|guardian|husband|spouse|\bswd\b|son[\s\/]*wife[\s\/]*daughter)/i.test(context + " " + id + " " + name);

    if (isRelationContext) {
      if (
        CANONICAL_FIELD_IDENTIFIERS.father_first_name.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.father_first_name.has(normName) ||
        /^(swd_?fname|rfname|father_?fname|rel_?fname|guardian_?firstname)$/i.test(id) ||
        /^(swd_?fname|rfname|father_?fname|rel_?fname|guardian_?firstname)$/i.test(name) ||
        /first\s*name/i.test(placeholder)
      ) {
        return "father_first_name";
      }
      if (
        CANONICAL_FIELD_IDENTIFIERS.father_middle_name.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.father_middle_name.has(normName) ||
        /^(swd_?mname|rmname|father_?mname|rel_?mname|guardian_?middlename)$/i.test(id) ||
        /^(swd_?mname|rmname|father_?mname|rel_?mname|guardian_?middlename)$/i.test(name) ||
        /middle\s*name/i.test(placeholder)
      ) {
        return "father_middle_name";
      }
      if (
        CANONICAL_FIELD_IDENTIFIERS.father_last_name.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.father_last_name.has(normName) ||
        /^(swd_?lname|rlname|father_?lname|rel_?lname|guardian_?lastname)$/i.test(id) ||
        /^(swd_?lname|rlname|father_?lname|rel_?lname|guardian_?lastname)$/i.test(name) ||
        /last\s*name/i.test(placeholder)
      ) {
        return "father_last_name";
      }
      if (
        CANONICAL_FIELD_IDENTIFIERS.father_name.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.father_name.has(normName) ||
        /^(father_?name|swd_?name|guardian_?name|relation_?name)$/i.test(id) ||
        /^(father_?name|swd_?name|guardian_?name|relation_?name)$/i.test(name)
      ) {
        return "father_name";
      }
    }

    // 5. APPLICANT NAME (first_name, middle_name, last_name, full_name)
    if (!isRelationContext) {
      if (
        CANONICAL_FIELD_IDENTIFIERS.full_name.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.full_name.has(normName) ||
        /^(newfullname|fullname|applfullname|nameasperrecords|applicantname)$/i.test(normId) ||
        /^(newfullname|fullname|applfullname|nameasperrecords|applicantname)$/i.test(normName) ||
        /new\s*full\s*name/i.test(placeholder) ||
        /full\s*name\s*as\s*per\s*records/i.test(context)
      ) {
        return "full_name";
      }
      if (
        CANONICAL_FIELD_IDENTIFIERS.first_name.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.first_name.has(normName) ||
        /^(fname|applfname|applicantfname|txtfname|first_?name)$/i.test(id) ||
        /^(fname|applfname|applicantfname|txtfname|first_?name)$/i.test(name) ||
        /first\s*name/i.test(placeholder)
      ) {
        return "first_name";
      }
      if (
        CANONICAL_FIELD_IDENTIFIERS.middle_name.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.middle_name.has(normName) ||
        /^(mname|applmname|applicantmname|txtmname|middle_?name)$/i.test(id) ||
        /^(mname|applmname|applicantmname|txtmname|middle_?name)$/i.test(name) ||
        /middle\s*name/i.test(placeholder)
      ) {
        return "middle_name";
      }
      if (
        CANONICAL_FIELD_IDENTIFIERS.last_name.has(normId) ||
        CANONICAL_FIELD_IDENTIFIERS.last_name.has(normName) ||
        /^(lname|appllname|applicantlname|txtlname|last_?name|surname)$/i.test(id) ||
        /^(lname|appllname|applicantlname|txtlname|last_?name|surname)$/i.test(name) ||
        /last\s*name|surname/i.test(placeholder)
      ) {
        return "last_name";
      }
    }

    // 6. ADDRESS FIELDS
    // Exclude RTO office pincode / controls
    if (!id.includes("rto") && !name.includes("rto") && !context.includes("rto")) {
      const isPerm = /(?:perm|permanent)/i.test(context + " " + id + " " + name);
      if (isPerm) {
        if (
          CANONICAL_FIELD_IDENTIFIERS.perm_address_house_no.has(normId) ||
          CANONICAL_FIELD_IDENTIFIERS.perm_address_house_no.has(normName) ||
          /house[\s\/-]*door[\s\/-]*flat/i.test(placeholder)
        ) {
          return "perm_address_house_no";
        }
        if (
          CANONICAL_FIELD_IDENTIFIERS.perm_address_street.has(normId) ||
          CANONICAL_FIELD_IDENTIFIERS.perm_address_street.has(normName) ||
          /street[\s\/-]*locality/i.test(placeholder)
        ) {
          return "perm_address_street";
        }
        if (
          CANONICAL_FIELD_IDENTIFIERS.perm_address_location.has(normId) ||
          CANONICAL_FIELD_IDENTIFIERS.perm_address_location.has(normName) ||
          /sub\s*district|location/i.test(placeholder)
        ) {
          return "perm_address_location";
        }
        if (
          CANONICAL_FIELD_IDENTIFIERS.perm_address_pincode.has(normId) ||
          CANONICAL_FIELD_IDENTIFIERS.perm_address_pincode.has(normName) ||
          /enter\s*pincode/i.test(placeholder)
        ) {
          return "perm_address_pincode";
        }
      } else {
        if (
          CANONICAL_FIELD_IDENTIFIERS.address_house_no.has(normId) ||
          CANONICAL_FIELD_IDENTIFIERS.address_house_no.has(normName) ||
          /^(preshouseno|houseno|presdoorno|doorno|flatno)$/i.test(id) ||
          /^(preshouseno|houseno|presdoorno|doorno|flatno)$/i.test(name) ||
          /house[\s\/-]*door[\s\/-]*flat/i.test(placeholder)
        ) {
          return "address_house_no";
        }
        if (
          CANONICAL_FIELD_IDENTIFIERS.address_street.has(normId) ||
          CANONICAL_FIELD_IDENTIFIERS.address_street.has(normName) ||
          /^(presstreet|streetname|street|locality)$/i.test(id) ||
          /^(presstreet|streetname|street|locality)$/i.test(name) ||
          /street[\s\/-]*locality/i.test(placeholder)
        ) {
          return "address_street";
        }
        if (
          CANONICAL_FIELD_IDENTIFIERS.address_location.has(normId) ||
          CANONICAL_FIELD_IDENTIFIERS.address_location.has(normName) ||
          /^(preslocation|location|landmark)$/i.test(id) ||
          /^(preslocation|location|landmark)$/i.test(name) ||
          /sub\s*district|location|landmark/i.test(placeholder)
        ) {
          return "address_location";
        }
        if (
          CANONICAL_FIELD_IDENTIFIERS.address_pincode.has(normId) ||
          CANONICAL_FIELD_IDENTIFIERS.address_pincode.has(normName) ||
          /^(prespincode|prespin|pincode|pincodeval)$/i.test(id) ||
          /^(prespincode|prespin|pincode|pincodeval)$/i.test(name) ||
          /enter\s*pincode/i.test(placeholder)
        ) {
          return "address_pincode";
        }
      }

      if (
        (CANONICAL_FIELD_IDENTIFIERS.address.has(normId) || CANONICAL_FIELD_IDENTIFIERS.address.has(normName)) &&
        !normId.includes("email") && !normName.includes("email")
      ) {
        return "address";
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
    const isLlAppSection =
      doc.getElementById("newFullName") ||
      doc.getElementById("presHouseNo") ||
      doc.getElementById("fname") ||
      path.includes("springcallbackurl") ||
      path.includes("newll") ||
      path.includes("llregistration") ||
      (doc.body && /Application for Learner['’]?s Licence/i.test(doc.body.innerText || doc.body.textContent || ""));

    if (isAppStatusSection) {
      pageType = "app_status";
      pageName = "Application Status";
    } else if (isDlSection) {
      pageType = "dl_entry";
      pageName = "DL Entry (Learner's Licence)";
    } else if (isLlAppSection) {
      pageType = "ll_application";
      pageName = "Application for Learner's Licence (LL)";
    }

    const detectedFields = [];
    const seenKeys = new Set();
    const matchedElements = new Set();

    // TIER 1: Targeted Selector Matching (with section scoping when available)
    for (const def of TARGET_FIELD_DEFINITIONS) {
      if (
        pageType === "ll_application" &&
        (def.key.startsWith("perm_") ||
         def.key === "address_street" ||
         def.key === "address_location" ||
         def.key === "address")
      ) {
        continue;
      }
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
    // Resolve any remaining form controls via visible human labels in DOM
    const labelMatches = findInputsByLabels(doc);
    for (const match of labelMatches) {
      if (
        pageType === "ll_application" &&
        (match.key.startsWith("perm_") ||
         match.key === "address_street" ||
         match.key === "address_location" ||
         match.key === "address")
      ) {
        continue;
      }
      if (!seenKeys.has(match.key) && !matchedElements.has(match.element)) {
        seenKeys.add(match.key);
        matchedElements.add(match.element);
        detectedFields.push(match);
      }
    }

    // TIER 3: Intelligent Heuristic Scanner
    // Scan all remaining safe text inputs in the DOM to discover any un-claimed controls
    let allInputs = [];
    try {
      allInputs = Array.from(doc.querySelectorAll("input, textarea"));
    } catch (e) {
      allInputs = [];
    }

    for (const input of allInputs) {
      if (matchedElements.has(input)) continue;
      if (!isSafeInputField(input)) continue;

      const detectedKey = classifyInputElement(input, doc);
      if (detectedKey && !seenKeys.has(detectedKey)) {
        if (
          pageType === "ll_application" &&
          (detectedKey.startsWith("perm_") ||
           detectedKey === "address_street" ||
           detectedKey === "address_location" ||
           detectedKey === "address")
        ) {
          continue;
        }
        const def = TARGET_FIELD_DEFINITIONS.find((d) => d.key === detectedKey);
        let label = def ? def.label : (detectedKey === "dob" ? "Date of Birth" : detectedKey);
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

    let emptyReason = "No matching Sarathi form fields detected in page DOM.";
    const bodyText = (doc && doc.body && (doc.body.innerText || doc.body.textContent || "")) || "";
    if (
      path.includes("printform") ||
      /session\s*expired|session\s*timed?\s*out|invalid\s*session|logged\s*out|please\s*try\s*again/i.test(bodyText)
    ) {
      emptyReason = "Sarathi session has expired or timed out (Sarathi expires sessions after 15 minutes of inactivity). Please refresh the page or start a fresh application from Parivahan home page.";
    }

    return {
      ready: detectedFields.length > 0,
      pageType,
      pageName,
      detectedFields,
      reason: detectedFields.length > 0 ? null : emptyReason,
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
      // Safety guard: For Sarathi LL Application, street & location are auto-filled by site dropdowns,
      // and permanent address must be manually handled by the user (never autofilled).
      // Also never touch generic 'address' or dropdowns on LL application.
      if (inspection.pageType === "ll_application") {
        if (
          detected.key.startsWith("perm_") ||
          detected.key === "address_street" ||
          detected.key === "address_location" ||
          detected.key === "address"
        ) {
          continue;
        }
      }

      let rawVal = fieldValues[detected.key];

      // On Sarathi LL Application, House/Door/FlatNo (#presHouseNo) must combine Hno and Street Locality
      if (detected.key === "address_house_no" && inspection.pageType === "ll_application") {
        const split = splitAddress(fieldValues["address"]);
        const hno = (
          fieldValues["pres_house_no"] ||
          fieldValues["house_no"] ||
          fieldValues["address_house_no"] ||
          split.houseNo ||
          ""
        ).trim();
        const street = (
          fieldValues["pres_street"] ||
          fieldValues["street"] ||
          fieldValues["address_street"] ||
          split.street ||
          ""
        ).trim();

        if (hno && street) {
          if (street.toLowerCase().startsWith(hno.toLowerCase())) {
            rawVal = street;
          } else {
            rawVal = `${hno}, ${street}`;
          }
        } else {
          rawVal = hno || street;
        }
      } else if (rawVal === undefined || rawVal === null) {
        if (detected.key === "application_no") {
          rawVal = fieldValues["appl_number"] || params.applicationNo;
        } else if (detected.key === "licence_no") {
          rawVal = fieldValues["licence_number"] || params.licenceNo;
        } else if (detected.key === "dob") {
          rawVal = params.dob;
        } else if (detected.key === "first_name") {
          const split = splitFullName(fieldValues["full_name"]);
          rawVal = split.firstName;
        } else if (detected.key === "middle_name") {
          const split = splitFullName(fieldValues["full_name"]);
          rawVal = split.middleName;
        } else if (detected.key === "last_name") {
          const split = splitFullName(fieldValues["full_name"]);
          rawVal = split.lastName;
        } else if (detected.key === "father_first_name") {
          const split = splitFullName(fieldValues["father_name"]);
          rawVal = split.firstName;
        } else if (detected.key === "father_middle_name") {
          const split = splitFullName(fieldValues["father_name"]);
          rawVal = split.middleName;
        } else if (detected.key === "father_last_name") {
          const split = splitFullName(fieldValues["father_name"]);
          rawVal = split.lastName;
        } else if (detected.key === "address_house_no") {
          const split = splitAddress(fieldValues["address"]);
          rawVal = fieldValues["pres_house_no"] || fieldValues["house_no"] || split.houseNo;
        } else if (detected.key === "perm_address_house_no") {
          const split = splitAddress(fieldValues["address"]);
          rawVal = fieldValues["perm_house_no"] || fieldValues["pres_house_no"] || fieldValues["house_no"] || split.houseNo;
        } else if (detected.key === "address_street") {
          const split = splitAddress(fieldValues["address"]);
          rawVal = fieldValues["pres_street"] || fieldValues["street"] || split.street;
        } else if (detected.key === "perm_address_street") {
          const split = splitAddress(fieldValues["address"]);
          rawVal = fieldValues["perm_street"] || fieldValues["pres_street"] || fieldValues["street"] || split.street;
        } else if (detected.key === "address_location") {
          const split = splitAddress(fieldValues["address"]);
          rawVal = fieldValues["pres_location"] || fieldValues["location"] || split.location;
        } else if (detected.key === "perm_address_location") {
          const split = splitAddress(fieldValues["address"]);
          rawVal = fieldValues["perm_location"] || fieldValues["pres_location"] || fieldValues["location"] || split.location;
        } else if (detected.key === "address_pincode") {
          const split = splitAddress(fieldValues["address"]);
          rawVal = fieldValues["pres_pincode"] || fieldValues["pincode"] || split.pincode;
        } else if (detected.key === "perm_address_pincode") {
          const split = splitAddress(fieldValues["address"]);
          rawVal = fieldValues["perm_pincode"] || fieldValues["pres_pincode"] || fieldValues["pincode"] || split.pincode;
        } else if (detected.key === "address") {
          const split = splitAddress(fieldValues["address"]);
          rawVal = fieldValues["address"] || [
            fieldValues["pres_house_no"] || fieldValues["house_no"] || split.houseNo,
            fieldValues["pres_street"] || fieldValues["street"] || split.street,
            fieldValues["pres_location"] || fieldValues["location"] || split.location,
            fieldValues["pres_district"] || fieldValues["district"] || split.district,
            fieldValues["pres_state"] || fieldValues["state"] || split.state,
            fieldValues["pres_pincode"] || fieldValues["pincode"] || split.pincode,
          ].filter(Boolean).join(", ");
        }
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

      // Dropdown protection guard: never write free text into a SELECT control
      if ((el.tagName || "").toUpperCase() === "SELECT") {
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

  /**
   * Classify a visible, enabled <input type="file"> element on Sarathi upload pages.
   * Returns 'photo' | 'signature' | 'document' | 'generic'.
   */
  function classifyFileInput(el, doc) {
    if (!el) return "generic";
    const idNorm = normalizeIdentifier(el.id || "");
    const nameNorm = normalizeIdentifier(el.name || "");
    const ctx = getElementContextText(el, doc).toLowerCase();
    const combined = `${idNorm} ${nameNorm} ${ctx}`;

    if (/\b(?:sign|signature|sigfile|signfile|thumb|biometricsign)\b/.test(combined) ||
        idNorm.includes("sign") || nameNorm.includes("sign")) {
      return "signature";
    }
    if (/\b(?:photo|photograph|passphoto|passport|applicantphoto|biophoto|imgfile|photofile)\b/.test(combined) ||
        idNorm.includes("photo") || nameNorm.includes("photo")) {
      return "photo";
    }
    if (/\b(?:document|doc|proof|aadhaar|aadhar|uploadedfile|uploadfile|certificate|licence|license|address)\b/.test(combined) ||
        idNorm.includes("doc") || nameNorm.includes("uploadedfile") || idNorm.includes("uploadedfile")) {
      return "document";
    }
    return "generic";
  }

  /**
   * Inspect visible, enabled <input type="file"> controls on the Sarathi portal.
   */
  function inspectFileInputs(doc, loc) {
    if (!validatePortalLocation(loc)) {
      return {
        ready: false,
        inputs: [],
        reason: "Active tab is not on the Sarathi portal (https://sarathi.parivahan.gov.in).",
      };
    }
    if (!doc) {
      return { ready: false, inputs: [], reason: "No document context available." };
    }

    const rawInputs = Array.from(doc.querySelectorAll("input")).filter(
      (el) => ((el.type || (typeof el.getAttribute === "function" ? el.getAttribute("type") : "") || "").toLowerCase() === "file")
    );
    const inputs = [];
    for (const el of rawInputs) {
      if (el.disabled || el.readOnly || !isElementVisible(el)) continue;
      const kind = classifyFileInput(el, doc);
      const label =
        kind === "photo" ? "Applicant Photo" :
        kind === "signature" ? "Applicant Signature" :
        kind === "document" ? "Supporting Document" : "Upload File";
      inputs.push({
        kind,
        label,
        id: el.id || "",
        name: el.name || "",
        element: el,
      });
    }

    return {
      ready: inputs.length > 0,
      inputs: inputs.map(({ kind, label, id, name }) => ({ kind, label, id, name })),
      _elements: inputs,
      reason: inputs.length > 0 ? null : "No visible file upload input found on this Sarathi page.",
    };
  }

  /**
   * Attach a pre-processed FormFlow file into the matching Sarathi <input type="file">
   * using DataTransfer (operator still previews and clicks Upload manually).
   */
  function attachFileToInput(doc, loc, params) {
    if (!validatePortalLocation(loc)) {
      return {
        ok: false,
        error: "Active tab is not on the Sarathi portal (https://sarathi.parivahan.gov.in).",
      };
    }
    if (!params || typeof params !== "object") {
      return { ok: false, error: "Invalid file attachment command." };
    }
    const filename = typeof params.filename === "string" ? params.filename.trim() : "";
    const mime = typeof params.mime === "string" && params.mime.trim() ? params.mime.trim() : "image/jpeg";
    const doctype = typeof params.doctype === "string" ? params.doctype.trim().toLowerCase() : "document";
    const bytes = params.bytes;

    if (!filename || !Array.isArray(bytes) || bytes.length === 0) {
      return { ok: false, error: "Prepared file bytes and filename are required." };
    }

    const inspection = inspectFileInputs(doc, loc);
    if (!inspection.ready || !inspection._elements || inspection._elements.length === 0) {
      return { ok: false, error: inspection.reason || "No file upload input found on this page." };
    }

    const desiredKind =
      doctype === "photo" ? "photo" :
      doctype === "signature" ? "signature" : "document";

    const elements = inspection._elements;
    let target = elements.find((item) => item.kind === desiredKind);
    if (!target && desiredKind === "document") {
      // On single-document upload screens, a generic file input is unambiguous
      const nonBio = elements.filter((item) => item.kind !== "photo" && item.kind !== "signature");
      if (nonBio.length === 1) {
        target = nonBio[0];
      }
    }
    if (!target && elements.length === 1) {
      target = elements[0];
    }

    if (!target || !target.element) {
      return {
        ok: false,
        error: `Could not uniquely match a '${desiredKind}' file input among ${elements.length} upload box(es) on this page. Select READY file directly from Inbox.`,
      };
    }

    try {
      const FileCtor = (doc.defaultView && doc.defaultView.File) || (typeof File !== "undefined" ? File : null);
      const DTCtor = (doc.defaultView && doc.defaultView.DataTransfer) || (typeof DataTransfer !== "undefined" ? DataTransfer : null);
      if (!FileCtor || !DTCtor) {
        return { ok: false, error: "Browser DataTransfer API is unavailable on this page." };
      }
      const fileObj = new FileCtor([new Uint8Array(bytes)], filename, { type: mime });
      const dt = new DTCtor();
      dt.items.add(fileObj);
      target.element.files = dt.files;
      target.element.dispatchEvent(new Event("input", { bubbles: true }));
      target.element.dispatchEvent(new Event("change", { bubbles: true }));

      const attachedOk =
        target.element.files &&
        target.element.files.length === 1 &&
        target.element.files[0].name === filename;

      if (!attachedOk) {
        return { ok: false, error: "File attachment read-back verification failed." };
      }

      return {
        ok: true,
        targetKind: target.kind,
        targetLabel: target.label,
        filename,
        message: `Attached ${filename} to ${target.label}. Review preview and click Upload on Sarathi.`,
      };
    } catch (e) {
      return {
        ok: false,
        error: `Could not attach file automatically (${e.message || "blocked"}). Pick ${filename} from Inbox.`,
      };
    }
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
    classifyFileInput,
    inspectFileInputs,
    attachFileToInput,
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
    splitFullName,
    splitAddress,
  };

  const root = typeof window !== "undefined" ? window : globalThis;
  root.FormFlowPortalAdapter = adapter;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = adapter;
  }
})();
