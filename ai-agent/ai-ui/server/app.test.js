import assert from "node:assert/strict";
import { test } from "node:test";
import { createApp } from "./app.js";

async function withServer(app, run) {
  const server = app.listen(0, "127.0.0.1");
  await new Promise((resolve, reject) => {
    server.once("listening", resolve);
    server.once("error", reject);
  });
  const address = server.address();
  try {
    await run(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}

test("health and readiness distinguish process health from backend readiness", async () => {
  const app = createApp({
    apiKey: "server-only-test-key",
    fetchImpl: async (url) => url.endsWith("/readyz") ? new Response("{}", { status: 200 }) : new Response("{}")
  });
  await withServer(app, async (base) => {
    const health = await fetch(`${base}/healthz`);
    assert.equal(health.status, 200);
    assert.deepEqual(await health.json(), { status: "ok" });
    const ready = await fetch(`${base}/api/readyz`);
    assert.equal(ready.status, 200);
    assert.deepEqual(await ready.json(), { status: "ready" });
    assert.equal(ready.headers.get("x-content-type-options"), "nosniff");
    assert.equal(ready.headers.get("x-frame-options"), "DENY");
  });
});

test("readiness fails closed when the API key or backend is unavailable", async () => {
  for (const options of [
    { apiKey: undefined, fetchImpl: async () => new Response("{}") },
    { apiKey: "server-only-test-key", fetchImpl: async () => new Response("{}", { status: 503 }) },
    { apiKey: "server-only-test-key", fetchImpl: async () => { throw new Error("internal host detail"); } }
  ]) {
    await withServer(createApp(options), async (base) => {
      const response = await fetch(`${base}/readyz`);
      assert.equal(response.status, 503);
      assert.deepEqual(await response.json(), { status: "not_ready" });
    });
  }
});

test("investigation proxy sends its secret only upstream and preserves backend errors", async () => {
  let captured;
  const app = createApp({
    apiKey: "server-only-test-key",
    fetchImpl: async (url, options) => {
      captured = { url, options };
      return new Response(JSON.stringify({ detail: "Backend validation failed" }), {
        status: 422,
        headers: { "content-type": "application/json" }
      });
    }
  });
  await withServer(app, async (base) => {
    const response = await fetch(`${base}/api/investigate`, {
      method: "POST",
      headers: { "content-type": "application/json", origin: base },
      body: JSON.stringify({ query: "  inspect errors  ", environment: " stg " })
    });
    assert.equal(response.status, 422);
    assert.deepEqual(await response.json(), { detail: "Backend validation failed" });
    assert.equal(captured.url, "http://sre-ai-backend.sre-ai.svc.cluster.local:8080/api/v1/investigate");
    assert.equal(captured.options.headers["X-API-Key"], "server-only-test-key");
    assert.deepEqual(JSON.parse(captured.options.body), { query: "inspect errors", environment: "stg" });
    assert.equal(captured.options.redirect, "manual");
    assert.ok(response.headers.get("x-request-id"));
    assert.equal(response.headers.get("cache-control"), "no-store");
  });
});

test("old backend validation identifies unsupported fields without retrying or dropping filters", async () => {
  let calls = 0;
  const detail = [{ type: "extra_forbidden", loc: ["body", "timezone"], msg: "Extra inputs are not permitted", input: "UTC" }];
  const app = createApp({ apiKey: "server-only-test-key", fetchImpl: async (_url, options) => {
    calls += 1;
    assert.equal(JSON.parse(options.body).timezone, "UTC");
    return new Response(JSON.stringify({ detail }), { status: 422, headers: { "content-type": "application/json" } });
  } });
  await withServer(app, async (base) => {
    const response = await fetch(`${base}/api/investigate`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "provide the last 5 days cpu trends for prod-content02.speech.prod.westus.azcloud.247-inc.net", environment: "prod", timezone: "UTC" })
    });
    const payload = await response.json();
    assert.equal(response.status, 422);
    assert.deepEqual(payload.detail, detail);
    assert.match(payload.error, /deployed backend.*timezone/);
    assert.match(payload.error, /updated backend image/);
    assert.equal(calls, 1);
  });
});

test("old backend rejecting all environments reports version mismatch without narrowing or retrying", async () => {
  let calls = 0;
  const detail = [{ type: "literal_error", loc: ["body", "environment"], msg: "Input should be 'stg', 'prod' or 'hub'", input: "all" }];
  const app = createApp({ apiKey: "server-only-test-key", fetchImpl: async (_url, options) => {
    calls += 1;
    assert.deepEqual(JSON.parse(options.body), { query: "Show CPU and memory in hub", environment: "all", time_range: "last 1 hour" });
    return new Response(JSON.stringify({ detail }), { status: 422, headers: { "content-type": "application/json" } });
  } });
  await withServer(app, async (base) => {
    const response = await fetch(`${base}/api/investigate`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "Show CPU and memory in hub", environment: "all", time_range: "last 1 hour" })
    });
    const payload = await response.json();
    assert.equal(response.status, 422);
    assert.deepEqual(payload.detail, detail);
    assert.match(payload.error, /deployed backend does not support All environments/);
    assert.match(payload.error, /Deploy the updated backend image/);
    assert.match(payload.error, /select STG, PROD or HUB explicitly/);
    assert.equal(calls, 1);
  });
});

