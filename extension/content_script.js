/**
 * FormFlow Content Script for Sarathi (https://sarathi.parivahan.gov.in/*)
 *
 * Listens for explicit operator actions from the extension popup.
 * Invariants:
 * - Never modifies or inspects DOM until explicitly queried/commanded
 * - Never touches CAPTCHA, submit/navigation buttons, or payment controls
 * - Never logs applicant data or tokens
 */

(function () {
  "use strict";

  const adapter = window.FormFlowPortalAdapter;
  if (!adapter) return;

  function validateTraceHeader(message) {
    const caseId = typeof message.caseId === "string" ? message.caseId.trim() : "";
    if (!caseId) {
      return { ok: false, error: "Invalid command: non-empty caseId is required for traceability." };
    }
    const reviewVersion = message.reviewVersion;
    if (typeof reviewVersion !== "number" || !Number.isInteger(reviewVersion) || reviewVersion <= 0) {
      return { ok: false, error: "Invalid command: positive integer reviewVersion is required." };
    }
    return { ok: true, caseId, reviewVersion };
  }

  function failCommand(sendResponse, error) {
    sendResponse({ ok: false, partial: false, error });
    return true;
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || typeof message !== "object") return;

    if (message.action === "CHECK_DL_PAGE") {
      const res = adapter.inspectDlPage(document, window.location);
      sendResponse({ ok: true, ready: res.ready, reason: res.reason });
      return true;
    }

    if (message.action === "FILL_DL_FIELDS") {
      const hdr = validateTraceHeader(message);
      if (!hdr.ok) return failCommand(sendResponse, hdr.error);
      const licenceNo = typeof message.licenceNo === "string" ? message.licenceNo.trim() : "";
      const dob = typeof message.dob === "string" ? message.dob.trim() : "";
      if (!licenceNo) return failCommand(sendResponse, "Invalid command: non-empty Learner's Licence Number is required.");
      if (!dob) return failCommand(sendResponse, "Invalid command: non-empty Date of Birth is required.");
      sendResponse(adapter.fillDlFields(document, window.location, {
        caseId: hdr.caseId,
        reviewVersion: hdr.reviewVersion,
        licenceNo,
        dob,
      }));
      return true;
    }

    if (message.action === "CHECK_APP_STATUS_PAGE") {
      const res = adapter.inspectAppStatusPage(document, window.location);
      sendResponse({ ok: true, ready: res.ready, reason: res.reason });
      return true;
    }

    if (message.action === "FILL_APP_STATUS_FIELDS") {
      const hdr = validateTraceHeader(message);
      if (!hdr.ok) return failCommand(sendResponse, hdr.error);
      const applicationNo = typeof message.applicationNo === "string" ? message.applicationNo.trim() : "";
      const dob = typeof message.dob === "string" ? message.dob.trim() : "";
      if (!applicationNo) return failCommand(sendResponse, "Invalid command: non-empty Application Number is required.");
      if (!dob) return failCommand(sendResponse, "Invalid command: non-empty Date of Birth is required.");
      sendResponse(adapter.fillAppStatusFields(document, window.location, {
        caseId: hdr.caseId,
        reviewVersion: hdr.reviewVersion,
        applicationNo,
        dob,
      }));
      return true;
    }

    if (message.action === "INSPECT_PAGE_FOR_AUTOFILL") {
      const res = adapter.inspectPageForAutofill(document, window.location);
      sendResponse({
        ok: true,
        ready: res.ready,
        pageType: res.pageType,
        pageName: res.pageName,
        detectedFields: res.detectedFields,
        reason: res.reason,
      });
      return true;
    }

    if (message.action === "AUTOFILL_MATCHED_FIELDS") {
      const hdr = validateTraceHeader(message);
      if (!hdr.ok) return failCommand(sendResponse, hdr.error);
      const fieldValues = message.fieldValues || message.fields || {};
      sendResponse(adapter.autofillMatchedFields(document, window.location, {
        caseId: hdr.caseId,
        reviewVersion: hdr.reviewVersion,
        fieldValues,
      }));
      return true;
    }

    if (message.action === "INSPECT_FILE_INPUTS") {
      const res = adapter.inspectFileInputs(document, window.location);
      sendResponse({ ok: true, ready: res.ready, inputs: res.inputs, reason: res.reason });
      return true;
    }

    if (message.action === "ATTACH_FILE_TO_INPUT") {
      sendResponse(adapter.attachFileToInput(document, window.location, {
        caseId: message.caseId,
        reviewVersion: message.reviewVersion,
        doctype: message.doctype,
        filename: message.filename,
        mime: message.mime,
        bytes: message.bytes,
      }));
      return true;
    }
  });
})();
