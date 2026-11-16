import * as client from "openid-client";
import { randomBytes, timingSafeEqual } from "node:crypto";
import type { AuthConfig } from "./config";
import { authLog, errorReason } from "./log";
import type { OidcClient } from "./oidc";
import { sealTransaction, type LoginTransaction } from "./seal";
import type { SessionRecord, SessionStore, SessionUser } from "./session-store";
import { safeReturnTo } from "./return-to";
import { TX_TTL_SECONDS } from "./cookies";

export type LoginErrorCode =
  | "missing_transaction"
  | "invalid_state"
  | "replayed"
  | "provider_error"
  | "exchange_failed"
  | "invalid_id_token"
  | "audience_missing";

export class LoginError extends Error {
  constructor(
    readonly code: LoginErrorCode,
    readonly reason: string,
  ) {
    super(code);
    this.name = "LoginError";
  }
}

/** Builds the authorization redirect: fresh state, nonce and PKCE (S256) verifier per login. */
export async function startLogin(
  config: AuthConfig,
  oidc: OidcClient,
  returnTo: string | null,
  now: () => number = Date.now,
): Promise<{ authorizationUrl: URL; txSeal: string; tx: LoginTransaction }> {
  const codeVerifier = client.randomPKCECodeVerifier();
  const tx: LoginTransaction = {
    v: 1,
    state: client.randomState(),
    nonce: client.randomNonce(),
    codeVerifier,
    returnTo: safeReturnTo(returnTo),
    createdAt: now(),
  };
  const authorizationUrl = client.buildAuthorizationUrl(oidc.configuration, {
    redirect_uri: config.redirectUri,
    response_type: "code",
    scope: config.scope,
    state: tx.state,
    nonce: tx.nonce,
    code_challenge: await client.calculatePKCECodeChallenge(codeVerifier),
    code_challenge_method: "S256",
  });
  return { authorizationUrl, txSeal: await sealTransaction(config, tx), tx };
}

function sameString(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  return ab.length === bb.length && timingSafeEqual(ab, bb);
}

export function newSessionId(): string {
  return randomBytes(32).toString("base64url");
}

export type CompletedLogin = { record: SessionRecord; returnTo: string };

/**
 * Handles `/api/auth/callback`: verifies state (constant-time, single use), exchanges the code with the
 * PKCE verifier at the internal token endpoint, verifies the ID token (signature, iss, aud ∋ client id,
 * nonce, exp/iat) and the bearer token we will forward (aud ∋ API audience), then builds a new
 * session record. The caller persists it and rotates the cookie.
 */
export async function completeLogin(
  config: AuthConfig,
  oidc: OidcClient,
  store: SessionStore,
  callbackParams: URLSearchParams,
  tx: LoginTransaction | null,
  now: () => number = Date.now,
): Promise<CompletedLogin> {
  if (!tx) throw new LoginError("missing_transaction", "no or invalid tx cookie");
  if (now() - tx.createdAt > TX_TTL_SECONDS * 1000) throw new LoginError("missing_transaction", "tx expired");

  const state = callbackParams.get("state") ?? "";
  if (!state || !sameString(state, tx.state)) throw new LoginError("invalid_state", "state mismatch");
  // Single use, recorded before any network call so a replayed callback can never race the original.
  if (!(await store.consumeState(tx.state, TX_TTL_SECONDS * 1000))) {
    throw new LoginError("replayed", "state already used");
  }
  if (callbackParams.has("error")) {
    throw new LoginError("provider_error", String(callbackParams.get("error")).slice(0, 64));
  }

  const currentUrl = new URL(config.redirectUri);
  for (const [k, v] of callbackParams) currentUrl.searchParams.append(k, v);

  let tokens: Awaited<ReturnType<typeof client.authorizationCodeGrant>>;
  try {
    tokens = await client.authorizationCodeGrant(oidc.configuration, currentUrl, {
      pkceCodeVerifier: tx.codeVerifier,
      expectedState: tx.state,
      expectedNonce: tx.nonce,
      idTokenExpected: true,
    });
  } catch (err) {
    throw new LoginError("exchange_failed", errorReason(err));
  }

  const idToken = tokens.id_token;
  if (!idToken) throw new LoginError("invalid_id_token", "missing id_token");
  let idClaims;
  try {
    idClaims = await oidc.verifyJwt(idToken, config.clientId);
  } catch (err) {
    throw new LoginError("invalid_id_token", errorReason(err));
  }
  if (typeof idClaims.nonce !== "string" || !sameString(idClaims.nonce, tx.nonce)) {
    throw new LoginError("invalid_id_token", "nonce mismatch");
  }

  const api = await selectApiToken(config, oidc, tokens.access_token, idToken);
  if (!api) throw new LoginError("audience_missing", "no token carries the API audience");
  if (api.subject !== idClaims.sub) throw new LoginError("invalid_id_token", "subject mismatch");

  const user: SessionUser = {
    issuer: config.issuerUrl,
    subject: idClaims.sub!,
    email: typeof idClaims.email === "string" ? idClaims.email : null,
    name: typeof idClaims.name === "string" ? idClaims.name : null,
  };
  const t = now();
  const record: SessionRecord = {
    id: newSessionId(),
    createdAt: t,
    expiresAt: t + config.sessionTtlSeconds * 1000,
    user,
    apiToken: api.token,
    apiTokenExpiresAt: api.expiresAt,
    refreshToken: tokens.refresh_token ?? null,
  };
  return { record, returnTo: safeReturnTo(tx.returnTo) };
}

