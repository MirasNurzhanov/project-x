const API_ROOT = "https://api.cloudflare.com/client/v4";
const DEFAULT_MAX_JOBS = 1;
const DEFAULT_MAX_FRAMES = 6;
const DEFAULT_MAX_TOKENS = 512;
const DEFAULT_HTTP_TIMEOUT_MS = 25_000;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function number(value, fallback, minimum, maximum) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(Math.max(parsed, minimum), maximum);
}

async function fetchWithTimeout(input, init, env) {
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    number(env.MOD_HTTP_TIMEOUT_MS, DEFAULT_HTTP_TIMEOUT_MS, 1_000, 60_000),
  );
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

function originUrl(origin, path) {
  const base = new URL(origin);
  return new URL(path, base).toString();
}

function bearer(token) {
  return token ? { authorization: `Bearer ${token}` } : {};
}

function safeError(error) {
  return String(error?.message || error || "worker request failed")
    .replace(/bearer\s+[\w.\-]+/gi, "Bearer [redacted]")
    .slice(0, 300);
}

function modelPath(model) {
  // Keep model slashes as path separators while escaping names and @ signs.
  return String(model || "").split("/").map(encodeURIComponent).join("/").replaceAll("%40", "@");
}

function mediaUrl(base, source, atMs, width) {
  if (!base) throw new Error("CLOUDFLARE_MEDIA_TRANSFORM_BASE is not configured");
  const seconds = Math.max(0, Number(atMs || 0)) / 1000;
  const options = `mode=frame,time=${seconds.toFixed(1)}s,format=jpg,width=${width},fit=scale-down`;
  return `${base.replace(/\/$/, "")}/cdn-cgi/media/${options}/${encodeURIComponent(source)}`;
}

async function readResponse(response, label) {
  const text = await response.text();
  if (!response.ok) throw new Error(`${label} returned ${response.status}: ${safeError(text)}`);
  return text;
}

async function fetchFrame(lease, atMs, env) {
  const response = await fetchWithTimeout(mediaUrl(
    env.CLOUDFLARE_MEDIA_TRANSFORM_BASE,
    lease.source_url,
    atMs,
    number(lease.frame_width, 640, 64, 1920),
  ), undefined, env);
  if (!response.ok) await readResponse(response, "Media Transformations");
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.length === 0) throw new Error("Media Transformations returned an empty frame");
  return bytes;
}

