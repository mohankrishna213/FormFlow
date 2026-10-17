/**
 * Node.js Test Suite for FormFlow Portal Adapter (Phase 7B)
 *
 * Tests:
 * 1. Location validation: accepts sarathi.parivahan.gov.in/sarathiservice/*, rejects others.
 * 2. Date formatting: YYYY-MM-DD -> DD-MM-YYYY, rejects invalid/out-of-range/ambiguous dates.
 * 3. Page inspection: verifies Holding Learners Licence mode, #lldetailsDisplay visibility.
 * 4. Section-scoped isolation: ignores unrelated #DOB inputs elsewhere in the document.
 * 5. Safety exclusions: CAPTCHA (#entcaptxt) and buttons (#ok, #can1) are never targeted.
 * 6. Element validation: detects hidden, disabled, read-only, missing, or ambiguous targets.
 * 7. Fill execution & event dispatch: values set, input/change events fired, read-back verified.
 * 8. Partial failure reporting: reports honestly if one field fails read-back.
 */

const assert = require("assert");
const portalAdapter = require("../extension/portal_adapter.js");

class MockElement {
  constructor(tag, id = "", name = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.name = name;
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.readOnly = false;
    this.hidden = false;
    this.style = {};
    this.children = [];
    this.parentNode = null;
    this.dispatchedEvents = [];
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }

  dispatchEvent(event) {
    this.dispatchedEvents.push(event);
    return true;
  }

  querySelectorAll(selector) {
    const results = [];
    const search = (node) => {
      for (const child of node.children) {
        if (selector === "#learningLicence" && child.id === "learningLicence") {
          results.push(child);
        } else if (selector === "#DOB" && child.id === "DOB") {
          results.push(child);
        } else if (selector === 'input[type="radio"][name="llHolder"]' && child.name === "llHolder") {
          results.push(child);
        } else if (selector === 'input[type="radio"][name="llHolder"]:checked' && child.name === "llHolder" && child.checked) {
          results.push(child);
        }
        search(child);
      }
    };
    search(this);
    return results;
  }

  querySelector(selector) {
    const res = this.querySelectorAll(selector);
    return res.length > 0 ? res[0] : null;
  }
}

class MockDocument {
  constructor() {
    this.root = new MockElement("BODY");
    this.idMap = new Map();
  }

  register(element) {
    if (element.id) {
      this.idMap.set(element.id, element);
    }
    return element;
  }

  getElementById(id) {
    return this.idMap.get(id) || null;
  }

  querySelectorAll(selector) {
    return this.root.querySelectorAll(selector);
  }

  querySelector(selector) {
    return this.root.querySelector(selector);
  }
}

// Global Event mock if running outside browser
if (typeof global.Event === "undefined") {
  global.Event = class Event {
    constructor(type, opts = {}) {
      this.type = type;
      this.bubbles = Boolean(opts.bubbles);
    }
  };
}

function buildValidSarathiDom() {
  const doc = new MockDocument();

  // Radio button 1: Holding Learners Licence (checked)
  const radio1 = new MockElement("input", "llHolder1", "llHolder");
  radio1.value = "1";
  radio1.checked = true;
  doc.root.appendChild(radio1);
  doc.register(radio1);

  // Radio button 2: Holding Foreign DL (unchecked)
  const radio2 = new MockElement("input", "llHolder2", "llHolder");
  radio2.value = "2";
  radio2.checked = false;
  doc.root.appendChild(radio2);
  doc.register(radio2);

  // Unrelated DOB input elsewhere in page (e.g. parent/guardian details)
  const unrelatedDob = new MockElement("input", "DOB", "parentDob");
  doc.root.appendChild(unrelatedDob);

  // Section container #lldetailsDisplay
  const section = new MockElement("div", "lldetailsDisplay");
  doc.root.appendChild(section);
  doc.register(section);

  // Target inputs inside #lldetailsDisplay
  const llInput = new MockElement("input", "learningLicence", "learningLicence");
  section.appendChild(llInput);
  doc.register(llInput);

  const targetDob = new MockElement("input", "DOB", "DOB");
  section.appendChild(targetDob);
  doc.register(targetDob);

  // CAPTCHA input inside section (must never be touched)
  const captcha = new MockElement("input", "entcaptxt", "entcaptxt");
  section.appendChild(captcha);
  doc.register(captcha);

  // OK button (must never be touched)
  const okBtn = new MockElement("input", "ok", "ok");
  section.appendChild(okBtn);
  doc.register(okBtn);

  return { doc, radio1, radio2, section, llInput, targetDob, unrelatedDob, captcha, okBtn };
}

const VALID_LOCATION = {
  hostname: "sarathi.parivahan.gov.in",
  pathname: "/sarathiservice/newDL_displayPage.do",
};

// -------------------------------------------------------------
// Test Suite
// -------------------------------------------------------------

