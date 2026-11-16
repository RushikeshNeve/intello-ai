import { authLog } from "./log";

/**
 * Web-session configuration (contract §6). Read from the environment at request time, never at
 * build time, so one image works in every environment.
 *
 * | Variable | Required | Notes |
 * |---|---|---|
 * | `OIDC_ISSUER_URL` | yes | Public issuer: browser redirects and the expected `iss`. |
 * | `OIDC_INTERNAL_URL` | no | Server-side base for discovery, token exchange and JWKS. Defaults to the issuer. |
 * | `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | yes | Confidential client `ukos-web`. |
 * | `OIDC_AUDIENCE` | yes | The API audience (`ukos-api`); the forwarded token must contain it in `aud`. |
 * | `WEB_PUBLIC_URL` | no | Public origin of the web app. Defaults to the origin of the first `OIDC_REDIRECT_URIS` entry. |
 * | `WEB_SESSION_SECRET` | yes outside local/test | ≥ 32 chars. Seals the session cookie. |
 * | `API_INTERNAL_BASE_URL` | yes for the BFF | API base reachable from the web server. |
 * | `UKOS_ENV` | no | `local` \| `test` \| `ci` enable dev conveniences (secret fallback, plain-http issuer). |
 */
export type AuthConfig = {
  readonly env: string;
  readonly issuerUrl: string;
  readonly internalUrl: string;
  readonly clientId: string;
  readonly clientSecret: string;
  readonly audience: string;
  readonly scope: string;
  readonly publicOrigin: string;
  readonly redirectUri: string;
  /** True when the public origin is https: cookies get `Secure` and the `__Host-` prefix. */
  readonly secureCookies: boolean;
  readonly sessionSecret: string;
  readonly sessionTtlSeconds: number;
  readonly apiInternalBaseUrl: string | null;
  /** Plain-http OIDC endpoints are accepted only in local/test/ci. */
  readonly allowInsecureHttp: boolean;
};

export class AuthConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthConfigError";
  }
}

const DEV_ENVS = new Set(["local", "test", "ci"]);
export const MIN_SESSION_SECRET_LENGTH = 32;
/** Absolute session lifetime. Tokens inside are refreshed server-side well before this. */
export const SESSION_TTL_SECONDS = 12 * 60 * 60;
/**
 * Only used when `UKOS_ENV` is local/test/ci and `WEB_SESSION_SECRET` is unset. It is public, so a
 * session sealed with it is not secret: never acceptable outside a developer machine.
 */
const DEV_FALLBACK_SECRET = "ukos-dev-only-insecure-web-session-secret-do-not-use";

type Env = Record<string, string | undefined>;

function required(env: Env, name: string): string {
  const value = env[name]?.trim();
  if (!value) throw new AuthConfigError(`${name} is required`);
  return value;
}

function trimSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

function parseUrl(name: string, value: string): URL {
  try {
    return new URL(value);
  } catch {
    throw new AuthConfigError(`${name} is not a valid URL`);
  }
}

let warnedFallback = false;

export function parseAuthConfig(env: Env): AuthConfig {
  const ukosEnv = (env.UKOS_ENV ?? "local").trim() || "local";
  const isDev = DEV_ENVS.has(ukosEnv);

  const issuerUrl = trimSlash(required(env, "OIDC_ISSUER_URL"));
  const internalUrl = trimSlash(env.OIDC_INTERNAL_URL?.trim() || issuerUrl);
  const issuer = parseUrl("OIDC_ISSUER_URL", issuerUrl);
  const internal = parseUrl("OIDC_INTERNAL_URL", internalUrl);
  if (!isDev && (issuer.protocol !== "https:" || internal.protocol !== "https:")) {
    throw new AuthConfigError("OIDC URLs must use https outside local/test/ci");
  }

  let publicUrl = env.WEB_PUBLIC_URL?.trim();
  if (!publicUrl) {
    const firstRedirect = env.OIDC_REDIRECT_URIS?.split(",")[0]?.trim();
    if (!firstRedirect) throw new AuthConfigError("WEB_PUBLIC_URL (or OIDC_REDIRECT_URIS) is required");
    publicUrl = parseUrl("OIDC_REDIRECT_URIS", firstRedirect).origin;
  }
  const publicOrigin = parseUrl("WEB_PUBLIC_URL", publicUrl).origin;
  const secureCookies = publicOrigin.startsWith("https://");

  let sessionSecret = env.WEB_SESSION_SECRET ?? "";
  if (!sessionSecret) {
    if (!isDev) throw new AuthConfigError("WEB_SESSION_SECRET is required outside local/test/ci");
    if (!warnedFallback) {
      warnedFallback = true;
      authLog("warn", "auth.config.dev_session_secret", {
        detail: "WEB_SESSION_SECRET unset; using the public dev fallback (UKOS_ENV=" + ukosEnv + ")",
      });
    }
    sessionSecret = DEV_FALLBACK_SECRET;
  } else if (sessionSecret.length < MIN_SESSION_SECRET_LENGTH) {
    throw new AuthConfigError(`WEB_SESSION_SECRET must be at least ${MIN_SESSION_SECRET_LENGTH} characters`);
  }

  const audience = required(env, "OIDC_AUDIENCE");
  const apiBase = env.API_INTERNAL_BASE_URL?.trim();

  return {
    env: ukosEnv,
    issuerUrl,
    internalUrl,
    clientId: required(env, "OIDC_CLIENT_ID"),
    clientSecret: required(env, "OIDC_CLIENT_SECRET"),
    audience,
    scope: `openid email profile offline_access audience:server:client_id:${audience}`,
    publicOrigin,
    redirectUri: `${publicOrigin}/api/auth/callback`,
    secureCookies,
    sessionSecret,
    sessionTtlSeconds: SESSION_TTL_SECONDS,
    apiInternalBaseUrl: apiBase ? trimSlash(parseUrl("API_INTERNAL_BASE_URL", apiBase).toString()) : null,
    allowInsecureHttp: isDev,
  };
}

let cached: AuthConfig | null = null;

/** Process-wide configuration, parsed on first use (request time). */
export function getAuthConfig(): AuthConfig {
  cached ??= parseAuthConfig(process.env);
  return cached;
}
