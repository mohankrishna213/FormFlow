/**
 * FormFlow Local API Client (Phase 7A)
 *
 * Connects securely to the local FormFlow service (default: http://127.0.0.1:5000).
 * Handles Bearer token authentication, timeout control, error normalization,
 * and ensures no sensitive applicant values or tokens are written to console logs.
 */

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.FormFlowApiClient = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const DEFAULT_BASE_URL = "http://127.0.0.1:5000";
  const DEFAULT_TIMEOUT_MS = 5000;

  class FormFlowApiClient {
    /**
     * @param {string} [baseUrl] - Base URL of local FormFlow service
     * @param {string} [token] - Bearer token for API authentication
     * @param {number} [timeoutMs] - Request timeout in milliseconds
     */
    constructor(baseUrl = DEFAULT_BASE_URL, token = "", timeoutMs = DEFAULT_TIMEOUT_MS) {
      this.baseUrl = (baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
      this.token = (token || "").trim();
      this.timeoutMs = timeoutMs;
    }

    /**
     * Update credentials without creating a new instance.
     * @param {string} baseUrl
     * @param {string} token
     */
    updateConfig(baseUrl, token) {
      if (baseUrl !== undefined) {
        this.baseUrl = (baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
      }
      if (token !== undefined) {
        this.token = (token || "").trim();
      }
    }

    /**
     * Internal request wrapper with timeout and normalized error reporting.
     * @private
     */
    async _request(path, options = {}) {
      if (!this.token) {
        return {
          ok: false,
          error: "missing_token",
          status: 401,
          message: "API Bearer token is not configured. Open Settings to set it.",
        };
      }

      const url = `${this.baseUrl}${path}`;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);

      const headers = {
        Accept: "application/json",
        Authorization: `Bearer ${this.token}`,
        ...(options.headers || {}),
      };

      try {
        const response = await fetch(url, {
          ...options,
          headers,
          signal: controller.signal,
        });

        clearTimeout(timer);

        // Parse JSON response safely
        let data = null;
        const text = await response.text();
        if (text) {
          try {
            data = JSON.parse(text);
          } catch (e) {
            return {
              ok: false,
              error: "invalid_json",
              status: response.status,
              message: "Server returned a non-JSON or malformed response.",
            };
          }
        }

        if (!response.ok) {
          return this._normalizeError(response.status, data);
        }

        return {
          ok: true,
          status: response.status,
          data: data,
        };
      } catch (err) {
        clearTimeout(timer);

        if (err.name === "AbortError") {
          return {
            ok: false,
            error: "timeout",
            status: 0,
            message: `Request to FormFlow timed out after ${this.timeoutMs / 1000}s.`,
          };
        }

        return {
          ok: false,
          error: "service_unavailable",
          status: 0,
          message: `Cannot connect to FormFlow at ${this.baseUrl}. Is the application running?`,
        };
      }
    }

    /**
     * Normalize HTTP status and error codes into user-friendly messages.
     * @private
     */
    _normalizeError(status, data) {
      const errCode = (data && data.error) || "http_error";

      switch (status) {
        case 401:
          return {
            ok: false,
            error: "unauthorized",
            status: 401,
            message: "Authentication failed. Check your Bearer token in Settings.",
          };
        case 403:
          return {
            ok: false,
            error: "forbidden",
            status: 403,
            message: "Origin not allowed. The server rejected this client's origin.",
          };
        case 404:
          return {
            ok: false,
            error: "not_found",
            status: 404,
            message: (data && data.error === "case_not_found")
              ? "The requested case was not found."
              : "Resource not found (404).",
          };
        case 409:
          return {
            ok: false,
            error: errCode,
            status: 409,
            message: this._conflictMessage(errCode),
          };
        case 413:
          return {
            ok: false,
            error: "payload_too_large",
            status: 413,
            message: "Event payload exceeds the maximum size limit.",
          };
        case 415:
          return {
            ok: false,
            error: "unsupported_media_type",
            status: 415,
            message: "Request must use application/json content type.",
          };
        case 503:
          return {
            ok: false,
            error: "service_unavailable",
            status: 503,
            message: "FormFlow database or service is currently unavailable.",
          };
        default:
          return {
            ok: false,
            error: errCode,
            status: status,
            message: (data && data.error) ? `Error: ${data.error}` : `HTTP Error ${status}`,
          };
      }
    }

    /**
     * Specific explanations for 409 Conflict statuses.
     * @private
     */
    _conflictMessage(errCode) {
      switch (errCode) {
        case "case_not_reviewed":
          return "Case is not reviewed. Operator must Validate & Save in FormFlow first.";
        case "review_metadata_missing":
          return "Review metadata is incomplete. Re-validation is required.";
        case "stale_review_version":
          return "Review version is stale. New evidence or edits arrived; re-validation required.";
        case "unresolved_conflicts":
          return "Unresolved or fresh conflicts detected. Resolve them in FormFlow before filling.";
        default:
          return `Case conflict (${errCode}). Please check case status in FormFlow.`;
      }
    }

    /**
     * List recent cases for dropdown selection.
     * GET /api/cases
     * @returns {Promise<{ok: boolean, cases?: Array, error?: string, message?: string}>}
     */
    async listCases() {
      const res = await this._request("/api/cases", { method: "GET" });
      if (!res.ok) {
        return res;
      }
      return {
        ok: true,
        cases: (res.data && Array.isArray(res.data.cases)) ? res.data.cases : [],
      };
    }

    /**
     * Fetch validated fill packet for a reviewed case.
     * GET /api/cases/<case_id>/fill-packet?workflow=<workflow>
     * @param {string} caseId
     * @param {string} [workflow="dl_application"]
     * @returns {Promise<{ok: boolean, packet?: Object, error?: string, message?: string}>}
     */
    async getFillPacket(caseId, workflow = "dl_application") {
      if (!caseId || typeof caseId !== "string") {
        return {
          ok: false,
          error: "invalid_case_id",
          status: 400,
          message: "Please select a valid case ID.",
        };
      }

      const q = new URLSearchParams({ workflow });
      const path = `/api/cases/${encodeURIComponent(caseId)}/fill-packet?${q.toString()}`;
      const res = await this._request(path, { method: "GET" });

      if (!res.ok) {
        return res;
      }
      return {
        ok: true,
        packet: res.data,
      };
    }

    /**
     * Record an audit event (fill/copy/restore attempt).
     * POST /api/cases/<case_id>/events
     * @param {string} caseId
     * @param {Object} eventPayload
     * @returns {Promise<{ok: boolean, error?: string, message?: string}>}
     */
    async recordEvent(caseId, eventPayload) {
      if (!caseId) {
        return { ok: false, error: "invalid_case_id", message: "Case ID is required." };
      }

      const path = `/api/cases/${encodeURIComponent(caseId)}/events`;
      const res = await this._request(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(eventPayload),
      });

      return res;
    }
  }

  return FormFlowApiClient;
});
