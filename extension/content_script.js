/**
 * FormFlow Content Script for Sarathi DL Entry (Phase 7B)
 *
 * Runs exclusively on https://sarathi.parivahan.gov.in/sarathiservice/*
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
      const fillRes = adapter.fillDlFields(document, window.location, {
        licenceNo: message.licenceNo,
        dob: message.dob,
      });
      sendResponse(fillRes);
      return true;
    }
  });
})();
