import "server-only";
import { cookies } from "next/headers";
import { sessionCookieName } from "./cookies";
import { authLog } from "./log";
import { buildUpstreamUrl } from "./bff";
import { defaultAuthDeps, getApiToken, loadSession, toPublicUser, type PublicSessionUser } from "./session";
import type { SessionRecord } from "./session-store";

/**
 * Server Component / Server Action helpers (the Data Access Layer for the session).
 *
 * Reading the session reads `cookies()`, which is request-time data: with Cache Components, call these
 * from a component rendered inside a `<Suspense>` boundary.
 */
async function currentRecord(): Promise<SessionRecord | null> {
  // `cookies()` first: it marks the render as request-time before any config is read.
  const jar = await cookies();
  const deps = defaultAuthDeps();
  return loadSession(deps, jar.get(sessionCookieName(deps.config))?.value);
}

/** The signed-in user (no tokens), or null. */
export async function getSessionUser(): Promise<PublicSessionUser | null> {
  const record = await currentRecord();
  return record ? toPublicUser(record.user) : null;
}

/**
 * Calls the API from the server with the session's bearer token, e.g. `apiFetch("/v1/orgs")`.
 * Returns a synthetic 401 JSON response when there is no usable session. On an API 401 the session
 * record is deleted (Server Components cannot write cookies; the stale cookie is then rejected and
 * cleared by the next route-handler or proxy request).
 */
export async function apiFetch(path: `/v1/${string}`, init: RequestInit = {}): Promise<Response> {
  const record = await currentRecord();
  const deps = defaultAuthDeps();
  const unauthenticated = () =>
    new Response(JSON.stringify({ detail: "unauthenticated" }), {
      status: 401,
      headers: { "content-type": "application/json" },
    });
  if (!record) return unauthenticated();
  const tokenResult = await getApiToken(deps, record);
  if (!tokenResult.ok) {
    if (tokenResult.reason === "unavailable") {
      return new Response(JSON.stringify({ detail: "identity_unavailable" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      });
    }
    return unauthenticated();
  }
  const token = tokenResult.token;
  if (!deps.config.apiInternalBaseUrl) throw new Error("API_INTERNAL_BASE_URL is not configured");

  const parsed = new URL(path, "http://ukos.invalid");
  const segments = parsed.pathname.replace(/^\/v1\//, "").split("/");
  const target = buildUpstreamUrl(deps.config.apiInternalBaseUrl, segments, parsed.search);
  if (!target) throw new Error("invalid API path");

  const headers = new Headers(init.headers);
  headers.delete("cookie");
  headers.set("authorization", `Bearer ${token}`);
  if (!headers.has("accept")) headers.set("accept", "application/json");
  const res = await fetch(target, { ...init, headers, redirect: "manual", cache: "no-store" });
  if (res.status === 401) {
    await deps.store.delete(record.id);
    authLog("info", "auth.session.ended", { reason: "api_401" });
  }
  return res;
}
