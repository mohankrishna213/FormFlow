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
    this.placeholder = "";
    this.title = "";
    this.checked = false;
    this.disabled = false;
    this.readOnly = false;
    this.hidden = false;
    this.style = {};
    this.children = [];
    this.parentNode = null;
    this.dispatchedEvents = [];
    this.attributes = {};
    this.textContent = "";
    this.className = "";
  }

  get previousElementSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    return idx > 0 ? siblings[idx - 1] : null;
  }

  get nextElementSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    return (idx >= 0 && idx < siblings.length - 1) ? siblings[idx + 1] : null;
  }

  get parentElement() {
    return this.parentNode;
  }

  getAttribute(attr) {
    if (attr === "placeholder") return this.placeholder;
    if (attr === "title") return this.title;
    if (attr === "id") return this.id;
    if (attr === "name") return this.name;
    if (attr === "type") return this.type || "";
    return this.attributes[attr] || null;
  }

  setAttribute(attr, val) {
    this.attributes[attr] = val;
    if (attr === "placeholder") this.placeholder = val;
    if (attr === "title") this.title = val;
    if (attr === "type") this.type = val;
  }

  focus() {
    this.focused = true;
  }

  blur() {
    this.blurred = true;
  }

  closest(selector) {
    let curr = this;
    const tokens = selector.split(",").map((s) => s.trim().toLowerCase());
    while (curr) {
      const tag = (curr.tagName || "").toLowerCase();
      const classes = (curr.className || "").split(/\s+/).filter(Boolean);
      for (const tok of tokens) {
        if (tok === tag) return curr;
        if (tok.startsWith(".") && classes.includes(tok.slice(1))) return curr;
        if (tok.startsWith("#") && curr.id === tok.slice(1)) return curr;
      }
      curr = curr.parentNode;
    }
    return null;
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

  contains(node) {
    let curr = node ? node.parentNode : null;
    while (curr) {
      if (curr === this) return true;
      curr = curr.parentNode;
    }
    return false;
  }

  querySelectorAll(selector) {
    if (selector.includes(",")) {
      const parts = selector.split(",").map((s) => s.trim());
      const set = new Set();
      const combined = [];
      for (const part of parts) {
        const sub = this.querySelectorAll(part);
        for (const el of sub) {
          if (!set.has(el)) {
            set.add(el);
            combined.push(el);
          }
        }
      }
      return combined;
    }

    const results = [];
    const search = (node) => {
      for (const child of node.children) {
        let match = false;
        if (selector === "input, textarea, select") {
          const t = child.tagName.toLowerCase();
          match = t === "input" || t === "textarea" || t === "select";
        } else if (selector === "label, th, td, span, div") {
          const t = child.tagName.toLowerCase();
          match = ["label", "th", "td", "span", "div"].includes(t);
        } else if (selector.startsWith("#") && child.id === selector.slice(1)) {
          match = true;
        } else if (selector.startsWith(".")) {
          const cls = selector.slice(1);
          const classes = (child.className || "").split(/\s+/).filter(Boolean);
          match = classes.includes(cls);
        } else if (selector.includes(" ")) {
          const parts = selector.split(/\s+/);
          const last = parts[parts.length - 1];
          if (last.startsWith("#") && child.id === last.slice(1)) match = true;
          else if (last.startsWith("input#") && child.tagName === "INPUT" && child.id === last.slice(6)) match = true;
        } else if (selector.startsWith("input#") && child.tagName === "INPUT" && child.id === selector.slice(6)) {
          match = true;
        } else if (selector.startsWith("input[name='") && selector.endsWith("']") && child.tagName === "INPUT") {
          match = child.name === selector.slice(12, -2);
        } else if (selector.startsWith("label[for='") && selector.endsWith("']") && child.tagName === "LABEL") {
          match = (child.getAttribute("for") || child.htmlFor) === selector.slice(11, -2);
        } else if (selector === 'input[type="radio"][name="llHolder"]' && child.name === "llHolder") {
          match = true;
        } else if (selector === 'input[type="radio"][name="llHolder"]:checked' && child.name === "llHolder" && child.checked) {
          match = true;
        } else if (selector.toLowerCase() === child.tagName.toLowerCase()) {
          match = true;
        }
        if (match) results.push(child);
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
    return this.idMap.get(id) || this.root.querySelector("#" + id) || null;
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

function buildValidAppStatusDom() {
  const doc = new MockDocument();

  // Enclosing form applViewStages
  const form = new MockElement("form", "applViewStages", "applViewStages");
  doc.root.appendChild(form);
  doc.register(form);

  // Section container #publicApplStatus
  const section = new MockElement("div", "publicApplStatus");
  form.appendChild(section);
  doc.register(section);

  // Target inputs inside #publicApplStatus
  const applInput = new MockElement("input", "applNum", "applNum");
  section.appendChild(applInput);
  doc.register(applInput);

  const dobInput = new MockElement("input", "dateOfBirth", "dateOfBirth");
  section.appendChild(dobInput);
  doc.register(dobInput);

  // Unrelated input outside section
  const outsideInput = new MockElement("input", "applNum", "otherApplNum");
  doc.root.appendChild(outsideInput);

  // CAPTCHA and buttons (must never be touched)
  const captcha = new MockElement("input", "entcaptxt", "entcaptxt");
  section.appendChild(captcha);
  doc.register(captcha);

  const submitBtn = new MockElement("input", "submit", "newll.submit");
  section.appendChild(submitBtn);
  doc.register(submitBtn);

  const cancelBtn = new MockElement("input", "cancel", "newll.cancel");
  section.appendChild(cancelBtn);
  doc.register(cancelBtn);

  return { doc, form, section, applInput, dobInput, captcha, submitBtn, cancelBtn, outsideInput };
}

const VALID_LOCATION = {
  hostname: "sarathi.parivahan.gov.in",
  pathname: "/sarathiservice/newDL_displayPage.do",
};

const VALID_APP_STATUS_LOCATION = {
  hostname: "sarathi.parivahan.gov.in",
  pathname: "/sarathiservice/applViewStatus.do",
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
    true,
    "Should accept any path under sarathi.parivahan.gov.in"
  );
  assert.strictEqual(
    portalAdapter.validatePortalLocation({ hostname: "sarathi.parivahan.gov.in", pathname: "/" }),
    true,
    "Should accept root path under sarathi.parivahan.gov.in"
  );
  assert.strictEqual(
    portalAdapter.validatePortalLocation({ hostname: "evil.com", pathname: "/sarathiservice/newDL.do" }),
    false,
    "Should reject non-sarathi hosts"
  );
  assert.strictEqual(portalAdapter.validatePortalLocation(null), false);
  console.log("[PASS] 1. Location validation strictly checks sarathi.parivahan.gov.in host");

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

  const wrongHost2Loc = { hostname: "not-sarathi.com", pathname: "/newDL.do" };
  const wrongHost2Fill = portalAdapter.fillDlFields(doc, wrongHost2Loc, {
    caseId: "CASE-1", reviewVersion: 1, licenceNo: "TS09/0001234/2021", dob: "1994-06-25",
  });
  assert.strictEqual(wrongHost2Fill.ok, false);
  assert.match(wrongHost2Fill.error, /Active tab is not on the Sarathi portal/);

  section.style.display = "none";
  const hiddenFill = portalAdapter.fillDlFields(doc, VALID_LOCATION, {
    caseId: "CASE-1", reviewVersion: 1, licenceNo: "TS09/0001234/2021", dob: "1994-06-25",
  });
  assert.strictEqual(hiddenFill.ok, false);
  assert.match(hiddenFill.error, /section \(#lldetailsDisplay\) is currently hidden/);
  section.style.display = "block";
  console.log("[PASS] 12. Unsupported host, path, or hidden section prevents filling");

  // 13. Application Status Page Inspection on Valid DOM
  const appDom = buildValidAppStatusDom();
  const appInspect = portalAdapter.inspectAppStatusPage(appDom.doc, VALID_APP_STATUS_LOCATION);
  assert.strictEqual(appInspect.ready, true);
  assert.strictEqual(appInspect.applNumInput, appDom.applInput);
  assert.strictEqual(appInspect.dobInput, appDom.dobInput);
  console.log("[PASS] 13. Page inspection accepts valid Sarathi Application Status DOM");

  // 14. Section Scoping: Unrelated inputs outside #publicApplStatus ignored
  assert.notStrictEqual(appInspect.applNumInput, appDom.outsideInput);
  console.log("[PASS] 14. Targets are scoped strictly inside #publicApplStatus (unrelated #applNum ignored)");

  // 15. Section Visibility Requirement
  appDom.section.style.display = "none";
  const hiddenAppRes = portalAdapter.inspectAppStatusPage(appDom.doc, VALID_APP_STATUS_LOCATION);
  assert.strictEqual(hiddenAppRes.ready, false);
  assert.match(hiddenAppRes.reason, /section \(#publicApplStatus\) is currently hidden/);
  appDom.section.style.display = "block";
  console.log("[PASS] 15. Blocks filling when #publicApplStatus is hidden");

  // 16. Input Disabled / Read-Only / Ambiguity Rejection
  appDom.applInput.disabled = true;
  const disabledAppRes = portalAdapter.inspectAppStatusPage(appDom.doc, VALID_APP_STATUS_LOCATION);
  assert.strictEqual(disabledAppRes.ready, false);
  assert.match(disabledAppRes.reason, /input is disabled or read-only/);
  appDom.applInput.disabled = false;

  const dupAppl = new MockElement("input", "applNum", "applNum");
  appDom.section.appendChild(dupAppl);
  const ambigAppRes = portalAdapter.inspectAppStatusPage(appDom.doc, VALID_APP_STATUS_LOCATION);
  assert.strictEqual(ambigAppRes.ready, false);
  assert.match(ambigAppRes.reason, /Ambiguous DOM: found 2/);
  appDom.section.children.pop(); // remove duplicate
  console.log("[PASS] 16. Rejects disabled, read-only, or ambiguous inputs on Application Status");

  // 17. Application Status Autofill Execution & Event Dispatch
  const appFillRes = portalAdapter.fillAppStatusFields(appDom.doc, VALID_APP_STATUS_LOCATION, {
    caseId: "CASE-20261017-001",
    reviewVersion: 2,
    applicationNo: "3984089026",
    dob: "1994-06-25",
  });
  assert.strictEqual(appFillRes.ok, true);
  assert.strictEqual(appFillRes.filled.applicationNo, true);
  assert.strictEqual(appFillRes.filled.dob, true);
  assert.strictEqual(appDom.applInput.value, "3984089026");
  assert.strictEqual(appDom.dobInput.value, "25-06-1994");

  // Verify events dispatched
  assert.strictEqual(appDom.applInput.dispatchedEvents.length, 2);
  assert.strictEqual(appDom.applInput.dispatchedEvents[0].type, "input");
  assert.strictEqual(appDom.applInput.dispatchedEvents[1].type, "change");
  assert.strictEqual(appDom.dobInput.dispatchedEvents.length, 2);
  assert.strictEqual(appDom.dobInput.dispatchedEvents[0].type, "input");
  assert.strictEqual(appDom.dobInput.dispatchedEvents[1].type, "change");

  // Verify safety invariants: CAPTCHA and buttons were completely untouched
  assert.strictEqual(appDom.captcha.value, "");
  assert.strictEqual(appDom.captcha.dispatchedEvents.length, 0);
  assert.strictEqual(appDom.submitBtn.dispatchedEvents.length, 0);
  assert.strictEqual(appDom.cancelBtn.dispatchedEvents.length, 0);
  console.log("[PASS] 17. Successfully filled Application Status fields, dispatched events, and left CAPTCHA/buttons untouched");

  // 18. Read-back Verification & Partial Failure Handling
  Object.defineProperty(appDom.dobInput, "value", {
    get: () => "failed-val",
    set: () => {},
    configurable: true,
  });
  const simulatedAppPartial = portalAdapter.fillAppStatusFields(appDom.doc, VALID_APP_STATUS_LOCATION, {
    caseId: "CASE-20261017-001",
    reviewVersion: 2,
    applicationNo: "3984089026",
    dob: "1994-06-25",
  });
  assert.strictEqual(simulatedAppPartial.ok, false);
  assert.strictEqual(simulatedAppPartial.partial, true);
  assert.strictEqual(simulatedAppPartial.filled.applicationNo, true);
  assert.strictEqual(simulatedAppPartial.filled.dob, false);
  console.log("[PASS] 18. Honestly reports partial failures if read-back verification fails on Application Status");

  // Restore dobInput
  delete appDom.dobInput.value;
  appDom.dobInput.value = "";

  // 19. Command Metadata Validation
  assert.strictEqual(portalAdapter.validateAppStatusCommand(null).valid, false);
  assert.strictEqual(portalAdapter.validateAppStatusCommand({}).valid, false);
  assert.strictEqual(portalAdapter.validateAppStatusCommand({ caseId: "", reviewVersion: 1, applicationNo: "3984089026", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateAppStatusCommand({ caseId: "CASE-1", reviewVersion: 0, applicationNo: "3984089026", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateAppStatusCommand({ caseId: "CASE-1", reviewVersion: -1, applicationNo: "3984089026", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateAppStatusCommand({ caseId: "CASE-1", reviewVersion: "1", applicationNo: "3984089026", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateAppStatusCommand({ caseId: "CASE-1", reviewVersion: 1, applicationNo: "", dob: "1994-06-25" }).valid, false);
  assert.strictEqual(portalAdapter.validateAppStatusCommand({ caseId: "CASE-1", reviewVersion: 1, applicationNo: "3984089026", dob: "bad-date" }).valid, false);
  assert.strictEqual(portalAdapter.validateAppStatusCommand({ caseId: "CASE-1", reviewVersion: 1, applicationNo: "3984089026", dob: "1994-06-25" }).valid, true);
  console.log("[PASS] 19. Command metadata validation enforces caseId, reviewVersion, applicationNo, and dob");

  // 20. Invalid command metadata prevents Application Status fill execution
  const invalidAppFill = portalAdapter.fillAppStatusFields(appDom.doc, VALID_APP_STATUS_LOCATION, {
    caseId: "",
    reviewVersion: 1,
    applicationNo: "3984089026",
    dob: "1994-06-25",
  });
  assert.strictEqual(invalidAppFill.ok, false);
  assert.match(invalidAppFill.error, /caseId is required/);
  console.log("[PASS] 20. Invalid command metadata rejected before filling Application Status");

  // 21. Unsupported host, path, or hidden section prevents Application Status fill
  const wrongAppHostLoc = { hostname: "evil.com", pathname: "/sarathiservice/applViewStatus.do" };
  const wrongAppHostFill = portalAdapter.fillAppStatusFields(appDom.doc, wrongAppHostLoc, {
    caseId: "CASE-1", reviewVersion: 1, applicationNo: "3984089026", dob: "1994-06-25",
  });
  assert.strictEqual(wrongAppHostFill.ok, false);
  assert.match(wrongAppHostFill.error, /Active tab is not on the Sarathi portal/);

  const wrongAppHost2Loc = { hostname: "fake-sarathi.org", pathname: "/applViewStatus.do" };
  const wrongAppHost2Fill = portalAdapter.fillAppStatusFields(appDom.doc, wrongAppHost2Loc, {
    caseId: "CASE-1", reviewVersion: 1, applicationNo: "3984089026", dob: "1994-06-25",
  });
  assert.strictEqual(wrongAppHost2Fill.ok, false);
  assert.match(wrongAppHost2Fill.error, /Active tab is not on the Sarathi portal/);
  console.log("[PASS] 21. Unsupported host prevents filling Application Status");

  // 22. Cross-workflow isolation: DL Entry DOM does not satisfy Application Status and vice versa
  const dlDom = buildValidSarathiDom();
  const dlDomAppInspect = portalAdapter.inspectAppStatusPage(dlDom.doc, VALID_APP_STATUS_LOCATION);
  assert.strictEqual(dlDomAppInspect.ready, false);
  assert.match(dlDomAppInspect.reason, /#publicApplStatus.*not found/);

  const appDomDlInspect = portalAdapter.inspectDlPage(appDom.doc, VALID_LOCATION);
  assert.strictEqual(appDomDlInspect.ready, false);
  console.log("[PASS] 22. DL Entry and Application Status workflows remain strictly isolated");

  // 23. Flexible Smart Autofill: inspectPageForAutofill detects Application Status DOM
  const flexAppInspect = portalAdapter.inspectPageForAutofill(appDom.doc, VALID_APP_STATUS_LOCATION);
  assert.strictEqual(flexAppInspect.ready, true);
  assert.strictEqual(flexAppInspect.pageType, "app_status");
  const flexAppKeys = flexAppInspect.detectedFields.map((f) => f.key);
  assert.ok(flexAppKeys.includes("application_no"), "Should detect application_no input");
  assert.ok(flexAppKeys.includes("dob"), "Should detect dob input");
  console.log("[PASS] 23. inspectPageForAutofill accurately detects Application Status inputs");

  // 24. Flexible Smart Autofill: inspectPageForAutofill detects DL Entry DOM
  const flexDlInspect = portalAdapter.inspectPageForAutofill(dlDom.doc, VALID_LOCATION);
  assert.strictEqual(flexDlInspect.ready, true);
  assert.strictEqual(flexDlInspect.pageType, "dl_entry");
  const flexDlKeys = flexDlInspect.detectedFields.map((f) => f.key);
  assert.ok(flexDlKeys.includes("licence_no"), "Should detect licence_no input");
  assert.ok(flexDlKeys.includes("dob"), "Should detect dob input");
  console.log("[PASS] 24. inspectPageForAutofill accurately detects DL Entry inputs");

  // 25. Location validation rejects non-Sarathi domains in flexible inspection
  const flexEvilInspect = portalAdapter.inspectPageForAutofill(appDom.doc, { hostname: "evil.com", pathname: "/sarathiservice/" });
  assert.strictEqual(flexEvilInspect.ready, false);
  assert.match(flexEvilInspect.reason, /Active tab is not on the Sarathi portal/);
  console.log("[PASS] 25. inspectPageForAutofill rejects non-Sarathi domains");

  // 26. Safety check: isSafeInputField excludes CAPTCHA and action buttons
  assert.strictEqual(portalAdapter.isSafeInputField(appDom.captcha), false, "CAPTCHA must NEVER be targeted");
  assert.strictEqual(portalAdapter.isSafeInputField(appDom.submitBtn), false, "Submit button must NEVER be targeted");
  assert.strictEqual(portalAdapter.isSafeInputField(appDom.cancelBtn), false, "Cancel button must NEVER be targeted");
  assert.strictEqual(portalAdapter.isSafeInputField(appDom.applInput), true, "Valid text input should be safe");
  console.log("[PASS] 26. isSafeInputField strictly excludes CAPTCHA and buttons");

  // 27. autofillMatchedFields fills detected inputs, formats DOB, and performs readback verification
  const freshAppDom = buildValidAppStatusDom();
  const flexFillRes = portalAdapter.autofillMatchedFields(freshAppDom.doc, VALID_APP_STATUS_LOCATION, {
    caseId: "CASE-20261015-002",
    reviewVersion: 1,
    fields: {
      application_no: "20261015-002",
      dob: "2006-10-06",
    },
  });
  assert.strictEqual(flexFillRes.ok, true);
  assert.strictEqual(flexFillRes.partial, false);
  assert.strictEqual(freshAppDom.applInput.value, "20261015-002");
  assert.strictEqual(freshAppDom.dobInput.value, "06-10-2006");
  assert.strictEqual(freshAppDom.captcha.value, "", "CAPTCHA must remain empty");
  console.log("[PASS] 27. autofillMatchedFields fills matched fields and verifies read-back");

  // 28. autofillMatchedFields enforces caseId and positive reviewVersion
  const flexInvalidRes = portalAdapter.autofillMatchedFields(freshAppDom.doc, VALID_APP_STATUS_LOCATION, {
    caseId: "",
    reviewVersion: 0,
    fields: {},
  });
  assert.strictEqual(flexInvalidRes.ok, false);
  assert.match(flexInvalidRes.error, /caseId is required/);
  console.log("[PASS] 28. autofillMatchedFields validates command metadata");

  // 29. Hidden field exclusion & DL Entry DOB resolution
  const dlDomWithHidden = buildValidSarathiDom();
  const hiddenFname = new MockElement("input", "fname", "fname");
  hiddenFname.type = "hidden";
  dlDomWithHidden.doc.root.appendChild(hiddenFname);
  dlDomWithHidden.doc.register(hiddenFname);

  const hiddenDob = new MockElement("input", "dateOfBirth", "dateOfBirth");
  hiddenDob.type = "hidden";
  dlDomWithHidden.doc.root.appendChild(hiddenDob);
  dlDomWithHidden.doc.register(hiddenDob);

  assert.strictEqual(portalAdapter.isSafeInputField(hiddenFname), false, "Hidden inputs must be rejected by isSafeInputField");
  assert.strictEqual(portalAdapter.isSafeInputField(hiddenDob), false, "Hidden inputs must be rejected by isSafeInputField");

  const dlInspectWithHidden = portalAdapter.inspectPageForAutofill(dlDomWithHidden.doc, VALID_LOCATION);
  assert.strictEqual(dlInspectWithHidden.ready, true);
  const detectedKeys = dlInspectWithHidden.detectedFields.map((f) => f.key);
  assert.strictEqual(detectedKeys.includes("full_name"), false, "full_name must NOT be detected");
  const dobDetected = dlInspectWithHidden.detectedFields.find((f) => f.key === "dob");
  assert.ok(dobDetected, "dob must be detected");
  assert.strictEqual(dobDetected.id, "DOB", "Must detect visible #DOB, NOT hidden #dateOfBirth");

  const fillResDl = portalAdapter.autofillMatchedFields(dlDomWithHidden.doc, VALID_LOCATION, {
    caseId: "CASE-20261015-002",
    reviewVersion: 1,
    fields: {
      licence_no: "TG305/0008413/2026",
      dob: "2006-10-06",
      full_name: "MIDATHAPALLY SAIRAM",
    },
  });
  assert.strictEqual(fillResDl.ok, true);
  assert.strictEqual(dlDomWithHidden.llInput.value, "TG305/0008413/2026");
  assert.strictEqual(dlDomWithHidden.targetDob.value, "06-10-2006", "Visible #DOB must be autofilled");
  assert.strictEqual(hiddenDob.value, "", "Hidden dateOfBirth must remain untouched");
  assert.strictEqual(hiddenFname.value, "", "Hidden fname must remain untouched");
  console.log("[PASS] 29. Hidden field exclusion guarantees visible #DOB is autofilled and hidden fields are ignored");

  // 30. classifyInputElement accurately handles diverse Sarathi DOM shapes
  const testDom = new MockDocument();
  // Upload Document shape: id="applicationNum", placeholder="Enter ApplicationNumber"
  const uploadApplInput = new MockElement("input", "applicationNum", "applicationNum");
  uploadApplInput.placeholder = "Enter ApplicationNumber";
  uploadApplInput.title = "Application Number in numerics only";
  testDom.root.appendChild(uploadApplInput);
  testDom.register(uploadApplInput);
  assert.strictEqual(portalAdapter.classifyInputElement(uploadApplInput, testDom), "application_no");

  // Print Form shape: name="applno", id="applNum"
  const printApplInput = new MockElement("input", "applNum", "applno");
  printApplInput.placeholder = "Application Number";
  testDom.root.appendChild(printApplInput);
  testDom.register(printApplInput);
  assert.strictEqual(portalAdapter.classifyInputElement(printApplInput, testDom), "application_no");

  // NIC shorthand shape: id="applnNo", name="txtApplnNo"
  const nicApplInput = new MockElement("input", "applnNo", "txtApplnNo");
  testDom.root.appendChild(nicApplInput);
  testDom.register(nicApplInput);
  assert.strictEqual(portalAdapter.classifyInputElement(nicApplInput, testDom), "application_no");

  // Bootstrap row shape: Sibling div has label "Application Number:"
  const rowDiv = new MockElement("div", "", "");
  rowDiv.className = "row form-group";
  const labelCol = new MockElement("div", "", "");
  labelCol.className = "col-md-2 labeled control-label";
  labelCol.textContent = "Application Number:";
  const inputCol = new MockElement("div", "", "");
  inputCol.className = "col-md-2";
  const unbrandedInput = new MockElement("input", "genericField123", "genericField123");
  inputCol.appendChild(unbrandedInput);
  rowDiv.appendChild(labelCol);
  rowDiv.appendChild(inputCol);
  testDom.root.appendChild(rowDiv);
  testDom.register(unbrandedInput);
  assert.strictEqual(portalAdapter.classifyInputElement(unbrandedInput, testDom), "application_no");

  // Table row shape: Sibling td has label "Date of Birth"
  const tableTr = new MockElement("tr", "", "");
  const tdLabel = new MockElement("td", "", "");
  tdLabel.textContent = "Date of Birth (dd-mm-yyyy)";
  const tdInput = new MockElement("td", "", "");
  const unbrandedDob = new MockElement("input", "genericField456", "genericField456");
  tdInput.appendChild(unbrandedDob);
  tableTr.appendChild(tdLabel);
  tableTr.appendChild(tdInput);
  testDom.root.appendChild(tableTr);
  testDom.register(unbrandedDob);
  assert.strictEqual(portalAdapter.classifyInputElement(unbrandedDob, testDom), "dob");

  // Negative checks: Captcha and Mobile inputs strictly rejected
  const captchaInput = new MockElement("input", "entcaptxt", "entcaptxt");
  captchaInput.placeholder = "Enter Captcha";
  assert.strictEqual(portalAdapter.classifyInputElement(captchaInput, testDom), null);
  const mobileInput = new MockElement("input", "mobileNo", "mobileNo");
  mobileInput.placeholder = "Enter Mobile Number";
  assert.strictEqual(portalAdapter.classifyInputElement(mobileInput, testDom), null);
  console.log("[PASS] 30. classifyInputElement accurately classifies diverse Sarathi inputs and rejects non-targets");

  // 31. inspectPageForAutofill discovers both application_no and dob on generic Sarathi service
  const serviceDom = new MockDocument();
  const servRow = new MockElement("div", "serviceRow", "");
  servRow.className = "row";
  const servAppl = new MockElement("input", "applicationNum", "applicationNum");
  servAppl.placeholder = "Enter ApplicationNumber";
  const servDob = new MockElement("input", "dateOfBirth", "dateOfBirth");
  servDob.placeholder = "dd-mm-yyyy";
  servRow.appendChild(servAppl);
  servRow.appendChild(servDob);
  serviceDom.root.appendChild(servRow);
  serviceDom.register(servAppl);
  serviceDom.register(servDob);

  const servInspect = portalAdapter.inspectPageForAutofill(serviceDom, {
    hostname: "sarathi.parivahan.gov.in",
    pathname: "/sarathiservice/uploadMain.do",
  });
  assert.strictEqual(servInspect.ready, true);
  const servKeys = servInspect.detectedFields.map((f) => f.key);
  assert.ok(servKeys.includes("application_no"), "Must detect application_no on uploadMain.do");
  assert.ok(servKeys.includes("dob"), "Must detect dob on uploadMain.do");
  console.log("[PASS] 31. inspectPageForAutofill discovers fields on general Sarathi services");

  // 32. autofillMatchedFields fills diverse service inputs and dispatches lifecycle events
  const servFillRes = portalAdapter.autofillMatchedFields(serviceDom, {
    hostname: "sarathi.parivahan.gov.in",
    pathname: "/sarathiservice/uploadMain.do",
  }, {
    caseId: "CASE-20261015-002",
    reviewVersion: 2,
    fields: {
      application_no: "3984089026",
      dob: "2006-10-06",
    },
  });
  assert.strictEqual(servFillRes.ok, true);
  assert.strictEqual(servAppl.value, "3984089026");
  assert.strictEqual(servDob.value, "06-10-2006");
  assert.strictEqual(servAppl.focused, true);
  assert.strictEqual(servAppl.blurred, true);
  const applEvents = servAppl.dispatchedEvents.map((e) => e.type);
  assert.ok(applEvents.includes("input"));
  assert.ok(applEvents.includes("change"));
  console.log("[PASS] 32. autofillMatchedFields fills diverse service inputs with full event lifecycle");

  // 33. Common case normalized checking across learning licence, dob, and application number
  const { normalizeIdentifier, CANONICAL_FIELD_IDENTIFIERS, classifyInputElement } = portalAdapter;

  assert.strictEqual(normalizeIdentifier("appNumber"), "appnumber");
  assert.strictEqual(normalizeIdentifier("APP_NUMBER"), "appnumber");
  assert.strictEqual(normalizeIdentifier("txt-app-no"), "txtappno");
  assert.strictEqual(normalizeIdentifier("learning_licence"), "learninglicence");
  assert.strictEqual(normalizeIdentifier("DateOfBirth"), "dateofbirth");

  assert.ok(CANONICAL_FIELD_IDENTIFIERS.application_no.has("appnumber"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.application_no.has("applicationnumber"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("learninglicence"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("learninglicense"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("learnerslicence"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.dob.has("dateofbirth"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.dob.has("dob"));

  const casingDom = new MockDocument();

  // Learning Licence casing variations
  const llVariants = ["learningLicense", "learning_licence", "LEARNINGLICENCE", "ll_number", "txtLLNo", "learnersLicence", "learner_licence"];
  for (const id of llVariants) {
    const el = new MockElement("input", id, id);
    assert.strictEqual(classifyInputElement(el, casingDom), "licence_no", `Failed on licence id: ${id}`);
  }

  // Date of Birth casing variations
  const dobVariants = ["dateofbirth", "DateOfBirth", "DATE_OF_BIRTH", "applicant_dob", "txt_dob", "applDateOfBirth"];
  for (const id of dobVariants) {
    const el = new MockElement("input", id, id);
    assert.strictEqual(classifyInputElement(el, casingDom), "dob", `Failed on dob id: ${id}`);
  }

  // Application Number casing variations
  const applVariants = ["appnumber", "appNumber", "APP_NUMBER", "txtApplnNo", "appln_num", "application_ref_no", "txt_appl_no"];
  for (const id of applVariants) {
    const el = new MockElement("input", id, id);
    assert.strictEqual(classifyInputElement(el, casingDom), "application_no", `Failed on appl id: ${id}`);
  }

  console.log("[PASS] 33. Common case normalized checking handles arbitrary casing and delimiter variants");

  // 34. licNo and expanded edge cases across licence_no, application_no, and dob
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("licno"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("licnumber"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("licnum"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("txtlicno"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("curlicno"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("dlnum"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.licence_no.has("llnum"));

  assert.ok(CANONICAL_FIELD_IDENTIFIERS.application_no.has("regno"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.application_no.has("transid"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.application_no.has("applcode"));

  assert.ok(CANONICAL_FIELD_IDENTIFIERS.dob.has("persondob"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.dob.has("candidatedob"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.dob.has("holderdob"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.dob.has("applicantbirthdate"));
  assert.ok(CANONICAL_FIELD_IDENTIFIERS.dob.has("txtbirthdate"));

  // Expanded licence_no classifications (including Mobile Number Update service 'licNo')
  const licVariants = ["licNo", "licno", "licNumber", "licnumber", "licNum", "licnum", "txtLicNo", "txtlicno", "curLicNo", "dlnum", "llnum"];
  for (const id of licVariants) {
    const el = new MockElement("input", id, id);
    assert.strictEqual(classifyInputElement(el, casingDom), "licence_no", `Failed on lic variant: ${id}`);
  }

  // Expanded application_no classifications
  const applMoreVariants = ["regNo", "regno", "regNum", "transId", "applCode", "applTransId", "fileNo"];
  for (const id of applMoreVariants) {
    const el = new MockElement("input", id, id);
    assert.strictEqual(classifyInputElement(el, casingDom), "application_no", `Failed on appl variant: ${id}`);
  }

  // Expanded dob classifications
  const dobMoreVariants = ["personDob", "persondob", "candidateDob", "holderDob", "applicantBirthDate", "txtBirthDate", "dobVal"];
  for (const id of dobMoreVariants) {
    const el = new MockElement("input", id, id);
    assert.strictEqual(classifyInputElement(el, casingDom), "dob", `Failed on dob variant: ${id}`);
  }

  // Simulated Mobile Number Update service DOM with licNo
  const mobileServiceDom = new MockDocument();
  const mobLicInput = new MockElement("input", "licNo", "licNo");
  mobLicInput.placeholder = "Enter Driving Licence Number";
  const mobDobInput = new MockElement("input", "dob", "dob");
  const mobCapInput = new MockElement("input", "entcaptxt", "entcaptxt");
  mobileServiceDom.root.appendChild(mobLicInput);
  mobileServiceDom.root.appendChild(mobDobInput);
  mobileServiceDom.root.appendChild(mobCapInput);
  mobileServiceDom.register(mobLicInput);
  mobileServiceDom.register(mobDobInput);
  mobileServiceDom.register(mobCapInput);

  const mobInspect = portalAdapter.inspectPageForAutofill(mobileServiceDom, {
    hostname: "sarathi.parivahan.gov.in",
    pathname: "/sarathiservice/mobileUpdate.do",
  });
  assert.strictEqual(mobInspect.ready, true);
  const mobKeys = mobInspect.detectedFields.map((f) => f.key);
  assert.ok(mobKeys.includes("licence_no"), "Mobile Update service must detect licence_no via licNo");
  assert.ok(mobKeys.includes("dob"), "Mobile Update service must detect dob");
  assert.ok(!mobKeys.includes("captcha"), "Mobile Update service must never target captcha");

  const mobFillRes = portalAdapter.autofillMatchedFields(mobileServiceDom, {
    hostname: "sarathi.parivahan.gov.in",
    pathname: "/sarathiservice/mobileUpdate.do",
  }, {
    caseId: "CASE-20261015-001",
    reviewVersion: 1,
    fields: {
      licence_no: "RJ14/2026/0001234",
      dob: "2005-08-15",
    },
  });
  assert.strictEqual(mobFillRes.ok, true);
  assert.strictEqual(mobLicInput.value, "RJ14/2026/0001234");
  assert.strictEqual(mobDobInput.value, "15-08-2005");
  assert.strictEqual(mobCapInput.value, "", "CAPTCHA must remain untouched");
  console.log("[PASS] 34. licNo and expanded edge cases accurately discovered and filled on Sarathi services");

  // 35. Label-to-Input Association Engine
  const { findInputsByLabels, resolveInputFromLabel, LABEL_FIELD_PATTERNS } = portalAdapter;
  assert.ok(typeof findInputsByLabels === "function");
  assert.ok(typeof resolveInputFromLabel === "function");
  assert.ok(Array.isArray(LABEL_FIELD_PATTERNS));

  const labelDom = new MockDocument();

  // Pattern A: Bootstrap grid layout with unbranded input ID
  // <div class="row">
  //   <div class="col-md-3 labeled"><label>Driving Licence Number</label></div>
  //   <div class="col-md-5"><input name="struts_random_fld_101"></div>
  // </div>
  const bsRow = new MockElement("div");
  bsRow.className = "row form-group";
  const bsColLabel = new MockElement("div");
  bsColLabel.className = "col-md-3 labeled";
  const bsLabel = new MockElement("label");
  bsLabel.textContent = "Driving Licence Number :";
  bsColLabel.appendChild(bsLabel);
  const bsColInput = new MockElement("div");
  bsColInput.className = "col-md-5";
  const unbrandedLicInput = new MockElement("input", "", "struts_random_fld_101");
  bsColInput.appendChild(unbrandedLicInput);
  bsRow.appendChild(bsColLabel);
  bsRow.appendChild(bsColInput);
  labelDom.root.appendChild(bsRow);

  // Pattern B: Table cell layout with unbranded input ID
  // <tr>
  //   <td>Date of Birth (DD-MM-YYYY)</td>
  //   <td><input name="struts_dob_token_999"></td>
  // </tr>
  const tr = new MockElement("tr");
  const tblTdLabel = new MockElement("td");
  tblTdLabel.textContent = "Date of Birth :";
  const tblTdInput = new MockElement("td");
  const unbrandedDobInput = new MockElement("input", "", "struts_dob_token_999");
  tblTdInput.appendChild(unbrandedDobInput);
  tr.appendChild(tblTdLabel);
  tr.appendChild(tblTdInput);
  labelDom.root.appendChild(tr);

  // Pattern C: Enclosed label layout with unbranded input ID
  // <label>Application Number: <input name="unbranded_appl_seq_42"></label>
  const enclosedLabel = new MockElement("label");
  enclosedLabel.textContent = "Application Number :";
  const unbrandedApplInput = new MockElement("input", "", "unbranded_appl_seq_42");
  enclosedLabel.appendChild(unbrandedApplInput);
  labelDom.root.appendChild(enclosedLabel);

  // Pattern D: CAPTCHA safety invariant - must never be paired with any field
  const capRow = new MockElement("tr");
  const capLabel = new MockElement("td");
  capLabel.textContent = "Enter Captcha";
  const capInputCell = new MockElement("td");
  const capInputField = new MockElement("input", "entcaptxt", "entcaptxt");
  capInputCell.appendChild(capInputField);
  capRow.appendChild(capLabel);
  capRow.appendChild(capInputCell);
  labelDom.root.appendChild(capRow);

  // Direct resolution tests
  const licResolved = resolveInputFromLabel(bsLabel, labelDom);
  assert.strictEqual(licResolved, unbrandedLicInput, "Should resolve licence input from Bootstrap column label");

  const dobResolved = resolveInputFromLabel(tblTdLabel, labelDom);
  assert.strictEqual(dobResolved, unbrandedDobInput, "Should resolve dob input from table cell label");

  const applResolved = resolveInputFromLabel(enclosedLabel, labelDom);
  assert.strictEqual(applResolved, unbrandedApplInput, "Should resolve application_no from enclosed label");

  const capResolved = resolveInputFromLabel(capLabel, labelDom);
  assert.strictEqual(capResolved, null, "CAPTCHA input must NEVER be resolved from label");

  // Whole document label scan
  const labelMatches = findInputsByLabels(labelDom);
  assert.strictEqual(labelMatches.length, 3, "Must discover all 3 fields via label association");
  const labelKeys = labelMatches.map((m) => m.key);
  assert.ok(labelKeys.includes("licence_no"));
  assert.ok(labelKeys.includes("dob"));
  assert.ok(labelKeys.includes("application_no"));
  for (const m of labelMatches) {
    assert.strictEqual(m.matchedBy, "label_association");
  }

  // End-to-end inspectPageForAutofill via Tier 2 Label Engine
  const labelInspect = portalAdapter.inspectPageForAutofill(labelDom, {
    hostname: "sarathi.parivahan.gov.in",
    pathname: "/sarathiservice/anyService.do",
  });
  assert.strictEqual(labelInspect.ready, true);
  assert.strictEqual(labelInspect.detectedFields.length, 3);

  // End-to-end autofill on label-resolved unbranded inputs
  const labelFillRes = portalAdapter.autofillMatchedFields(labelDom, {
    hostname: "sarathi.parivahan.gov.in",
    pathname: "/sarathiservice/anyService.do",
  }, {
    caseId: "CASE-20261015-003",
    reviewVersion: 3,
    fields: {
      licence_no: "MH12/2026/0009876",
      dob: "2004-12-25",
      application_no: "9988776655",
    },
  });
  assert.strictEqual(labelFillRes.ok, true);
  assert.strictEqual(unbrandedLicInput.value, "MH12/2026/0009876");
  assert.strictEqual(unbrandedDobInput.value, "25-12-2004");
  assert.strictEqual(unbrandedApplInput.value, "9988776655");
  assert.strictEqual(capInputField.value, "", "CAPTCHA must remain empty");
  console.log("[PASS] 35. Label-to-Input Association Engine discovers and fills unbranded inputs across tables, grids, and containers");

  // 36. DL vs LL Number Semantics Regression Suite (Phase 7C)
  {
    const { getLicenceTargetType } = portalAdapter;
    assert.ok(typeof getLicenceTargetType === "function");

    // 36.1 Target Classification Unit Checks
    assert.strictEqual(getLicenceTargetType("#learningLicence"), "ll");
    assert.strictEqual(getLicenceTargetType("#lldetailsDisplay input#learningLicence"), "ll");
    assert.strictEqual(getLicenceTargetType("#txtLLNo"), "ll");
    assert.strictEqual(getLicenceTargetType("#llno"), "ll");
    assert.strictEqual(getLicenceTargetType("#drivingLicenceNo"), "dl");
    assert.strictEqual(getLicenceTargetType("#dlno"), "dl");
    assert.strictEqual(getLicenceTargetType("#txtDLNo"), "dl");
    assert.strictEqual(getLicenceTargetType("#licNo"), "generic");
    assert.strictEqual(getLicenceTargetType("#licenceNo"), "generic");
    assert.strictEqual(getLicenceTargetType("", null, null, "Learner's Licence Number"), "ll");
    assert.strictEqual(getLicenceTargetType("", null, null, "Driving Licence Number"), "dl");
    assert.strictEqual(getLicenceTargetType("", null, null, "Licence Number"), "generic");

    // 36.2 LL source number into #learningLicence (ALLOWED)
    const llDom = new MockDocument();
    const llSection = new MockElement("div", "lldetailsDisplay");
    const llInput = new MockElement("input", "learningLicence", "learningLicence");
    llSection.appendChild(llInput);
    llDom.root.appendChild(llSection);
    const llFillRes = portalAdapter.autofillMatchedFields(llDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/displayDLPage.do",
    }, {
      caseId: "CASE-20261017-LL01",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "TS09/LL/1234567/2021",
        licence_type: "ll",
      },
    });
    assert.strictEqual(llFillRes.ok, true, "LL source number must fill #learningLicence");
    assert.strictEqual(llInput.value, "TS09/LL/1234567/2021");

    // 36.3 DL source number into a clearly labelled Driving Licence field on a general service page (ALLOWED)
    const dlDom = new MockDocument();
    const dlRow = new MockElement("div");
    dlRow.className = "form-group";
    const dlLabel = new MockElement("label");
    dlLabel.textContent = "Driving Licence Number";
    const dlInput = new MockElement("input", "dlno", "dlno");
    dlRow.appendChild(dlLabel);
    dlRow.appendChild(dlInput);
    dlDom.root.appendChild(dlRow);
    const dlFillRes = portalAdapter.autofillMatchedFields(dlDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/mobileUpdate.do",
    }, {
      caseId: "CASE-20261017-DL01",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "DL-0420110012345",
        licence_type: "dl",
      },
    });
    assert.strictEqual(dlFillRes.ok, true, "DL source number must fill clearly labelled DL field");
    assert.strictEqual(dlInput.value, "DL-0420110012345");

    // 36.4 DL source number NOT entering #learningLicence (STRICTLY BLOCKED)
    const dlIntoLlDom = new MockDocument();
    const dlIntoLlSection = new MockElement("div", "lldetailsDisplay");
    const dlIntoLlInput = new MockElement("input", "learningLicence", "learningLicence");
    const dlIntoLlDob = new MockElement("input", "DOB", "DOB");
    dlIntoLlSection.appendChild(dlIntoLlInput);
    dlIntoLlSection.appendChild(dlIntoLlDob);
    dlIntoLlDom.root.appendChild(dlIntoLlSection);
    const dlIntoLlRes = portalAdapter.autofillMatchedFields(dlIntoLlDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/displayDLPage.do",
    }, {
      caseId: "CASE-20261017-DL02",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "DL-0420110012345",
        licence_type: "dl",
        dob: "1994-06-25",
      },
    });
    assert.strictEqual(dlIntoLlInput.value, "", "DL source number must NEVER enter #learningLicence");
    assert.strictEqual(dlIntoLlDob.value, "25-06-1994", "DOB must still autofill even when DL number is blocked from LL field");
    assert.ok(dlIntoLlRes.errors.some((e) => e.key === "licence_no" && e.error.includes("Cannot fill Driving Licence number into a Learner's Licence field")));

    // 36.5 LL source number NOT entering an explicitly DL-only field (STRICTLY BLOCKED)
    const llIntoDlDom = new MockDocument();
    const llIntoDlInput = new MockElement("input", "drivingLicenceNo", "drivingLicenceNo");
    llIntoDlDom.root.appendChild(llIntoDlInput);
    const llIntoDlRes = portalAdapter.autofillMatchedFields(llIntoDlDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/dlServices.do",
    }, {
      caseId: "CASE-20261017-LL02",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "TS09/LL/1234567/2021",
        licence_type: "ll",
      },
    });
    assert.strictEqual(llIntoDlRes.ok, false, "Autofill must fail when LL is targeted to DL-only field");
    assert.strictEqual(llIntoDlInput.value, "", "LL source number must NEVER enter explicitly DL-only field");
    assert.ok(llIntoDlRes.errors.some((e) => e.key === "licence_no" && e.error.includes("Cannot fill Learner's Licence number into a Driving Licence field")));

    // 36.6 Generic licence-number field with adequate source/service context (ALLOWED for both)
    const genDom = new MockDocument();
    const genInput = new MockElement("input", "licNo", "licNo");
    genDom.root.appendChild(genInput);
    // Fill DL into generic
    const genDlRes = portalAdapter.autofillMatchedFields(genDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/changeAddress.do",
    }, {
      caseId: "CASE-20261017-DL03",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "DL-0420110012345",
        licence_type: "dl",
      },
    });
    assert.strictEqual(genDlRes.ok, true);
    assert.strictEqual(genInput.value, "DL-0420110012345");
    // Fill LL into generic
    genInput.value = "";
    const genLlRes = portalAdapter.autofillMatchedFields(genDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/changeAddress.do",
    }, {
      caseId: "CASE-20261017-LL03",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "TS09/LL/1234567/2021",
        licence_type: "ll",
      },
    });
    assert.strictEqual(genLlRes.ok, true);
    assert.strictEqual(genInput.value, "TS09/LL/1234567/2021");

    // 36.7 Unknown source type without pretending it is DL or LL (BLOCKED from DL or LL, ALLOWED on generic)
    // Blocked from LL:
    llInput.value = "";
    const unkLlRes = portalAdapter.autofillMatchedFields(llDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/displayDLPage.do",
    }, {
      caseId: "CASE-20261017-UNK01",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "UNK-99887766",
        licence_type: "unknown",
      },
    });
    assert.strictEqual(unkLlRes.ok, false);
    assert.strictEqual(llInput.value, "", "Unknown licence type must NOT enter #learningLicence");
    assert.ok(unkLlRes.errors.some((e) => e.key === "licence_no" && e.error.includes("unknown source type")));

    // Blocked from DL:
    dlInput.value = "";
    const unkDlRes = portalAdapter.autofillMatchedFields(dlDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/mobileUpdate.do",
    }, {
      caseId: "CASE-20261017-UNK02",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "UNK-99887766",
        licence_type: "unknown",
      },
    });
    assert.strictEqual(unkDlRes.ok, false);
    assert.strictEqual(dlInput.value, "", "Unknown licence type must NOT enter DL field");

    // Allowed on generic:
    genInput.value = "";
    const unkGenRes = portalAdapter.autofillMatchedFields(genDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/changeAddress.do",
    }, {
      caseId: "CASE-20261017-UNK03",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "UNK-99887766",
        licence_type: "unknown",
      },
    });
    assert.strictEqual(unkGenRes.ok, true);
    assert.strictEqual(genInput.value, "UNK-99887766");

    // 36.8 Existing cases that have only licence_no and no type metadata (PRESERVED)
    llInput.value = "";
    const legacyLlRes = portalAdapter.autofillMatchedFields(llDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/displayDLPage.do",
    }, {
      caseId: "CASE-LEGACY-001",
      reviewVersion: 1,
      fieldValues: {
        licence_no: "LEGACY-LIC-001",
        // licence_type omitted
      },
    });
    assert.strictEqual(legacyLlRes.ok, true, "Legacy cases without licence_type must continue to autofill");
    assert.strictEqual(llInput.value, "LEGACY-LIC-001");

    // 36.9 Existing application-number and DOB matching preserved
    const appDobDom = new MockDocument();
    const appInput = new MockElement("input", "applNum", "applNum");
    const dobInput = new MockElement("input", "dateOfBirth", "dateOfBirth");
    appDobDom.root.appendChild(appInput);
    appDobDom.root.appendChild(dobInput);
    const appDobRes = portalAdapter.autofillMatchedFields(appDobDom, {
      hostname: "sarathi.parivahan.gov.in",
      pathname: "/sarathiservice/applViewStatus.do",
    }, {
      caseId: "CASE-20261017-APP01",
      reviewVersion: 1,
      fieldValues: {
        application_no: "2847593021",
        dob: "1990-11-05",
      },
    });
    assert.strictEqual(appDobRes.ok, true);
    assert.strictEqual(appInput.value, "2847593021");
    assert.strictEqual(dobInput.value, "05-11-1990");

    console.log("[PASS] 36. DL vs LL Number Semantics Regression Suite passes all 9 criteria (strict separation, unknown safety, legacy compatibility)");
  }

  console.log("\nAll Portal Adapter tests passed successfully (36/36)!\n");
}

runTests();
