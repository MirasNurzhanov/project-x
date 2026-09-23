import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import worker from "./index.js";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const env = { BACKEND_ORIGIN: "https://api.example.test" };

test("proxy preserves authenticated uploads, path and query at the configured origin", async () => {
  globalThis.fetch = async (request, init) => {
    assert.equal(request.url, "https://api.example.test/api/swifts/?page=2");
    assert.equal(request.method, "POST");
    assert.equal(await request.text(), "upload-body");
    assert.equal(request.headers.get("authorization"), "Bearer test-user-token");
    assert.equal(request.headers.get("host"), "api.example.test");
    assert.equal(request.headers.get("x-forwarded-host"), "edge.example.test");
    assert.equal(request.headers.get("connection"), null);
    assert.equal(init.redirect, "manual");
    return new Response("created", { status: 201 });
  };
  const response = await worker.fetch(new Request("https://edge.example.test/api/swifts/?page=2", {
    method: "POST", body: "upload-body",
    headers: { authorization: "Bearer test-user-token", host: "edge.example.test", connection: "keep-alive" },
  }), env);
  assert.equal(response.status, 201);
  assert.equal(await response.text(), "created");
});

test("WebSocket upgrade headers and origin response pass through unchanged", async () => {
  const upgradeResponse = { status: 101, webSocket: {} };
  globalThis.fetch = async request => {
    assert.equal(request.headers.get("upgrade"), "websocket");
    assert.equal(request.headers.get("connection"), "Upgrade");
    assert.equal(request.url, "https://api.example.test/ws/chat/?ticket=test");
    return upgradeResponse;
  };
  const result = await worker.fetch(new Request("https://edge.example.test/ws/chat/?ticket=test", {
    headers: { upgrade: "websocket", connection: "Upgrade" },
  }), env);
  assert.equal(result, upgradeResponse);
});

test("origin failures return a gateway error", async () => {
  globalThis.fetch = async () => { throw new Error("origin unreachable"); };
  assert.equal((await worker.fetch(new Request("https://edge.example.test/"), env)).status, 502);
});
