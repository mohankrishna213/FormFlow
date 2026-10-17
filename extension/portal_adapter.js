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
   * Validate that the active URL is on the official Sarathi portal under /sarathiservice/.
   */
  function validatePortalLocation(loc) {
    if (!loc) return false;
    const hostname = (loc.hostname || "").toLowerCase();
    const pathname = loc.pathname || "";
    return hostname === "sarathi.parivahan.gov.in" && pathname.startsWith("/sarathiservice/");
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
        reason: "Active tab is not on the Sarathi portal (https://sarathi.parivahan.gov.in/sarathiservice/*).",
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
   * Explicitly fill #learningLicence and #DOB with framework event dispatch
   * and read-back verification.
   */
  function fillDlFields(doc, loc, params) {
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

  // Export
  const adapter = {
    validatePortalLocation,
    formatDobForPortal,
    inspectDlPage,
    fillDlFields,
  };

  const root = typeof window !== "undefined" ? window : globalThis;
  root.FormFlowPortalAdapter = adapter;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = adapter;
  }
})();
