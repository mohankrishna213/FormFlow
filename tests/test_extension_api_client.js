/**
 * Node.js Test Suite for FormFlow Extension API Client & Workflow Logic (Phase 7A)
 *
 * Tests:
 * 1. Successful case listing.
 * 2. Empty case list.
 * 3. Invalid or missing token.
 * 4. FormFlow not running.
 * 5. Invalid JSON or unexpected API response.
 * 6. Unreviewed case or stale packet (409).
 * 7. Missing/unconfirmed values in fill packet.
 * 8. Workflow choices not yet supported.
 * 9. Privacy invariant: ensure no applicant values or token are written to console logs.
 */

const http = require("http");
const assert = require("assert");
const FormFlowApiClient = require("../extension/api_client.js");
const { WORKFLOW_META } = require("../extension/popup.js");

const TEST_TOKEN = "a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890";
const SECRET_PII_NAME = "Sneha Ramanathan";
const SECRET_PII_DOB = "1994-06-25";

let server;
let serverPort = 0;
let requestHandler = (req, res) => {
  res.writeHead(404);
  res.end();
};

function startServer() {
  return new Promise((resolve) => {
    server = http.createServer((req, res) => requestHandler(req, res));
    server.listen(0, "127.0.0.1", () => {
      serverPort = server.address().port;
      resolve();
    });
  });
}

function stopServer() {
  return new Promise((resolve) => {
    server.close(() => resolve());
  });
}

