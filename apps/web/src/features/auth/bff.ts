import { clearSessionCookie } from "./cookies";
import { checkCsrf } from "./csrf";
import { authLog, errorReason } from "./log";
import { getApiToken, loadSessionFromRequest, type AuthDeps } from "./session";
import type { FetchLike } from "./oidc";

/**
 * Backend-for-frontend proxy: `/api/v1/*` → `${API_INTERNAL_BASE_URL}/v1/*`.
 *
 * Request headers are allow-listed (never deny-listed), so `Cookie`, `Authorization`, `Host`,
 * `X-Forwarded-*` and anything else the browser sends is dropped; the only credential upstream is the
 * session's bearer token. Response headers drop `Set-Cookie` and hop-by-hop headers so the API can
 * never set cookies on the web origin.
 */
const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "accept-language",
  "content-type",
  "if-match",
  "if-none-match",
  "if-modified-since",
  "last-event-id",
  "x-request-id",
] as const;

const DROPPED_RESPONSE_HEADERS = new Set([
  "set-cookie",
  "connection",
  "keep-alive",
  "transfer-encoding",
  "content-encoding",
  "content-length",
  "upgrade",
  "proxy-authenticate",
  "proxy-connection",
  "trailer",
  "te",
  "www-authenticate",
]);

export function buildUpstreamHeaders(incoming: Headers, bearerToken: string): Headers {
  const out = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = incoming.get(name);
    if (value !== null) out.set(name, value);
  }
  out.set("authorization", `Bearer ${bearerToken}`);
  return out;
}

export function filterResponseHeaders(upstream: Headers): Headers {
  const out = new Headers();
  upstream.forEach((value, name) => {
    if (!DROPPED_RESPONSE_HEADERS.has(name.toLowerCase())) out.append(name, value);
  });
  out.set("cache-control", "no-store");
  return out;
}

const SEGMENT = /^[A-Za-z0-9._~\-:@!$&'()*+,;=]+$/;

/** Returns the upstream URL, or null when any segment is empty, a dot-segment, or has odd characters. */
export function buildUpstreamUrl(apiBase: string, segments: readonly string[], search: string): URL | null {
  if (segments.length === 0) return null;
  for (const s of segments) {
    if (s === "." || s === ".." || !SEGMENT.test(s)) return null;
  }
  const url = new URL(`${apiBase}/v1/${segments.map(encodeURIComponent).join("/")}`);
  const base = new URL(apiBase);
  if (url.origin !== base.origin || !url.pathname.startsWith(`${base.pathname.replace(/\/$/, "")}/v1/`)) {
    return null;
  }
  url.search = search;
  return url;
}

function json(status: number, body: unknown, extraHeaders: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...extraHeaders },
  });
}

const MAX_BODY_BYTES = 10 * 1024 * 1024;
const UPSTREAM_TIMEOUT_MS = 30_000;
/** Server-sent event streams (ingestion status, chat run events) live up to 5 minutes server-side (m03 §11); then the client reconnects. */
const STREAM_TIMEOUT_MS = 330_000;

/** `…/chat/runs/{id}/events` is a stream even if the client forgot `Accept: text/event-stream`. */
export function isStreamRequest(segments: readonly string[], accept: string | null): boolean {
  if (accept?.includes("text/event-stream")) return true;
  const n = segments.length;
  return n >= 4 && segments[n - 1] === "events" && segments[n - 3] === "runs" && segments[n - 4] === "chat";
}

export async function proxyToApi(
  deps: AuthDeps,
  request: Request,
  segments: readonly string[],
  upstreamFetch: FetchLike = fetch,
): Promise<Response> {
  const csrf = checkCsrf(request, deps.config.publicOrigin);
  if (!csrf.ok) {
    authLog("warn", "bff.csrf_rejected", { reason: csrf.reason, method: request.method });
    return json(403, { detail: "csrf_rejected" });
  }
  if (!deps.config.apiInternalBaseUrl) return json(503, { detail: "api_not_configured" });

  const record = await loadSessionFromRequest(deps, request);
  if (!record) return json(401, { detail: "unauthenticated" });
  const tokenResult = await getApiToken(deps, record);
  if (!tokenResult.ok) {
    if (tokenResult.reason === "unavailable") return json(503, { detail: "identity_unavailable" });
    return json(401, { detail: "unauthenticated" }, { "set-cookie": clearSessionCookie(deps.config) });
  }
  const token = tokenResult.token;

  const target = buildUpstreamUrl(deps.config.apiInternalBaseUrl, segments, new URL(request.url).search);
  if (!target) return json(400, { detail: "invalid_path" });

  const method = request.method.toUpperCase();
  let body: ArrayBuffer | undefined;
  if (method !== "GET" && method !== "HEAD") {
    body = await request.arrayBuffer();
    if (body.byteLength > MAX_BODY_BYTES) return json(413, { detail: "payload_too_large" });
  }

  let upstream: Response;
  try {
    upstream = await upstreamFetch(target, {
      method,
      headers: buildUpstreamHeaders(request.headers, token),
      body,
      redirect: "manual",
      signal: AbortSignal.timeout(isStreamRequest(segments, request.headers.get("accept")) ? STREAM_TIMEOUT_MS : UPSTREAM_TIMEOUT_MS),
    });
  } catch (err) {
    authLog("error", "bff.upstream_unavailable", { reason: errorReason(err) });
    return json(502, { detail: "upstream_unavailable" });
  }

  if (upstream.status === 401) {
    // The API no longer accepts this session's token: end the session so the user signs in again.
    await deps.store.delete(record.id);
    authLog("info", "auth.session.ended", { reason: "api_401" });
    await upstream.body?.cancel();
    return json(401, { detail: "unauthenticated" }, { "set-cookie": clearSessionCookie(deps.config) });
  }

  return new Response(method === "HEAD" ? null : upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: filterResponseHeaders(upstream.headers),
  });
}