async function callWorkersAi(model, payload, env) {
  if (!env.CLOUDFLARE_ACCOUNT_ID || !env.CLOUDFLARE_API_TOKEN) {
    throw new Error("CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN is not configured");
  }
  const url = `${API_ROOT}/accounts/${encodeURIComponent(env.CLOUDFLARE_ACCOUNT_ID)}/ai/run/${modelPath(model)}`;
  const response = await fetchWithTimeout(url, {
    method: "POST",
    headers: {
      ...bearer(env.CLOUDFLARE_API_TOKEN),
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  }, env);
  const text = await readResponse(response, "Workers AI");
  let decoded;
  try {
    decoded = JSON.parse(text);
  } catch {
    throw new Error("Workers AI returned invalid JSON");
  }
  if (!decoded.success) {
    throw new Error(`Workers AI reported failure: ${safeError(decoded.errors?.[0]?.message)}`);
  }
  return decoded.result || {};
}

async function classifyFrame(lease, image, env) {
  const bytes = Array.from(image);
  const payload = {
    max_tokens: number(lease.vision_max_tokens, DEFAULT_MAX_TOKENS, 1, 2048),
    temperature: 0.1,
    stream: false,
  };

  if (lease.vision_input_format === "messages") {
    payload.messages = [
      { role: "system", content: lease.prompt || "Classify the supplied frame and return JSON." },
      {
        role: "user",
        content: [
          { type: "text", text: "Classify only the supplied frame. Return the required JSON object." },
          { type: "image_url", image_url: { url: `data:image/jpeg;base64,${bytesToBase64(image)}` } },
        ],
      },
    ];
  } else {
    payload.image = bytes;
    payload.prompt = lease.prompt || "Classify the supplied frame and return JSON.";
  }

  if (lease.vision_response_format) payload.response_format = lease.vision_response_format;
  const result = await callWorkersAi(lease.vision_model, payload, env);
  const response = result.response;
  if (typeof response === "string") return response;
  return JSON.stringify(response ?? null);
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

async function transcribe(lease, audio, env) {
  const result = await callWorkersAi(lease.transcribe_model, { audio: Array.from(audio) }, env);
  if (typeof result.text !== "string") throw new Error("Workers AI transcription returned no text");
  return result.text.trim();
}

async function leaseJobs(env) {
  if (!env.DJANGO_ORIGIN || !env.MODERATION_WORKER_TOKEN) {
    throw new Error("DJANGO_ORIGIN or MODERATION_WORKER_TOKEN is not configured");
  }
  const maxJobs = number(env.MOD_MAX_JOBS, DEFAULT_MAX_JOBS, 1, 4);
  const url = originUrl(env.DJANGO_ORIGIN, `/api/internal/moderation/lease/?limit=${maxJobs}`);
  const response = await fetchWithTimeout(url, {
    headers: {
      ...bearer(env.MODERATION_WORKER_TOKEN),
      "x-moderation-vision-formats": "image,messages",
    },
  }, env);
  const text = await readResponse(response, "Django lease");
  const decoded = JSON.parse(text);
  return Array.isArray(decoded.leases) ? decoded.leases : [];
}

async function settle(env, payload) {
  const response = await fetchWithTimeout(originUrl(env.DJANGO_ORIGIN, "/api/internal/moderation/settle/"), {
    method: "POST",
    headers: {
      ...bearer(env.MODERATION_WORKER_TOKEN),
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  }, env);
  await readResponse(response, "Django settle");
}

async function processLease(lease, env) {
  const timestamps = Array.isArray(lease.timestamps_ms)
    ? lease.timestamps_ms.slice(0, number(env.MOD_MAX_FRAMES, DEFAULT_MAX_FRAMES, 1, 12))
    : [];
  if (!lease.job_id || !/^https?:\/\//i.test(String(lease.source_url || "")) || timestamps.length === 0) {
    await settle(env, { job_id: lease.job_id, error: "lease contains an invalid source or no frame timestamps", retryable: false });
    return;
  }

  const frames = await Promise.all(timestamps.map(async (atMs) => {
    try {
      const image = await fetchFrame(lease, atMs, env);
      return { at_ms: atMs, raw: await classifyFrame(lease, image, env) };
    } catch (error) {
      return { at_ms: atMs, error: safeError(error) };
    }
  }));

  const result = { job_id: lease.job_id, frames, model_used: lease.vision_model };
  if (lease.want_audio) {
    try {
      const duration = Math.min(Math.max(Math.floor(Number(lease.duration_ms || 1000) / 1000), 1), 60);
      const audioUrl = `${env.CLOUDFLARE_MEDIA_TRANSFORM_BASE.replace(/\/$/, "")}/cdn-cgi/media/mode=audio,duration=${duration}s/${encodeURIComponent(lease.source_url)}`;
      const audioResponse = await fetchWithTimeout(audioUrl, undefined, env);
      if (audioResponse.ok) {
        result.transcript = await transcribe(lease, new Uint8Array(await audioResponse.arrayBuffer()), env);
        result.audio_checked = true;
      }
    } catch (error) {
      console.log(`audio skipped: ${safeError(error)}`);
    }
  }
  await settle(env, result);
}

async function runOnce(env) {
  const leases = await leaseJobs(env);
  const results = await Promise.allSettled(leases.map((lease) => processLease(lease, env)));
  const failed = results.filter((result) => result.status === "rejected");
  if (failed.length) console.log(`moderation failures: ${failed.length}/${results.length}`);
  return { leased: leases.length, failed: failed.length };
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(runOnce(env).catch((error) => {
      console.log(`scheduled moderation failed: ${safeError(error)}`);
    }));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/healthz" && request.method === "GET") return json({ ok: true });
    if (url.pathname === "/run" && request.method === "POST") {
      if (!env.MODERATION_ADMIN_TOKEN || request.headers.get("authorization") !== `Bearer ${env.MODERATION_ADMIN_TOKEN}`) {
        return json({ detail: "unauthorized" }, 401);
      }
      try {
        return json(await runOnce(env));
      } catch (error) {
        return json({ detail: safeError(error) }, 500);
      }
    }
    return json({ detail: "not found" }, 404);
  },
};
