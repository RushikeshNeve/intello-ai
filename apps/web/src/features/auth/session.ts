import { getAuthConfig, type AuthConfig } from "./config";
import { sessionCookieName, readCookie } from "./cookies";
import { refreshSession } from "./flow";
import { authLog } from "./log";
import { getOidcClient, type OidcClient } from "./oidc";
import { unsealSessionRef } from "./seal";
import { getSessionStore, type SessionRecord, type SessionStore, type SessionUser } from "./session-store";

export type AuthDeps = {
  config: AuthConfig;
  store: SessionStore;
  oidc: () => Promise<OidcClient>;
  now: () => number;
};

export function defaultAuthDeps(): AuthDeps {
  const config = getAuthConfig();
  return { config, store: getSessionStore(), oidc: () => getOidcClient(config), now: Date.now };
}

/** Refresh the API token when it has less than this left. */
export const REFRESH_MARGIN_MS = 60_000;

/** Resolves the session record referenced by a (sealed) cookie value, or null. Never refreshes. */
export async function loadSession(deps: AuthDeps, cookieValue: string | undefined): Promise<SessionRecord | null> {
  const sid = await unsealSessionRef(deps.config, cookieValue);
  if (!sid) return null;
  const record = await deps.store.get(sid);
  if (!record || record.expiresAt <= deps.now()) return null;
  return record;
}

export async function loadSessionFromRequest(deps: AuthDeps, request: Request): Promise<SessionRecord | null> {
  return loadSession(deps, readCookie(request, sessionCookieName(deps.config)));
}

/** Minimum remaining lifetime for a token to be forwarded at all. */
const MIN_FORWARD_LIFETIME_MS = 5_000;

export type ApiTokenResult =
  | { ok: true; token: string }
  /** Session is over (refresh rejected or impossible): treat as signed out, clear the cookie. */
  | { ok: false; reason: "ended" }
  /** Provider unreachable and the current token is expired: retry later, keep the session. */
  | { ok: false; reason: "unavailable" };

/** After a transient refresh failure, wait this long before trying the provider again. */
export const REFRESH_RETRY_BACKOFF_MS = 15_000;

const INFLIGHT_KEY = Symbol.for("ukos.web.refreshInflight");
const BACKOFF_KEY = Symbol.for("ukos.web.refreshBackoff");
type G = typeof globalThis & {
  [INFLIGHT_KEY]?: Map<string, Promise<ApiTokenResult>>;
  [BACKOFF_KEY]?: Map<string, number>;
};
function inflight(): Map<string, Promise<ApiTokenResult>> {
  const g = globalThis as G;
  g[INFLIGHT_KEY] ??= new Map();
  return g[INFLIGHT_KEY];
}
function retryAfter(): Map<string, number> {
  const g = globalThis as G;
  g[BACKOFF_KEY] ??= new Map();
  return g[BACKOFF_KEY];
}

/**
 * Returns a bearer token for the API, refreshing it server-side (single flight per session) when it has
 * less than `REFRESH_MARGIN_MS` left. A rejected refresh deletes the session. If the provider is merely
 * unreachable, the still-valid current token keeps being used. The cookie never changes on refresh
 * because it only references the record.
 */
export async function getApiToken(deps: AuthDeps, record: SessionRecord): Promise<ApiTokenResult> {
  const remaining = record.apiTokenExpiresAt - deps.now();
  if (remaining > REFRESH_MARGIN_MS) return { ok: true, token: record.apiToken };
  // After a transient failure, don't make every request wait on a dead provider again.
  const backoff = retryAfter();
  if ((backoff.get(record.id) ?? 0) > deps.now()) {
    return remaining > MIN_FORWARD_LIFETIME_MS ? { ok: true, token: record.apiToken } : { ok: false, reason: "unavailable" };
  }

  const map = inflight();
  let job = map.get(record.id);
  if (!job) {
    job = (async (): Promise<ApiTokenResult> => {
      let outcome;
      try {
        outcome = await refreshSession(deps.config, await deps.oidc(), record);
      } catch {
        outcome = { kind: "unavailable" as const, reason: "oidc_client_unavailable" };
      }
      backoff.delete(record.id);
      if (outcome.kind === "ok") {
        await deps.store.set(outcome.record);
        return { ok: true, token: outcome.record.apiToken };
      }
      if (outcome.kind === "unavailable") {
        backoff.set(record.id, deps.now() + REFRESH_RETRY_BACKOFF_MS);
        if (record.apiTokenExpiresAt - deps.now() > MIN_FORWARD_LIFETIME_MS) return { ok: true, token: record.apiToken };
        return { ok: false, reason: "unavailable" };
      }
      await deps.store.delete(record.id);
      authLog("info", "auth.session.ended", { reason: "refresh_rejected" });
      return { ok: false, reason: "ended" };
    })().finally(() => map.delete(record.id));
    map.set(record.id, job);
  }
  return job;
}

export async function destroySession(deps: AuthDeps, record: SessionRecord | null): Promise<void> {
  if (record) await deps.store.delete(record.id);
}

/** The narrow, browser-safe view of a session. Never includes tokens. */
export type PublicSessionUser = { email: string | null; name: string | null; subject: string };

export function toPublicUser(user: SessionUser): PublicSessionUser {
  return { email: user.email, name: user.name, subject: user.subject };
}