test("proxy forwards all environments unchanged to an updated backend", async () => {
  let calls = 0;
  const app = createApp({ apiKey: "server-only-test-key", fetchImpl: async (_url, options) => {
    calls += 1;
    assert.equal(JSON.parse(options.body).environment, "all");
    return new Response(JSON.stringify({ summary: "Metrics for stg, prod and hub" }), { headers: { "content-type": "application/json" } });
  } });
  await withServer(app, async (base) => {
    const response = await fetch(`${base}/api/investigate`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "Show CPU and memory", environment: "all" })
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { summary: "Metrics for stg, prod and hub" });
    assert.equal(calls, 1);
  });
});

test("proxy preserves time and search filters without forwarding unknown fields", async () => {
  let captured;
  const app = createApp({ apiKey: "server-only-test-key", fetchImpl: async (_url, options) => {
    captured = JSON.parse(options.body);
    return new Response("{}");
  } });
  await withServer(app, async (base) => {
    const filters = { time_range: "custom", start_time: "2026-10-25T00:00:00Z", end_time: "2026-10-26T00:00:00Z", timezone: "Asia/Kolkata", search_mode: "hybrid", service: "checkout" };
    const response = await fetch(`${base}/api/investigate`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ query: "checkout errors", environment: "stg", ...filters, api_key: "untrusted" }) });
    assert.equal(response.status, 200);
    assert.deepEqual(captured, { query: "checkout errors", environment: "stg", ...filters });
    for (const invalid of [{ search_mode: "invalid" }, { time_range: 42 }, { timezone: "x".repeat(65) }]) {
      const rejected = await fetch(`${base}/api/investigate`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ query: "checkout errors", environment: "stg", ...invalid }) });
      assert.equal(rejected.status, 400);
    }
  });
});

test("proxy rejects cross-origin, malformed, oversized, and invalid requests", async () => {
  let upstreamCalls = 0;
  const app = createApp({ apiKey: "server-only-test-key", fetchImpl: async () => {
    upstreamCalls += 1;
    return new Response("{}");
  } });
  await withServer(app, async (base) => {
    const crossOrigin = await fetch(`${base}/api/investigate`, {
      method: "POST", headers: { "content-type": "application/json", origin: "https://attacker.example" },
      body: JSON.stringify({ query: "errors", environment: "stg" })
    });
    assert.equal(crossOrigin.status, 403);

    const invalid = await fetch(`${base}/api/investigate`, {
      method: "POST", headers: { "content-type": "application/json", origin: base },
      body: JSON.stringify({ query: " ", environment: "stg" })
    });
    assert.equal(invalid.status, 400);

    const malformed = await fetch(`${base}/api/investigate`, {
      method: "POST", headers: { "content-type": "application/json", origin: base }, body: "{"
    });
    assert.equal(malformed.status, 400);

    const oversized = await fetch(`${base}/api/investigate`, {
      method: "POST", headers: { "content-type": "application/json", origin: base },
      body: JSON.stringify({ query: "x".repeat(20_000), environment: "stg" })
    });
    assert.equal(oversized.status, 413);
    assert.equal(upstreamCalls, 0);
  });
});

test("upstream network errors never expose internal URLs or exception details", async () => {
  const app = createApp({
    apiKey: "server-only-test-key",
    logger: { error() {} },
    fetchImpl: async () => { throw new Error("secret.internal:8080 connection refused"); }
  });
  await withServer(app, async (base) => {
    const response = await fetch(`${base}/api/investigate`, {
      method: "POST", headers: { "content-type": "application/json", origin: base },
      body: JSON.stringify({ query: "errors", environment: "stg" })
    });
    assert.equal(response.status, 502);
    const body = await response.text();
    assert.match(body, /investigation service is unavailable/i);
    assert.doesNotMatch(body, /secret\.internal|connection refused/);
  });
});

test("oversized backend responses are rejected", async () => {
  const app = createApp({
    apiKey: "server-only-test-key",
    fetchImpl: async () => new Response("x".repeat(1_000_001), {
      headers: { "content-length": "1000001" }
    })
  });
  await withServer(app, async (base) => {
    const response = await fetch(`${base}/api/investigate`, {
      method: "POST", headers: { "content-type": "application/json", origin: base },
      body: JSON.stringify({ query: "errors", environment: "stg" })
    });
    assert.equal(response.status, 502);
    assert.deepEqual(await response.json(), { error: "Backend response exceeded the UI proxy limit." });
  });
});
