/**
 * CSRF guard for state-changing requests that ride on the session cookie (BFF writes, logout).
 * SameSite=Lax already withholds the cookie from cross-site subresource POSTs; this is the explicit
 * second layer:
 *
 * 1. `Sec-Fetch-Site`, when present (all current browsers), must be `same-origin`.
 * 2. `Origin`, when present, must equal the app's public origin.
 * 3. A request with neither header is rejected: browsers always send at least one on unsafe methods,
 *    so its absence means a non-browser client, which has no business using the cookie session.
 */
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export type CsrfResult = { ok: true } | { ok: false; reason: "cross_site" | "origin_mismatch" | "origin_missing" };

export function checkCsrf(request: Request, publicOrigin: string): CsrfResult {
  if (SAFE_METHODS.has(request.method.toUpperCase())) return { ok: true };
  const site = request.headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin") return { ok: false, reason: "cross_site" };
  const origin = request.headers.get("origin");
  if (origin !== null) return origin === publicOrigin ? { ok: true } : { ok: false, reason: "origin_mismatch" };
  return site === "same-origin" ? { ok: true } : { ok: false, reason: "origin_missing" };
}
