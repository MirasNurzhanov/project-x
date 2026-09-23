import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import worker from "./index.js";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const env = {
  DJANGO_ORIGIN: "https://api.example.test",
  MODERATION_WORKER_TOKEN: "test-worker-token",
  MODERATION_ADMIN_TOKEN: "test-admin-token",
  CLOUDFLARE_ACCOUNT_ID: "test-account",
  CLOUDFLARE_API_TOKEN: "test-ai-token",
  CLOUDFLARE_MEDIA_TRANSFORM_BASE: "https://media.example.test",
  MOD_MAX_FRAMES: "2",
};
const lease = {
  job_id: 42, source_url: "https://cdn.example.test/video.mp4",
  timestamps_ms: [0, 1000, 2000, 3000, 4000, 5000, 6000],
  vision_model: "@cf/test/vision", vision_input_format: "messages",
  prompt: "Inspect pixels", vision_max_tokens: 512,
  vision_response_format: { type: "json_schema", json_schema: { type: "object" } },
};
function run(bindings = env) {
  return worker.fetch(new Request("https://worker.example.test/run", {
    method: "POST", headers: { authorization: "Bearer test-admin-token" },
  }), bindings);
}

test("unauthenticated manual runs never lease work", async () => {
  globalThis.fetch = () => { throw new Error("unexpected network call"); };
  assert.equal((await worker.fetch(new Request("https://worker.example.test/run", { method: "POST" }), env)).status, 401);
});

test("all Django timestamps are checked with bounded concurrency and raw results retained", async () => {
  let settled, active = 0, peak = 0, calls = 0;
  globalThis.fetch = async (input, init) => {
    const url = new URL(input);
    if (url.pathname.endsWith("/lease/")) {
      assert.equal(init.headers.authorization, "Bearer test-worker-token");
      assert.equal(init.headers["x-moderation-vision-formats"], "image,messages");
      return Response.json({ leases: [lease] });
    }
    if (url.pathname.endsWith("/settle/")) {
      settled = JSON.parse(init.body);
      return Response.json({ applied: true });
    }
    if (url.hostname === "media.example.test") return new Response(new Uint8Array([1, 2, 3]));
    assert.equal(url.hostname, "api.cloudflare.com");
    assert.equal(init.headers.authorization, "Bearer test-ai-token");
    const body = JSON.parse(init.body);
    assert.deepEqual(body.response_format, lease.vision_response_format);
    assert.equal(body.messages[1].content[1].image_url.url, "data:image/jpeg;base64,AQID");
    assert.equal(body.max_tokens, 512);
    const call = calls++;
    peak = Math.max(peak, ++active);
    await new Promise(resolve => setTimeout(resolve, 5));
    active--;
    // A malformed model answer must reach Django's validator unchanged.
    return Response.json({ success: true, result: { response: call === 0 ? "invalid output" : "{}" } });
  };
  assert.equal((await run()).status, 200);
  assert.deepEqual(settled.frames.map(frame => frame.at_ms), lease.timestamps_ms);
  assert.equal(settled.frames[0].raw, "invalid output");
  assert.equal(calls, lease.timestamps_ms.length);
  assert.ok(peak <= 2, `peak concurrency ${peak}`);
});

test("missing AI credentials fail before claiming a job", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json({ leases: [] }); };
  assert.equal((await run({ ...env, CLOUDFLARE_API_TOKEN: "" })).status, 500);
  assert.equal(calls, 0);
});

test("response-body stalls time out and failed frames remain in settlement", async () => {
  let settled;
  globalThis.fetch = async (input, init) => {
    const url = new URL(input);
    if (url.pathname.endsWith("/lease/")) return Response.json({ leases: [{ ...lease, timestamps_ms: [0] }] });
    if (url.pathname.endsWith("/settle/")) {
      settled = JSON.parse(init.body);
      return Response.json({ applied: true });
    }
    assert.equal(url.hostname, "media.example.test");
    return new Response(new ReadableStream({
      start(controller) {
        init.signal.addEventListener("abort", () => controller.error(new Error("request timed out")), { once: true });
      },
    }));
  };
  assert.equal((await run({ ...env, MOD_HTTP_TIMEOUT_MS: "1000" })).status, 200);
  assert.equal(settled.frames.length, 1);
  assert.equal(settled.frames[0].at_ms, 0);
  assert.match(settled.frames[0].error, /timed out/);
});
