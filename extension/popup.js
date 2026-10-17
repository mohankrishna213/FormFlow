/**
 * FormFlow Extension Popup Controller (Phase 7A)
 *
 * Coordinates extension settings, case selection, workflow routing, and
 * operator-confirmed fill packet preview.
 *
 * Invariants:
 * - Never prints token or applicant field values to console.
 * - Never persists applicant values in chrome.storage.
 * - Never automates or touches portal DOM in Phase 7A.
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

  // Workflow definitions (Phase 7A)
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
      plannedPhase: "Phase 7B",
    },
    app_status: {
      label: "Application Status",
      supported: false,
      tag: "app_status",
      plannedPhase: "Phase 7B",
    },
    print_form: {
      label: "Print Form",
      supported: false,
      tag: "print_form",
      plannedPhase: "Phase 7B",
    },
    payment_status: {
      label: "Payment Status",
      supported: false,
      tag: "payment_status",
      plannedPhase: "Phase 7B",
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
          out[k] = localStorage.getItem(`formflow_${k}`) || "";
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
        for (const [k, v] of Object.entries(items)) {
          localStorage.setItem(`formflow_${k}`, v);
        }
        resolve();
      }
    });
  }

  /**
   * Update Connection Status Badge
   */
  function setConnectionStatus(status, text) {
    connIndicator.className = `conn-badge conn-${status}`;
    connLabel.textContent = text;
  }

  /**
   * Show/Hide Alerts
   */
  function showGlobalAlert(message, type = "error") {
    globalAlert.className = `alert alert-${type}`;
    globalAlert.textContent = message;
    globalAlert.classList.remove("hidden");
  }

  function hideGlobalAlert() {
    globalAlert.classList.add("hidden");
    globalAlert.textContent = "";
  }

  function showSettingsAlert(message, type = "error") {
    settingsAlert.className = `alert alert-${type}`;
    settingsAlert.textContent = message;
    settingsAlert.classList.remove("hidden");
  }

  function hideSettingsAlert() {
    settingsAlert.classList.add("hidden");
    settingsAlert.textContent = "";
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
      return;
    }

    setConnectionStatus("connected", "Connected");
    cachedCases = res.cases || [];

    if (cachedCases.length === 0) {
      caseSelect.innerHTML = '<option value="">No cases found in FormFlow</option>';
      renderEmptyPreview("No cases found. Ingest and review documents in FormFlow first.");
      return;
    }

    let html = '<option value="">-- Select a case --</option>';
    for (const c of cachedCases) {
      const stateLabel = c.reviewed ? "Reviewed" : (c.status === "needs_attention" ? "Needs Attention" : c.status);
      html += `<option value="${escapeHtml(c.case_id)}">${escapeHtml(c.case_id)} &bull; ${escapeHtml(stateLabel)}</option>`;
    }
    caseSelect.innerHTML = html;

    // Restore selected case if still present
    if (selectedCaseId && cachedCases.some((c) => c.case_id === selectedCaseId)) {
      caseSelect.value = selectedCaseId;
      onCaseSelected(selectedCaseId);
    } else {
      selectedCaseId = "";
      caseMetaBanner.classList.add("hidden");
      renderEmptyPreview();
    }
  }

  /**
   * Handle Case Selection Change
   */
  function onCaseSelected(caseId) {
    selectedCaseId = caseId;
    hideGlobalAlert();

    if (!caseId) {
      caseMetaBanner.classList.add("hidden");
      renderEmptyPreview();
      return;
    }

    const c = cachedCases.find((x) => x.case_id === caseId);
    if (c) {
      caseMetaBanner.classList.remove("hidden");
      if (c.reviewed) {
        caseStatusBadge.className = "badge badge-reviewed";
        caseStatusBadge.textContent = "Reviewed";
      } else {
        caseStatusBadge.className = "badge badge-attention";
        caseStatusBadge.textContent = c.status || "Needs Attention";
      }
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
    const meta = WORKFLOW_META[wfKey] || { label: wfKey, plannedPhase: "Phase 7B" };
    unsupportedTitle.textContent = `Workflow '${meta.label}' Planned for ${meta.plannedPhase || "Phase 7B"}`;
    unsupportedDesc.innerHTML = `The FormFlow backend currently implements the <code>dl_application</code> contract. Workflow <strong>${escapeHtml(meta.label)}</strong> is planned for future phases. No fake packet is fabricated.`;
    unsupportedNotice.classList.remove("hidden");
  }

  function renderPreviewError(message) {
    hideAllPreviewStates();
    previewErrorMessage.textContent = message;
    previewError.classList.remove("hidden");
  }

  /**
   * Fetch and display fill packet.
   */
  async function loadFillPacketPreview() {
    if (!selectedCaseId) {
      renderEmptyPreview();
      return;
    }

    const wf = WORKFLOW_META[selectedWorkflow];
    if (!wf || !wf.supported) {
      renderUnsupportedWorkflow(selectedWorkflow);
      return;
    }

    hideAllPreviewStates();
    previewLoading.classList.remove("hidden");

    const res = await apiClient.getFillPacket(selectedCaseId, selectedWorkflow);

    if (!res.ok) {
      renderPreviewError(res.message);
      return;
    }

    renderPacketTable(res.packet);
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

    // Update metadata banner
    if (packet.review_version) {
      caseVersionBadge.textContent = `v${packet.review_version}`;
    }
    if (packet.reviewed_at) {
      try {
        const dt = new Date(packet.reviewed_at);
        caseDateBadge.textContent = dt.toLocaleDateString();
      } catch (e) {
        caseDateBadge.textContent = packet.reviewed_at;
      }
    }

    for (const f of packet.fields) {
      const tr = document.createElement("tr");

      // Column 1: Field Label & semantic key
      const tdLabel = document.createElement("td");
      tdLabel.innerHTML = `
        <span class="field-label">${escapeHtml(f.label || f.key)}</span>
        <span class="field-key">${escapeHtml(f.key)}</span>
      `;

      // Column 2: Confirmed Value
      const tdValue = document.createElement("td");
      if (f.value !== null && f.value !== undefined && String(f.value).trim() !== "") {
        tdValue.innerHTML = `<span class="field-val">${escapeHtml(f.value)}</span>`;
      } else {
        tdValue.innerHTML = `<span class="field-val val-empty">—</span>`;
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
    });

    // Workflow Selection Change
    workflowSelect.addEventListener("change", (e) => {
      selectedWorkflow = e.target.value;
      const meta = WORKFLOW_META[selectedWorkflow];
      previewWorkflowTag.textContent = (meta && meta.tag) || selectedWorkflow;

      if (!meta || !meta.supported) {
        renderUnsupportedWorkflow(selectedWorkflow);
      } else {
        loadFillPacketPreview();
      }
    });
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
    };
  }
})();
