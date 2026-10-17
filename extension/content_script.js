/**
 * FormFlow Content Script for Sarathi DL Entry (Phase 7B)
 *
 * Runs exclusively on https://sarathi.parivahan.gov.in/*
 * Listens for explicit operator actions from the extension popup.
 *
 * Invariants:
 * - Does not modify or inspect DOM until explicitly queried/commanded by operator
 * - Never touches CAPTCHA, buttons, or navigation
 * - Never logs applicant data or tokens
 */

(function () {
  "use strict";

  const adapter = window.FormFlowPortalAdapter;
  if (!adapter) {
    // If running in environment without separate script loading, adapter might be loaded together
    return;
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || typeof message !== "object") return;

    if (message.action === "CHECK_DL_PAGE") {
      const res = adapter.inspectDlPage(document, window.location);
      sendResponse({
        ok: true,
        ready: res.ready,
        reason: res.reason,
      });
      return true;
    }

    if (message.action === "FILL_DL_FIELDS") {
      const caseId = typeof message.caseId === "string" ? message.caseId.trim() : "";
      const reviewVersion = message.reviewVersion;
      const licenceNo = typeof message.licenceNo === "string" ? message.licenceNo.trim() : "";
      const dob = typeof message.dob === "string" ? message.dob.trim() : "";

      if (!caseId) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: non-empty caseId is required for traceability.",
        });
        return true;
      }

      if (
        typeof reviewVersion !== "number" ||
        !Number.isInteger(reviewVersion) ||
        reviewVersion <= 0
      ) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: positive integer reviewVersion is required.",
        });
        return true;
      }

      if (!licenceNo) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: non-empty Learner's Licence Number is required.",
        });
        return true;
      }

      if (!dob) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: non-empty Date of Birth is required.",
        });
        return true;
      }

      // Re-confirm intended page, visible section, and perform read-back verified fill
      const fillRes = adapter.fillDlFields(document, window.location, {
        caseId,
        reviewVersion,
        licenceNo,
        dob,
      });
      sendResponse(fillRes);
      return true;
    }

    if (message.action === "CHECK_APP_STATUS_PAGE") {
      const res = adapter.inspectAppStatusPage(document, window.location);
      sendResponse({
        ok: true,
        ready: res.ready,
        reason: res.reason,
      });
      return true;
    }

    if (message.action === "FILL_APP_STATUS_FIELDS") {
      const caseId = typeof message.caseId === "string" ? message.caseId.trim() : "";
      const reviewVersion = message.reviewVersion;
      const applicationNo = typeof message.applicationNo === "string" ? message.applicationNo.trim() : "";
      const dob = typeof message.dob === "string" ? message.dob.trim() : "";

      if (!caseId) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: non-empty caseId is required for traceability.",
        });
        return true;
      }

      if (
        typeof reviewVersion !== "number" ||
        !Number.isInteger(reviewVersion) ||
        reviewVersion <= 0
      ) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: positive integer reviewVersion is required.",
        });
        return true;
      }

      if (!applicationNo) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: non-empty Application Number is required.",
        });
        return true;
      }

      if (!dob) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: non-empty Date of Birth is required.",
        });
        return true;
      }

      const fillRes = adapter.fillAppStatusFields(document, window.location, {
        caseId,
        reviewVersion,
        applicationNo,
        dob,
      });
      sendResponse(fillRes);
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
      const caseId = typeof message.caseId === "string" ? message.caseId.trim() : "";
      const reviewVersion = message.reviewVersion;
      const fieldValues = message.fieldValues || message.fields || {};

      if (!caseId) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: non-empty caseId is required for traceability.",
        });
        return true;
      }

      if (
        typeof reviewVersion !== "number" ||
        !Number.isInteger(reviewVersion) ||
        reviewVersion <= 0
      ) {
        sendResponse({
          ok: false,
          partial: false,
          error: "Invalid command: positive integer reviewVersion is required.",
        });
        return true;
      }

      const fillRes = adapter.autofillMatchedFields(document, window.location, {
        caseId,
        reviewVersion,
        fieldValues,
      });
      sendResponse(fillRes);
      return true;
    }
  });
})();