function runTests() {
  console.log("\n=== Running FormFlow Sarathi Portal Adapter Tests ===");

  // 1. Location Validation
  assert.strictEqual(portalAdapter.validatePortalLocation(VALID_LOCATION), true);
  assert.strictEqual(
    portalAdapter.validatePortalLocation({ hostname: "sarathi.parivahan.gov.in", pathname: "/other/page.do" }),
    false,
    "Should reject paths outside /sarathiservice/"
  );
  assert.strictEqual(
    portalAdapter.validatePortalLocation({ hostname: "evil.com", pathname: "/sarathiservice/newDL.do" }),
    false,
    "Should reject non-sarathi hosts"
  );
  assert.strictEqual(portalAdapter.validatePortalLocation(null), false);
  console.log("[PASS] 1. Location validation strictly checks host and /sarathiservice/ path");

  // 2. Date Formatting
  assert.strictEqual(portalAdapter.formatDobForPortal("1994-06-25"), "25-06-1994");
  assert.strictEqual(portalAdapter.formatDobForPortal("2001-01-05"), "05-01-2001");
  assert.throws(() => portalAdapter.formatDobForPortal("25-06-1994"), /Expected YYYY-MM-DD/);
  assert.throws(() => portalAdapter.formatDobForPortal("1995-02-30"), /not a valid calendar day/);
  assert.throws(() => portalAdapter.formatDobForPortal("1995-13-01"), /outside valid range/);
  assert.throws(() => portalAdapter.formatDobForPortal(""), /missing or invalid/);
  console.log("[PASS] 2. Date conversion validates calendar correctness and formats to DD-MM-YYYY");

  // 3. Page Inspection on Valid DOM
  const { doc, radio1, radio2, section, llInput, targetDob, unrelatedDob, captcha, okBtn } = buildValidSarathiDom();
  const inspectRes = portalAdapter.inspectDlPage(doc, VALID_LOCATION);
  assert.strictEqual(inspectRes.ready, true);
  assert.strictEqual(inspectRes.llInput, llInput);
  assert.strictEqual(inspectRes.dobInput, targetDob);
  console.log("[PASS] 3. Page inspection accepts valid Sarathi DL Entry DOM");

  // 4. Section Scoping: Unrelated DOB outside section ignored
  assert.notStrictEqual(inspectRes.dobInput, unrelatedDob);
  console.log("[PASS] 4. Targets are scoped strictly inside #lldetailsDisplay (unrelated #DOB ignored)");

  // 5. Holding Learners Licence Mode Requirement
  radio1.checked = false;
  radio2.checked = true; // Foreign DL selected
  const modeRes = portalAdapter.inspectDlPage(doc, VALID_LOCATION);
  assert.strictEqual(modeRes.ready, false);
  assert.match(modeRes.reason, /Current mode is not 'Holding Learners Licence'/);
  radio1.checked = true;
  radio2.checked = false;
  console.log("[PASS] 5. Disables filling if 'Holding Learners Licence' is not the active mode");

  // 6. Section Visibility Requirement
  section.style.display = "none";
  const hiddenSecRes = portalAdapter.inspectDlPage(doc, VALID_LOCATION);
  assert.strictEqual(hiddenSecRes.ready, false);
  assert.match(hiddenSecRes.reason, /section \(#lldetailsDisplay\) is currently hidden/);
  section.style.display = "block";
  console.log("[PASS] 6. Blocks filling when #lldetailsDisplay is hidden");

  // 7. Input Disabled / Read-Only / Ambiguity Rejection
  llInput.disabled = true;
  const disabledRes = portalAdapter.inspectDlPage(doc, VALID_LOCATION);
  assert.strictEqual(disabledRes.ready, false);
  assert.match(disabledRes.reason, /input is disabled or read-only/);
  llInput.disabled = false;

  // Ambiguous duplicate input inside section
  const dupLl = new MockElement("input", "learningLicence", "learningLicence");
  section.appendChild(dupLl);
  const ambigRes = portalAdapter.inspectDlPage(doc, VALID_LOCATION);
  assert.strictEqual(ambigRes.ready, false);
  assert.match(ambigRes.reason, /Ambiguous DOM: found 2/);
  section.children.pop(); // Remove duplicate
  console.log("[PASS] 7. Rejects disabled, read-only, or ambiguous inputs");

  // 8. Autofill Execution & Event Dispatch
  const fillRes = portalAdapter.fillDlFields(doc, VALID_LOCATION, {
    licenceNo: "TS09/0001234/2021",
    dob: "1994-06-25",
  });
  assert.strictEqual(fillRes.ok, true);
  assert.strictEqual(fillRes.filled.licenceNo, true);
  assert.strictEqual(fillRes.filled.dob, true);
  assert.strictEqual(llInput.value, "TS09/0001234/2021");
  assert.strictEqual(targetDob.value, "25-06-1994");

  // Verify events dispatched
  assert.strictEqual(llInput.dispatchedEvents.length, 2);
  assert.strictEqual(llInput.dispatchedEvents[0].type, "input");
  assert.strictEqual(llInput.dispatchedEvents[1].type, "change");
  assert.strictEqual(targetDob.dispatchedEvents.length, 2);
  assert.strictEqual(targetDob.dispatchedEvents[0].type, "input");
  assert.strictEqual(targetDob.dispatchedEvents[1].type, "change");

  // Verify safety invariants: CAPTCHA and buttons were completely untouched
  assert.strictEqual(captcha.value, "");
  assert.strictEqual(captcha.dispatchedEvents.length, 0);
  assert.strictEqual(okBtn.dispatchedEvents.length, 0);
  console.log("[PASS] 8. Successfully filled fields, dispatched events, and left CAPTCHA/buttons untouched");

  // 9. Read-back Verification & Partial Failure Handling
  // Simulate read-back mismatch on DOB (e.g., portal validation script overwrote or cleared it)
  targetDob.value = ""; // cleared
  const partialRes = portalAdapter.fillDlFields(doc, VALID_LOCATION, {
    licenceNo: "TS09/0001234/2021",
    dob: "1994-06-25",
  });
  // Note: in fillDlFields, it sets both values, then reads back. To simulate failure on one:
  // let's create a proxy or setter that rejects DOB
  Object.defineProperty(targetDob, "value", {
    get: () => "failed-val",
    set: () => {},
    configurable: true,
  });
  const simulatedPartial = portalAdapter.fillDlFields(doc, VALID_LOCATION, {
    licenceNo: "TS09/0001234/2021",
    dob: "1994-06-25",
  });
  assert.strictEqual(simulatedPartial.ok, false);
  assert.strictEqual(simulatedPartial.partial, true);
  assert.strictEqual(simulatedPartial.filled.licenceNo, true);
  assert.strictEqual(simulatedPartial.filled.dob, false);
  console.log("[PASS] 9. Honestly reports partial failures if read-back verification fails on any field");

  // 10. Command Metadata Validation
  assert.strictEqual(portalAdapter.validateFillCommand(null).valid, false);
  assert.strictEqual(portalAdapter.validateFillCommand({}).valid, false);
  assert.strictEqual(portalAdapter.validateFillCommand({ caseId: "", reviewVersion: 1, licenceNo: "TS09/123", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateFillCommand({ caseId: "CASE-1", reviewVersion: 0, licenceNo: "TS09/123", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateFillCommand({ caseId: "CASE-1", reviewVersion: "1", licenceNo: "TS09/123", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateFillCommand({ caseId: "CASE-1", reviewVersion: -2, licenceNo: "TS09/123", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateFillCommand({ caseId: "CASE-1", reviewVersion: 1, licenceNo: "", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateFillCommand({ caseId: "CASE-1", reviewVersion: 1, licenceNo: "TS09/123", dob: "invalid-date" }).valid, false);
  const validCmd = portalAdapter.validateFillCommand({
    caseId: "CASE-20261017-001",
    reviewVersion: 2,
    licenceNo: "TS09/0001234/2021",
    dob: "1994-06-25",
  });
  assert.strictEqual(validCmd.valid, true);
  console.log("[PASS] 10. Command metadata validation enforces caseId, reviewVersion, licenceNo, and dob");

  // 11. Invalid command metadata prevents fill execution
  const invalidFill = portalAdapter.fillDlFields(doc, VALID_LOCATION, {
    caseId: "",
    reviewVersion: 1,
    licenceNo: "TS09/0001234/2021",
    dob: "1994-06-25",
  });
  assert.strictEqual(invalidFill.ok, false);
  assert.match(invalidFill.error, /caseId is required/);
  console.log("[PASS] 11. Invalid command metadata rejected before filling");

  // 12. Unsupported host, path, mode, or hidden section prevents filling
  const wrongHostLoc = { hostname: "google.com", pathname: "/sarathiservice/newDL_displayDLPage.do" };
  const wrongHostFill = portalAdapter.fillDlFields(doc, wrongHostLoc, {
    caseId: "CASE-1", reviewVersion: 1, licenceNo: "TS09/0001234/2021", dob: "1994-06-25",
  });
  assert.strictEqual(wrongHostFill.ok, false);
  assert.match(wrongHostFill.error, /Active tab is not on the Sarathi portal/);

  const wrongPathLoc = { hostname: "sarathi.parivahan.gov.in", pathname: "/other/page.do" };
  const wrongPathFill = portalAdapter.fillDlFields(doc, wrongPathLoc, {
    caseId: "CASE-1", reviewVersion: 1, licenceNo: "TS09/0001234/2021", dob: "1994-06-25",
  });
  assert.strictEqual(wrongPathFill.ok, false);
  assert.match(wrongPathFill.error, /Active tab is not on the Sarathi portal/);

  section.style.display = "none";
  const hiddenFill = portalAdapter.fillDlFields(doc, VALID_LOCATION, {
    caseId: "CASE-1", reviewVersion: 1, licenceNo: "TS09/0001234/2021", dob: "1994-06-25",
  });
  assert.strictEqual(hiddenFill.ok, false);
  assert.match(hiddenFill.error, /section \(#lldetailsDisplay\) is currently hidden/);
  section.style.display = "block";
  console.log("[PASS] 12. Unsupported host, path, or hidden section prevents filling");

  console.log("\nAll Portal Adapter tests passed successfully (12/12)!\n");
}

runTests();
