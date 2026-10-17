/**
 * FormFlow Extension Popup Controller (Phase 7B)
 *
 * Coordinates extension settings, case selection, workflow routing,
 * operator-confirmed fill packet preview, and assisted autofill on Sarathi DL Entry.
 *
 * Invariants:
 * - Never prints token or applicant field values to console.
 * - Never persists applicant values in chrome.storage.
 * - Strictly section-scoped DL entry targeting (#lldetailsDisplay).
 * - Never clicks OK/Submit, never touches CAPTCHA, never bypasses human verification.
 */

(function () {
  "use strict";

  // Elements (bound during DOM initialization)
  let connIndicator,
    connLabel,
    toggleSettingsBtn,
    closeSettingsBtn,
    settingsCard,
    apiBaseUrlInput,
    apiTokenInput,
    saveSettingsBtn,
    testConnBtn,
    settingsAlert,
    globalAlert,
    caseSelect,
    refreshCasesBtn,
    caseMetaBanner,
    caseStatusBadge,
    caseVersionBadge,
    caseDateBadge,
    workflowSelect,
    previewWorkflowTag,
    fillActionSection,
    previewActionLicence,
    previewActionDob,
    fillDlBtn,
    fillAlert,
    previewLoading,
    previewEmpty,
    unsupportedNotice,
    unsupportedTitle,
    unsupportedDesc,
    previewError,
    previewErrorMessage,
    previewContent,
    fieldsTableBody;

  function bindElements() {
    if (typeof document === "undefined") return;
    connIndicator = document.getElementById("connIndicator");
    connLabel = document.getElementById("connLabel");
    toggleSettingsBtn = document.getElementById("toggleSettingsBtn");
    closeSettingsBtn = document.getElementById("closeSettingsBtn");
    settingsCard = document.getElementById("settingsCard");
    apiBaseUrlInput = document.getElementById("apiBaseUrl");
    apiTokenInput = document.getElementById("apiToken");
    saveSettingsBtn = document.getElementById("saveSettingsBtn");
    testConnBtn = document.getElementById("testConnBtn");
    settingsAlert = document.getElementById("settingsAlert");
    globalAlert = document.getElementById("globalAlert");

    caseSelect = document.getElementById("caseSelect");
    refreshCasesBtn = document.getElementById("refreshCasesBtn");
    caseMetaBanner = document.getElementById("caseMetaBanner");
    caseStatusBadge = document.getElementById("caseStatusBadge");
    caseVersionBadge = document.getElementById("caseVersionBadge");
    caseDateBadge = document.getElementById("caseDateBadge");

    workflowSelect = document.getElementById("workflowSelect");
    previewWorkflowTag = document.getElementById("previewWorkflowTag");

    fillActionSection = document.getElementById("fillActionSection");
    previewActionLicence = document.getElementById("previewActionLicence");
    previewActionDob = document.getElementById("previewActionDob");
    fillDlBtn = document.getElementById("fillDlBtn");
    fillAlert = document.getElementById("fillAlert");

    previewLoading = document.getElementById("previewLoading");
    previewEmpty = document.getElementById("previewEmpty");
    unsupportedNotice = document.getElementById("unsupportedWorkflowNotice");
    unsupportedTitle = document.getElementById("unsupportedTitle");
    unsupportedDesc = document.getElementById("unsupportedDesc");
    previewError = document.getElementById("previewError");
    previewErrorMessage = document.getElementById("previewErrorMessage");
    previewContent = document.getElementById("previewContent");
    fieldsTableBody = document.getElementById("fieldsTableBody");
  }

  // State
  let apiClient = null;
  let cachedCases = [];
  let selectedCaseId = "";
  let selectedWorkflow = "dl_application";
  let currentPacket = null;
  let pageReadyForDl = false;
  let pageReadyReason = "";
  let requestSeq = 0;

  // Workflow definitions (Phase 7B)
  const WORKFLOW_META = {
    dl_application: {
      label: "DL Entry",
      supported: true,
      tag: "dl_application",
    },
    ll_application: {
      label: "LL Application",
      supported: false,
      tag: "ll_application",
      plannedPhase: "Future Phase",
    },
    app_status: {
      label: "Application Status",
      supported: false,
      tag: "app_status",
      plannedPhase: "Future Phase",
    },
    print_form: {
      label: "Print Form",
      supported: false,
      tag: "print_form",
      plannedPhase: "Future Phase",
    },
    payment_status: {
      label: "Payment Status",
      supported: false,
      tag: "payment_status",
      plannedPhase: "Future Phase",
    },
  };

  /**
   * Escape HTML to prevent injection in preview table.
   */
  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  /**
   * Safe storage access abstraction (handles both Chrome extension and mock/node environments).
   */
  function getStorage(keys) {
    return new Promise((resolve) => {
      if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.local) {
        chrome.storage.local.get(keys, resolve);
      } else {
        const out = {};
        for (const k of keys) {
          out[k] = (typeof localStorage !== "undefined" && localStorage.getItem(`formflow_${k}`)) || "";
        }
        resolve(out);
      }
    });
  }

  function setStorage(items) {
    return new Promise((resolve) => {
      if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.local) {
        chrome.storage.local.set(items, resolve);
      } else {
        if (typeof localStorage !== "undefined") {
          for (const [k, v] of Object.entries(items)) {
            localStorage.setItem(`formflow_${k}`, v);
          }
        }
        resolve();
      }
    });
  }

  /**
   * Update Connection Status Badge
   */
  function setConnectionStatus(status, text) {
    if (!connIndicator) return;
    connIndicator.className = `conn-badge conn-${status}`;
    connLabel.textContent = text;
  }


  /**
   * Show/Hide Alerts
   */
  function showGlobalAlert(message, type = "error") {
    if (!globalAlert) return;
    globalAlert.className = `alert alert-${type}`;
    globalAlert.textContent = message;
    globalAlert.classList.remove("hidden");
  }

  function hideGlobalAlert() {
    if (!globalAlert) return;
    globalAlert.classList.add("hidden");
    globalAlert.textContent = "";
  }

  function showSettingsAlert(message, type = "error") {
    if (!settingsAlert) return;
    settingsAlert.className = `alert alert-${type}`;
    settingsAlert.textContent = message;
    settingsAlert.classList.remove("hidden");
  }

  function hideSettingsAlert() {
    if (!settingsAlert) return;
    settingsAlert.classList.add("hidden");
    settingsAlert.textContent = "";
  }

  function showFillAlert(message, type = "error") {
    if (!fillAlert) return;
    fillAlert.className = `alert alert-${type}`;
    fillAlert.textContent = message;
    fillAlert.classList.remove("hidden");
  }

  function hideFillAlert() {
    if (!fillAlert) return;
    fillAlert.classList.add("hidden");
    fillAlert.textContent = "";
  }


  /**
   * Check active tab readiness using content script CHECK_DL_PAGE.
   */
  function checkPageReadiness(callback) {
    if (typeof chrome === "undefined" || !chrome.tabs || !chrome.tabs.query) {
      pageReadyForDl = false;
      pageReadyReason = "Browser tab API unavailable.";
      if (callback) callback(false, pageReadyReason, null);
      updateFillActionSection(currentPacket);
      return;
    }

    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs || tabs.length === 0) {
        pageReadyForDl = false;
        pageReadyReason = "No active browser tab found.";
        if (callback) callback(false, pageReadyReason, null);
        updateFillActionSection(currentPacket);
        return;
      }

      const tab = tabs[0];
      chrome.tabs.sendMessage(tab.id, { action: "CHECK_DL_PAGE" }, (response) => {
        if (chrome.runtime.lastError || !response || !response.ok) {
          pageReadyForDl = false;
          pageReadyReason = "Active tab is not on Sarathi DL Entry page.";
        } else if (!response.ready) {
          pageReadyForDl = false;
          pageReadyReason = response.reason || "Sarathi DL Entry section is not ready.";
        } else {
          pageReadyForDl = true;
          pageReadyReason = "";
        }
        if (callback) callback(pageReadyForDl, pageReadyReason, tab);
        updateFillActionSection(currentPacket);
      });
    });
  }

  /**
   * Load and refresh case dropdown.
   */
  async function refreshCases() {
    if (!apiClient) return;

    setConnectionStatus("checking", "Connecting");
    caseSelect.innerHTML = '<option value="">Loading cases from FormFlow...</option>';
    caseSelect.disabled = true;
    hideGlobalAlert();

    const res = await apiClient.listCases();

    caseSelect.disabled = false;

    if (!res.ok) {
      setConnectionStatus("disconnected", "Offline");
      caseSelect.innerHTML = '<option value="">(Failed to load cases)</option>';
      showGlobalAlert(res.message, "error");
      renderEmptyPreview("Cannot load cases. Fix connection in Settings.");
      updateFillActionSection(null);
      return;
    }

    setConnectionStatus("connected", "Connected");
    cachedCases = res.cases || [];

    if (cachedCases.length === 0) {
      selectedCaseId = "";
      currentPacket = null;
      caseSelect.innerHTML = '<option value="">No cases found in FormFlow</option>';
      renderEmptyPreview("No cases found. Ingest and review documents in FormFlow first.");
      updateFillActionSection(null);
      return;
    }

    let html = '<option value="">-- Select a case --</option>';
    for (const c of cachedCases) {
      const stateLabel = c.reviewed ? "Reviewed" : (c.status === "needs_attention" ? "Needs Attention" : c.status);
      html += `<option value="${escapeHtml(c.case_id)}">${escapeHtml(c.case_id)} &bull; ${escapeHtml(stateLabel)}</option>`;
    }
    caseSelect.innerHTML = html;

    const previouslySelected = selectedCaseId;
    const caseStillPresent = Boolean(previouslySelected && cachedCases.some((c) => c.case_id === previouslySelected));

    if (caseStillPresent) {
      caseSelect.value = previouslySelected;
      onCaseSelected(previouslySelected);
    } else {
      selectedCaseId = "";
      currentPacket = null;
      caseSelect.value = "";
      caseMetaBanner.classList.add("hidden");
      renderEmptyPreview("Select a case above to preview operator-confirmed fields.");
      updateFillActionSection(null);
    }
  }

  /**
   * Handle Case Selection
   */
  function onCaseSelected(caseId) {
    selectedCaseId = caseId || "";
    requestSeq++;
    currentPacket = null;
    hideGlobalAlert();
    hideFillAlert();
    updateFillActionSection(null);

    if (!selectedCaseId) {
      caseMetaBanner.classList.add("hidden");
      renderEmptyPreview();
      return;
    }

    const c = cachedCases.find((x) => x.case_id === selectedCaseId);
    if (c) {
      caseMetaBanner.classList.remove("hidden");
      caseStatusBadge.textContent = c.reviewed ? "Reviewed" : c.status;
      caseStatusBadge.className = `badge ${c.reviewed ? "badge-eligible" : "badge-unavailable"}`;
      caseVersionBadge.textContent = `v${c.review_version}`;
      caseDateBadge.textContent = c.reviewed_at ? c.reviewed_at.split("T")[0] : "";
    } else {
      caseMetaBanner.classList.add("hidden");
      caseStatusBadge.textContent = "";
      caseVersionBadge.textContent = "";
      caseDateBadge.textContent = "";
    }

    loadFillPacketPreview();
  }

  /**
   * Render Preview States
   */
  function hideAllPreviewStates() {
    previewLoading.classList.add("hidden");
    previewEmpty.classList.add("hidden");
    unsupportedNotice.classList.add("hidden");
    previewError.classList.add("hidden");
    previewContent.classList.add("hidden");
  }

  function renderEmptyPreview(msg) {
    hideAllPreviewStates();
    previewEmpty.classList.remove("hidden");
    if (msg) {
      previewEmpty.querySelector("p").textContent = msg;
    } else {
      previewEmpty.querySelector("p").textContent = "Select a case above to preview operator-confirmed fields.";
    }
  }

  function renderUnsupportedWorkflow(wfKey) {
    hideAllPreviewStates();
    const meta = WORKFLOW_META[wfKey] || { label: wfKey, plannedPhase: "Future Phase" };
    unsupportedTitle.textContent = `Workflow '${meta.label}' Planned for ${meta.plannedPhase || "Future Phase"}`;
    unsupportedDesc.innerHTML = `The FormFlow backend currently implements the <code>dl_application</code> contract. Workflow <strong>${escapeHtml(meta.label)}</strong> is planned for future phases. No fake packet is fabricated.`;
    unsupportedNotice.classList.remove("hidden");
    updateFillActionSection(null);
  }

  function renderPreviewError(message) {
    hideAllPreviewStates();
    previewErrorMessage.textContent = message;
    previewError.classList.remove("hidden");
    updateFillActionSection(null);
  }

  /**
   * Fetch and display fill packet.
   */
  async function loadFillPacketPreview() {
    if (!selectedCaseId) {
      renderEmptyPreview();
      updateFillActionSection(null);
      return;
    }

    const wf = WORKFLOW_META[selectedWorkflow];
    if (!wf || !wf.supported) {
      renderUnsupportedWorkflow(selectedWorkflow);
      return;
    }

    hideAllPreviewStates();
    previewLoading.classList.remove("hidden");

    const thisSeq = ++requestSeq;
    const thisCaseId = selectedCaseId;
    const thisWorkflow = selectedWorkflow;

    const res = await apiClient.getFillPacket(thisCaseId, thisWorkflow);

    if (thisSeq !== requestSeq || selectedCaseId !== thisCaseId || selectedWorkflow !== thisWorkflow) {
      return; // Discard late/stale response
    }

    if (!res.ok) {
      renderPreviewError(res.message);
      currentPacket = null;
      updateFillActionSection(null);
      return;
    }

    currentPacket = res.packet;
    renderPacketTable(res.packet);
    updateFillActionSection(res.packet);
  }

  /**
   * Update the Operator-Assisted Autofill Action Section
   */
  function updateFillActionSection(packet) {
    if (!fillActionSection) return;

    if (selectedWorkflow !== "dl_application" || !packet || !Array.isArray(packet.fields)) {
      fillActionSection.classList.add("hidden");
      return;
    }

    fillActionSection.classList.remove("hidden");
    hideFillAlert();

    const fmap = {};
    for (const f of packet.fields) {
      fmap[f.key] = f;
    }

    const llField = fmap["licence_no"];
    const dobField = fmap["dob"];

    const adapter = root.FormFlowPortalAdapter;
    let formattedDob = "";
    let dobValid = false;

    if (dobField && dobField.eligible_to_fill && dobField.value) {
      try {
        formattedDob = adapter ? adapter.formatDobForPortal(dobField.value) : dobField.value;
        dobValid = true;
      } catch (err) {
        formattedDob = "(Invalid Date)";
        dobValid = false;
      }
    }

    const llEligible = Boolean(llField && llField.eligible_to_fill && llField.value);
    const dobEligible = Boolean(dobField && dobField.eligible_to_fill && dobField.value && dobValid);

    previewActionLicence.textContent = llEligible ? llField.value : "(Not confirmed/eligible)";
    previewActionDob.textContent = dobEligible ? formattedDob : "(Not confirmed/eligible)";

    if (!llEligible || !dobEligible) {
      fillDlBtn.disabled = true;
      fillDlBtn.textContent = "Fill LL Number and DOB";
      showFillAlert("Learner's Licence Number or DOB is not confirmed by operator in FormFlow.", "warning");
      return;
    }

    if (!pageReadyForDl) {
      fillDlBtn.disabled = true;
      fillDlBtn.textContent = "Fill LL Number and DOB";
      showFillAlert(pageReadyReason || "Active tab is not on Sarathi DL Entry page ('Holding Learners Licence').", "warning");
      return;
    }

    // Both confirmed & page is ready
    fillDlBtn.disabled = false;
    fillDlBtn.textContent = "Fill LL Number and DOB";
    hideFillAlert();
  }

  /**
   * Execute explicit autofill action
   */
  async function executeAutofill() {
    if (!selectedCaseId) return;

    fillDlBtn.disabled = true;
    fillDlBtn.textContent = "Checking page readiness...";
    hideFillAlert();

    // 1. Re-check active tab and page readiness immediately before filling
    checkPageReadiness(async (isReady, reason, activeTab) => {
      if (!isReady || !activeTab) {
        fillDlBtn.disabled = true;
        fillDlBtn.textContent = "Fill LL Number and DOB";
        showFillAlert(
          "Fill blocked: " + (reason || "Active tab is not on Sarathi DL Entry page ('Holding Learners Licence')."),
          "error"
        );
        return;
      }

      fillDlBtn.textContent = "Verifying fresh packet...";

      // 2. Always fetch fresh eligible packet immediately before filling
      const freshRes = await apiClient.getFillPacket(selectedCaseId, "dl_application");
      if (!freshRes.ok) {
        fillDlBtn.disabled = true;
        fillDlBtn.textContent = "Fill LL Number and DOB";
        showFillAlert(`Fill blocked: ${freshRes.message}. Case state may have changed.`, "error");
        return;
      }

      const freshPacket = freshRes.packet;
      const fmap = {};
      for (const f of freshPacket.fields) {
        fmap[f.key] = f;
      }

      const freshLl = fmap["licence_no"];
      const freshDob = fmap["dob"];

      if (
        !freshLl ||
        !freshLl.eligible_to_fill ||
        !freshLl.value ||
        !freshDob ||
        !freshDob.eligible_to_fill ||
        !freshDob.value
      ) {
        fillDlBtn.disabled = true;
        fillDlBtn.textContent = "Fill LL Number and DOB";
        showFillAlert("Fill blocked: Required fields are no longer confirmed/eligible in FormFlow.", "error");
        return;
      }

      fillDlBtn.textContent = "Filling fields...";

      // 3. Send fill command with valid shape
      chrome.tabs.sendMessage(
        activeTab.id,
        {
          action: "FILL_DL_FIELDS",
          caseId: selectedCaseId,
          reviewVersion: freshPacket.review_version,
          licenceNo: freshLl.value,
          dob: freshDob.value,
        },
        async (fillResult) => {
          fillDlBtn.disabled = false;
          fillDlBtn.textContent = "Fill LL Number and DOB";

          if (chrome.runtime.lastError || !fillResult) {
            showFillAlert("Cannot fill: communication with Sarathi tab failed. Verify page is loaded.", "error");
            return;
          }

          if (fillResult.ok) {
            currentPacket = freshPacket;

            // 4. Record audit event (fill_attempt)
            const auditRes = await apiClient.recordEvent(selectedCaseId, {
              event_type: "fill_attempt",
              review_version: freshPacket.review_version,
              detail: "Operator filled LL Number and DOB on Sarathi DL page",
            });

            if (!auditRes.ok) {
              showFillAlert(
                `Fields filled on portal, but audit logging failed: ${auditRes.message || auditRes.error || "rejected"}`,
                "warning"
              );
            } else {
              showFillAlert(
                fillResult.message || "Learner's Licence Number and DOB filled successfully! Enter CAPTCHA manually and verify before clicking OK.",
                "success"
              );
            }
          } else if (fillResult.partial) {
            showFillAlert("Partial fill: " + fillResult.error, "warning");
          } else {
            showFillAlert("Fill failed: " + (fillResult.error || "Verification mismatch"), "error");
          }
        }
      );
    });
  }

  /**
   * Render packet fields into the preview table.
   */
  function renderPacketTable(packet) {
    hideAllPreviewStates();
    fieldsTableBody.innerHTML = "";

    if (!packet || !Array.isArray(packet.fields) || packet.fields.length === 0) {
      renderEmptyPreview("Fill packet contains no fields for this workflow.");
      return;
    }

    for (const f of packet.fields) {
      const tr = document.createElement("tr");

      // Column 1: Field Label & Key
      const tdLabel = document.createElement("td");
      tdLabel.innerHTML = `
        <span class="field-label">${escapeHtml(f.label)}</span>
        <span class="field-key">${escapeHtml(f.key)}</span>
      `;

      // Column 2: Value
      const tdValue = document.createElement("td");
      if (f.eligible_to_fill && f.value) {
        tdValue.innerHTML = `<span class="field-val">${escapeHtml(f.value)}</span>`;
      } else {
        tdValue.innerHTML = `<span class="val-empty">&mdash;</span>`;
      }

      // Column 3: Eligibility & Status
      const tdStatus = document.createElement("td");
      if (f.eligible_to_fill) {
        tdStatus.innerHTML = `<span class="badge badge-eligible">Eligible</span>`;
      } else {
        const reasonText = f.reason ? escapeHtml(f.reason) : "Unavailable";
        tdStatus.innerHTML = `<span class="badge badge-unavailable" title="${reasonText}">Unavailable</span>`;
      }

      tr.appendChild(tdLabel);
      tr.appendChild(tdValue);
      tr.appendChild(tdStatus);
      fieldsTableBody.appendChild(tr);
    }

    previewContent.classList.remove("hidden");
  }

  /**
   * Event Listeners & Setup
   */
  async function init() {
    bindElements();

    checkPageReadiness();

    if (typeof window !== "undefined") {
      window.addEventListener("focus", () => {
        checkPageReadiness();
      });
    }

    // Load stored settings
    const stored = await getStorage(["apiBaseUrl", "apiToken"]);
    const initialBaseUrl = stored.apiBaseUrl || "http://127.0.0.1:5000";
    const initialToken = stored.apiToken || "";

    apiBaseUrlInput.value = initialBaseUrl;
    apiTokenInput.value = initialToken;

    apiClient = new root.FormFlowApiClient(initialBaseUrl, initialToken);

    // If token is missing, open settings drawer automatically
    if (!initialToken) {
      settingsCard.classList.remove("hidden");
      showSettingsAlert("Welcome! Enter your FormFlow Bearer token from data/.api_token to connect.", "warning");
      setConnectionStatus("disconnected", "No Token");
    } else {
      refreshCases();
    }

    // Toggle Settings
    toggleSettingsBtn.addEventListener("click", () => {
      settingsCard.classList.toggle("hidden");
      hideSettingsAlert();
    });

    closeSettingsBtn.addEventListener("click", () => {
      settingsCard.classList.add("hidden");
      hideSettingsAlert();
    });

    // Save Settings
    saveSettingsBtn.addEventListener("click", async () => {
      const newUrl = (apiBaseUrlInput.value || "http://127.0.0.1:5000").trim();
      const newToken = (apiTokenInput.value || "").trim();

      if (!newToken) {
        showSettingsAlert("API Bearer token is required. Check data/.api_token.", "error");
        return;
      }

      await setStorage({ apiBaseUrl: newUrl, apiToken: newToken });
      apiClient.updateConfig(newUrl, newToken);

      showSettingsAlert("Settings saved successfully. Testing connection...", "success");

      // Verify connection immediately
      const test = await apiClient.listCases();
      if (test.ok) {
        showSettingsAlert("Connected successfully! Ready to preview cases.", "success");
        setConnectionStatus("connected", "Connected");
        setTimeout(() => {
          settingsCard.classList.add("hidden");
          hideSettingsAlert();
        }, 1200);
        refreshCases();
      } else {
        showSettingsAlert(test.message, "error");
        setConnectionStatus("disconnected", "Error");
      }
    });

    // Test Connection Button
    testConnBtn.addEventListener("click", async () => {
      const tempUrl = (apiBaseUrlInput.value || "http://127.0.0.1:5000").trim();
      const tempToken = (apiTokenInput.value || "").trim();

      if (!tempToken) {
        showSettingsAlert("Enter a Bearer token first to test.", "error");
        return;
      }

      testConnBtn.disabled = true;
      testConnBtn.textContent = "Testing...";
      hideSettingsAlert();

      const tempClient = new root.FormFlowApiClient(tempUrl, tempToken);
      const res = await tempClient.listCases();

      testConnBtn.disabled = false;
      testConnBtn.textContent = "Test Connection";

      if (res.ok) {
        showSettingsAlert(`Connection OK! Found ${res.cases.length} cases.`, "success");
      } else {
        showSettingsAlert(res.message, "error");
      }
    });

    // Case Selection Change
    caseSelect.addEventListener("change", (e) => {
      onCaseSelected(e.target.value);
    });

    // Refresh Cases Button
    refreshCasesBtn.addEventListener("click", () => {
      refreshCases();
      checkPageReadiness();
    });

    // Workflow Selection Change
    workflowSelect.addEventListener("change", (e) => {
      selectedWorkflow = e.target.value;
      requestSeq++;
      currentPacket = null;
      updateFillActionSection(null);
      const meta = WORKFLOW_META[selectedWorkflow];
      previewWorkflowTag.textContent = (meta && meta.tag) || selectedWorkflow;

      if (!meta || !meta.supported) {
        renderUnsupportedWorkflow(selectedWorkflow);
      } else {
        loadFillPacketPreview();
        checkPageReadiness();
      }
    });

    // Autofill Action Button
    if (fillDlBtn) {
      fillDlBtn.addEventListener("click", () => {
        executeAutofill();
      });
    }
  }

  // Self-boot
  const root = typeof window !== "undefined" ? window : globalThis;
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }

  // Export controller internals for headless testability
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      WORKFLOW_META,
      escapeHtml,
      _getState: () => ({
        selectedCaseId,
        selectedWorkflow,
        currentPacket,
        pageReadyForDl,
        pageReadyReason,
        requestSeq,
        cachedCases,
      }),
      _setState: (patch) => {
        if ("selectedCaseId" in patch) selectedCaseId = patch.selectedCaseId;
        if ("selectedWorkflow" in patch) selectedWorkflow = patch.selectedWorkflow;
        if ("currentPacket" in patch) currentPacket = patch.currentPacket;
        if ("pageReadyForDl" in patch) pageReadyForDl = patch.pageReadyForDl;
        if ("pageReadyReason" in patch) pageReadyReason = patch.pageReadyReason;
        if ("requestSeq" in patch) requestSeq = patch.requestSeq;
        if ("cachedCases" in patch) cachedCases = patch.cachedCases;
      },
    };
  }
})();
