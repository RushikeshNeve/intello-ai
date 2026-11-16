import { tokenRevocation } from "openid-client";
import {
  clearSessionCookie,
  clearTxCookie,
  readCookie,
  sessionCookie,
  sessionCookieName,
  txCookie,
  txCookieName,
} from "./cookies";
import { checkCsrf } from "./csrf";
import { completeLogin, LoginError, startLogin } from "./flow";
import { authLog, errorReason } from "./log";
import { sealSessionRef, unsealTransaction } from "./seal";
import { destroySession, loadSession, loadSessionFromRequest, toPublicUser, type AuthDeps } from "./session";

const NO_STORE = "no-store";

function redirect(location: string, status: 302 | 303 | 307, cookies: string[] = []): Response {
  const headers = new Headers({ location, "cache-control": NO_STORE });
  for (const c of cookies) headers.append("set-cookie", c);
  return new Response(null, { status, headers });
}

function json(status: number, body: unknown, cookies: string[] = []): Response {
  const headers = new Headers({ "content-type": "application/json", "cache-control": NO_STORE });
  for (const c of cookies) headers.append("set-cookie", c);
  return new Response(JSON.stringify(body), { status, headers });
}

/** GET /api/auth/login?returnTo=/path — starts a new login transaction and redirects to the provider. */
export async function handleLogin(deps: AuthDeps, request: Request): Promise<Response> {
  let oidc;
  try {
    oidc = await deps.oidc();
  } catch {
    return redirect(`${deps.config.publicOrigin}/?auth_error=provider_unavailable`, 303);
  }
  const returnTo = new URL(request.url).searchParams.get("returnTo");
  const { authorizationUrl, txSeal } = await startLogin(deps.config, oidc, returnTo, deps.now);
  authLog("info", "auth.login.started");
  return redirect(authorizationUrl.href, 302, [txCookie(deps.config, txSeal)]);
}

/**
 * GET /api/auth/callback — completes the login. On success the previous session (if any) is destroyed
 * and a brand-new session id is issued (rotation, prevents fixation). Failures never create a session.
 */
export async function handleCallback(deps: AuthDeps, request: Request): Promise<Response> {
  const params = new URL(request.url).searchParams;
  const tx = await unsealTransaction(deps.config, readCookie(request, txCookieName(deps.config)));
  const clearTx = clearTxCookie(deps.config);
  try {
    const oidc = await deps.oidc();
    const { record, returnTo } = await completeLogin(deps.config, oidc, deps.store, params, tx, deps.now);

    const previous = await loadSessionFromRequest(deps, request);
    if (previous) await destroySession(deps, previous);
    await deps.store.set(record);
    const seal = await sealSessionRef(deps.config, record.id);
    authLog("info", "auth.login.success", { rotated: previous !== null });
    return redirect(`${deps.config.publicOrigin}${returnTo}`, 303, [clearTx, sessionCookie(deps.config, seal)]);
  } catch (err) {
    const code = err instanceof LoginError ? err.code : "exchange_failed";
    const reason = err instanceof LoginError ? err.reason : errorReason(err);
    authLog("warn", "auth.login.failed", { code, reason });
    return redirect(`${deps.config.publicOrigin}/?auth_error=${code}`, 303, [clearTx]);
  }
}

/**
 * POST /api/auth/logout — CSRF-checked. Deletes the server-side session (so every copy of the cookie is
 * dead), clears the cookie, and revokes the refresh token when the provider advertises a revocation
 * endpoint. Dex v2.46 does not, so the refresh token is dropped with the record and simply expires.
 */
export async function handleLogout(deps: AuthDeps, request: Request): Promise<Response> {
  const csrf = checkCsrf(request, deps.config.publicOrigin);
  if (!csrf.ok) {
    authLog("warn", "auth.logout.csrf_rejected", { reason: csrf.reason });
    return json(403, { detail: "csrf_rejected" });
  }
  const record = await loadSession(deps, readCookie(request, sessionCookieName(deps.config)));
  let revocation: "revoked" | "unsupported" | "failed" | "none" = "none";
  if (record) {
    await destroySession(deps, record);
    if (record.refreshToken) {
      try {
        const oidc = await deps.oidc();
        if (oidc.supportsRevocation) {
          await tokenRevocation(oidc.configuration, record.refreshToken, { token_type_hint: "refresh_token" });
          revocation = "revoked";
        } else {
          revocation = "unsupported";
        }
      } catch {
        revocation = "failed";
      }
    }
  }
  authLog("info", "auth.logout", { had_session: record !== null, revocation });
  const cookies = [clearSessionCookie(deps.config)];
  const accept = request.headers.get("accept") ?? "";
  if (accept.includes("application/json")) return json(200, { status: "signed_out" }, cookies);
  return redirect(`${deps.config.publicOrigin}/`, 303, cookies);
}

/** GET /api/auth/session — browser-safe session status. Never returns tokens. */
export async function handleSession(deps: AuthDeps, request: Request): Promise<Response> {
  const record = await loadSessionFromRequest(deps, request);
  if (!record) return json(200, { authenticated: false });
  return json(200, { authenticated: true, user: toPublicUser(record.user), expiresAt: record.expiresAt });
}
