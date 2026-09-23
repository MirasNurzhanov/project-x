const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function originUrl(origin, requestUrl) {
  const configured = new URL(origin);
  const incoming = new URL(requestUrl);
  configured.pathname = incoming.pathname;
  configured.search = incoming.search;
  return configured;
}

function forwardedRequest(request, target) {
  // Constructing a new Request preserves the body stream and WebSocket
  // upgrade request while replacing only the destination origin.
  const forwarded = new Request(target, request);
  const isWebSocket = request.headers.get("upgrade")?.toLowerCase() === "websocket";
  for (const name of HOP_BY_HOP_HEADERS) {
    // The Upgrade and Connection headers are required for the API's WebSocket
    // endpoint. They remain hop-by-hop for ordinary HTTP requests.
    if (isWebSocket && (name === "upgrade" || name === "connection")) continue;
    forwarded.headers.delete(name);
  }
  forwarded.headers.set("x-forwarded-host", new URL(request.url).host);
  forwarded.headers.set("x-forwarded-proto", "https");
  return forwarded;
}

export default {
  async fetch(request, env) {
    if (!env.BACKEND_ORIGIN) {
      return new Response("BACKEND_ORIGIN is not configured", { status: 500 });
    }

    let target;
    try {
      target = originUrl(env.BACKEND_ORIGIN, request.url);
    } catch {
      return new Response("Invalid BACKEND_ORIGIN", { status: 500 });
    }

    try {
      return await fetch(forwardedRequest(request, target));
    } catch {
      return new Response("Backend origin unavailable", { status: 502 });
    }
  },
};