/**
 * The API accepts a JWT whose `aud` contains `OIDC_AUDIENCE`. Dex issues the access token as such a
 * JWT when the `audience:server:client_id:` scope is granted; the ID token carries the same audience,
 * so it is the fallback. Whichever is chosen is fully verified first.
 */
export async function selectApiToken(
  config: AuthConfig,
  oidc: OidcClient,
  accessToken: string | undefined,
  idToken: string | undefined,
): Promise<{ token: string; expiresAt: number; subject: string } | null> {
  for (const candidate of [accessToken, idToken]) {
    if (!candidate || candidate.split(".").length !== 3) continue;
    try {
      const claims = await oidc.verifyJwt(candidate, config.audience);
      return { token: candidate, expiresAt: (claims.exp ?? 0) * 1000, subject: String(claims.sub) };
    } catch {
      // try the next candidate
    }
  }
  return null;
}

/**
 * - `ok`: new tokens, verified.
 * - `rejected`: the provider refused (e.g. `invalid_grant`) or returned unusable tokens. The session is over.
 * - `unavailable`: network error / timeout / 5xx. Transient; the session survives while its token is valid.
 */
export type RefreshOutcome =
  | { kind: "ok"; record: SessionRecord }
  | { kind: "rejected"; reason: string }
  | { kind: "unavailable"; reason: string };

/** Refresh-token grant at the internal token endpoint. Never mutates the input record. */
export async function refreshSession(
  config: AuthConfig,
  oidc: OidcClient,
  record: SessionRecord,
): Promise<RefreshOutcome> {
  if (!record.refreshToken) return { kind: "rejected", reason: "no_refresh_token" };
  let tokens: Awaited<ReturnType<typeof client.refreshTokenGrant>>;
  try {
    // No `scope` parameter: the provider re-issues the originally granted scopes (incl. the audience).
    tokens = await client.refreshTokenGrant(oidc.configuration, record.refreshToken);
  } catch (err) {
    const reason = errorReason(err);
    const rejected =
      err instanceof client.ResponseBodyError && err.status >= 400 && err.status < 500;
    authLog("warn", rejected ? "auth.refresh.rejected" : "auth.refresh.unavailable", { reason });
    return rejected ? { kind: "rejected", reason } : { kind: "unavailable", reason };
  }
  const api = await selectApiToken(config, oidc, tokens.access_token, tokens.id_token);
  if (!api || api.subject !== record.user.subject) {
    const reason = api ? "subject_changed" : "audience_missing";
    authLog("warn", "auth.refresh.rejected", { reason });
    return { kind: "rejected", reason };
  }
  authLog("info", "auth.refresh.ok");
  return {
    kind: "ok",
    record: {
      ...record,
      apiToken: api.token,
      apiTokenExpiresAt: api.expiresAt,
      // Dex rotates refresh tokens; keep the old one only if none was returned.
      refreshToken: tokens.refresh_token ?? record.refreshToken,
    },
  };
}