async function runTests() {
  await startServer();
  const baseUrl = `http://127.0.0.1:${serverPort}`;
  console.log(`\n=== Running FormFlow Extension Tests against ${baseUrl} ===\n`);

  let passed = 0;
  let failed = 0;

  async function test(name, fn) {
    try {
      await fn();
      console.log(`[PASS] ${name}`);
      passed++;
    } catch (err) {
      console.error(`[FAIL] ${name}:`, err.message);
      failed++;
    }
  }

  // 1. Successful case listing
  await test("1. Successful case listing returns cases array", async () => {
    requestHandler = (req, res) => {
      assert.strictEqual(req.url, "/api/cases");
      assert.strictEqual(req.headers["authorization"], `Bearer ${TEST_TOKEN}`);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        cases: [
          { case_id: "CASE-20261017-001", status: "reviewed", reviewed: true },
          { case_id: "CASE-20261017-002", status: "needs_attention", reviewed: false },
        ],
      }));
    };

    const client = new FormFlowApiClient(baseUrl, TEST_TOKEN);
    const res = await client.listCases();
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.cases.length, 2);
    assert.strictEqual(res.cases[0].case_id, "CASE-20261017-001");
    assert.strictEqual(res.cases[0].reviewed, true);
  });

  // 2. Empty case list
  await test("2. Empty case list returns empty array", async () => {
    requestHandler = (req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ cases: [] }));
    };

    const client = new FormFlowApiClient(baseUrl, TEST_TOKEN);
    const res = await client.listCases();
    assert.strictEqual(res.ok, true);
    assert.deepStrictEqual(res.cases, []);
  });

  // 3. Missing or invalid token
  await test("3a. Missing token returns missing_token error before request", async () => {
    const client = new FormFlowApiClient(baseUrl, "");
    const res = await client.listCases();
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error, "missing_token");
    assert.strictEqual(res.status, 401);
  });

  await test("3b. Invalid token triggers 401 unauthorized", async () => {
    requestHandler = (req, res) => {
      res.writeHead(401, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "authentication_required" }));
    };

    const client = new FormFlowApiClient(baseUrl, "wrong-token");
    const res = await client.listCases();
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error, "unauthorized");
    assert.strictEqual(res.status, 401);
  });

  // 4. FormFlow not running
  await test("4. FormFlow not running returns service_unavailable", async () => {
    // Port 1 is not running FormFlow
    const client = new FormFlowApiClient("http://127.0.0.1:1", TEST_TOKEN, 500);
    const res = await client.listCases();
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error, "service_unavailable");
    assert.strictEqual(res.status, 0);
  });

  // 5. Invalid JSON or unexpected API response
  await test("5. Invalid JSON response returns invalid_json error", async () => {
    requestHandler = (req, res) => {
      res.writeHead(500, { "Content-Type": "text/html" });
      res.end("<html><body>Internal Server Error</body></html>");
    };

    const client = new FormFlowApiClient(baseUrl, TEST_TOKEN);
    const res = await client.listCases();
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error, "invalid_json");
  });

  // 6. Unreviewed case or stale packet
  await test("6a. Unreviewed case returns 409 case_not_reviewed", async () => {
    requestHandler = (req, res) => {
      res.writeHead(409, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "case_not_reviewed" }));
    };

    const client = new FormFlowApiClient(baseUrl, TEST_TOKEN);
    const res = await client.getFillPacket("CASE-UNREVIEWED");
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.status, 409);
    assert.strictEqual(res.error, "case_not_reviewed");
    assert.ok(res.message.includes("Validate & Save"));
  });

  await test("6b. Stale review version returns 409 stale_review_version", async () => {
    requestHandler = (req, res) => {
      res.writeHead(409, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "stale_review_version" }));
    };

    const client = new FormFlowApiClient(baseUrl, TEST_TOKEN);
    const res = await client.getFillPacket("CASE-STALE");
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.status, 409);
    assert.strictEqual(res.error, "stale_review_version");
    assert.ok(res.message.includes("stale"));
  });

  // 7. Missing and unconfirmed values in fill packet
  await test("7. Packet preserves missing and unconfirmed values without fabricating", async () => {
    requestHandler = (req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        case_id: "CASE-20261017-001",
        workflow: "dl_application",
        workflow_label: "Driving Licence Application",
        review_version: 1,
        reviewed_at: "2026-10-17T03:00:00Z",
        fields: [
          { key: "full_name", label: "Full name", value: SECRET_PII_NAME, eligible_to_fill: true },
          { key: "dob", label: "Date of birth", value: SECRET_PII_DOB, eligible_to_fill: true },
          { key: "father_name", label: "Father's name", value: null, eligible_to_fill: false, reason: "Value not available" },
          { key: "licence_no", label: "Learner's Licence Number", value: null, eligible_to_fill: false, reason: "Value not confirmed by operator" },
        ],
      }));
    };

    const client = new FormFlowApiClient(baseUrl, TEST_TOKEN);
    const res = await client.getFillPacket("CASE-20261017-001");
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.packet.fields.length, 4);

    const fmap = {};
    for (const f of res.packet.fields) {
      fmap[f.key] = f;
    }

    assert.strictEqual(fmap.full_name.eligible_to_fill, true);
    assert.strictEqual(fmap.full_name.value, SECRET_PII_NAME);

    // Unconfirmed and missing fields are NOT eligible and have correct reasons
    assert.strictEqual(fmap.father_name.eligible_to_fill, false);
    assert.strictEqual(fmap.father_name.value, null);
    assert.strictEqual(fmap.father_name.reason, "Value not available");

    assert.strictEqual(fmap.licence_no.eligible_to_fill, false);
    assert.strictEqual(fmap.licence_no.value, null);
    assert.strictEqual(fmap.licence_no.reason, "Value not confirmed by operator");
  });

  // 8. Workflow choices not yet supported
  await test("8. Workflow metadata distinguishes supported from planned workflows", () => {
    assert.strictEqual(WORKFLOW_META.dl_application.supported, true);
    assert.strictEqual(WORKFLOW_META.ll_application.supported, false);
    assert.strictEqual(WORKFLOW_META.app_status.supported, false);
    assert.strictEqual(WORKFLOW_META.print_form.supported, false);
    assert.strictEqual(WORKFLOW_META.payment_status.supported, false);
    assert.strictEqual(Boolean(WORKFLOW_META.ll_application.plannedPhase), true);
  });

  // 9. Privacy invariant: no token or applicant PII in console logs
  await test("9. No Bearer token or applicant PII leaked to console logs", async () => {
    const originalLog = console.log;
    const originalError = console.error;
    const originalWarn = console.warn;

    let loggedOutput = "";
    const intercept = (...args) => {
      loggedOutput += " " + args.map(a => (typeof a === "object" ? JSON.stringify(a) : String(a))).join(" ");
    };

    console.log = intercept;
    console.error = intercept;
    console.warn = intercept;

    try {
      requestHandler = (req, res) => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({
          case_id: "CASE-PRIVACY",
          workflow: "dl_application",
          fields: [
            { key: "full_name", value: SECRET_PII_NAME, eligible_to_fill: true },
            { key: "dob", value: SECRET_PII_DOB, eligible_to_fill: true },
          ],
        }));
      };

      const client = new FormFlowApiClient(baseUrl, TEST_TOKEN);
      await client.listCases();
      await client.getFillPacket("CASE-PRIVACY");
      await client.recordEvent("CASE-PRIVACY", { event_type: "copy_attempt", review_version: 1, field_key: "full_name" });
    } finally {
      console.log = originalLog;
      console.error = originalError;
      console.warn = originalWarn;
    }

    assert.strictEqual(loggedOutput.includes(TEST_TOKEN), false, "Token leaked to console logs!");
    assert.strictEqual(loggedOutput.includes(SECRET_PII_NAME), false, "Applicant name leaked to console logs!");
    assert.strictEqual(loggedOutput.includes(SECRET_PII_DOB), false, "Applicant DOB leaked to console logs!");
  });

  await stopServer();

  console.log(`\nResults: ${passed} passed, ${failed} failed.\n`);
  if (failed > 0) {
    process.exit(1);
  }
}

runTests().catch((err) => {
  console.error("Test execution failed:", err);
  process.exit(1);
});
