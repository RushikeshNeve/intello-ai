import * as client from "openid-client";
import { createRemoteJWKSet, jwtVerify, type JWTPayload, type JWTVerifyGetKey } from "jose";
import type { AuthConfig } from "./config";
import { authLog, errorReason } from "./log";

/**
 * OIDC relying party for `ukos-web` built on openid-client v6 (protocol: auth code + PKCE, state,
 * nonce, token/refresh grants) and jose (explicit RS256 signature + claim verification of every JWT
 * we accept, against the provider's JWKS).
 *
 * Split-horizon URLs: the browser is sent to the public issuer (`authorization_endpoint`), but every
 * server-to-server call (discovery, token, JWKS, userinfo) goes to `OIDC_INTERNAL_URL`. Discovery is
 * fetched from the internal base, its `issuer` must equal the public issuer exactly, and endpoints
 * under the public issuer are rewritten to the internal base.
 */
export type FetchLike = (input: string | URL, init?: RequestInit) => Promise<Response>;

export type OidcDeps = {
  fetch?: FetchLike;
  /** Key resolver for JWT verification. Defaults to the provider's remote JWKS (internal URL). */
  jwks?: JWTVerifyGetKey;
};

export type OidcClient = {
  readonly configuration: client.Configuration;
  readonly metadata: client.ServerMetadata;
  /** Verifies signature (RS256), `iss`, `aud ∋ audience`, `exp`, `iat`, `sub`. Throws on failure. */
  verifyJwt(token: string, audience: string): Promise<JWTPayload>;
  readonly supportsRevocation: boolean;
};

const SERVER_SIDE_ENDPOINTS = [
  "token_endpoint",
  "jwks_uri",
  "userinfo_endpoint",
  "introspection_endpoint",
  "revocation_endpoint",
] as const;

export const CLOCK_TOLERANCE_SECONDS = 30;

export function rewriteToInternal(
  metadata: client.ServerMetadata,
  issuerUrl: string,
  internalUrl: string,
): client.ServerMetadata {
  if (issuerUrl === internalUrl) return metadata;
  const out: Record<string, unknown> = { ...metadata };
  for (const key of SERVER_SIDE_ENDPOINTS) {
    const value = out[key];
    if (typeof value === "string" && (value === issuerUrl || value.startsWith(issuerUrl + "/"))) {
      out[key] = internalUrl + value.slice(issuerUrl.length);
    }
  }
  return out as client.ServerMetadata;
}

export async function createOidcClient(config: AuthConfig, deps: OidcDeps = {}): Promise<OidcClient> {
  const doFetch: FetchLike = deps.fetch ?? fetch;
  const discoveryUrl = `${config.internalUrl}/.well-known/openid-configuration`;
  const res = await doFetch(discoveryUrl, {
    headers: { accept: "application/json" },
    redirect: "error",
    signal: AbortSignal.timeout(5000),
  });
  if (!res.ok) throw new Error(`oidc discovery failed: HTTP ${res.status}`);
  const discovered = (await res.json()) as client.ServerMetadata;
  if (discovered.issuer !== config.issuerUrl) {
    throw new Error("oidc discovery issuer mismatch");
  }
  const metadata = rewriteToInternal(discovered, config.issuerUrl, config.internalUrl);

  const configuration = new client.Configuration(
    metadata,
    config.clientId,
    { client_secret: config.clientSecret },
    client.ClientSecretBasic(config.clientSecret),
  );
  const needsHttp = [config.issuerUrl, config.internalUrl].some((u) => u.startsWith("http://"));
  if (needsHttp) {
    if (!config.allowInsecureHttp) throw new Error("plain-http OIDC is only allowed in local/test/ci");
    client.allowInsecureRequests(configuration);
  }
  if (deps.fetch) {
    const f = deps.fetch;
    configuration[client.customFetch] = (url, options) => f(url, options as RequestInit);
  }
  // Seconds. Short, so a hung provider fails fast (refresh then falls back to the still-valid token).
  configuration.timeout = 5;

  if (!metadata.jwks_uri && !deps.jwks) throw new Error("oidc metadata has no jwks_uri");
  const jwks = deps.jwks ?? createRemoteJWKSet(new URL(metadata.jwks_uri!), { timeoutDuration: 5000 });

  return {
    configuration,
    metadata,
    supportsRevocation: typeof metadata.revocation_endpoint === "string",
    async verifyJwt(token, audience) {
      const { payload } = await jwtVerify(token, jwks, {
        issuer: config.issuerUrl,
        audience,
        algorithms: ["RS256"],
        clockTolerance: CLOCK_TOLERANCE_SECONDS,
        requiredClaims: ["exp", "iat", "sub"],
      });
      if (typeof payload.sub !== "string" || payload.sub.length === 0) throw new Error("empty sub");
      if (typeof payload.iat === "number" && payload.iat > Date.now() / 1000 + CLOCK_TOLERANCE_SECONDS) {
        throw new Error("iat in the future");
      }
      return payload;
    },
  };
}

let pending: Promise<OidcClient> | null = null;

/** Process-wide client; discovery is retried on the next call if it failed. */
export function getOidcClient(config: AuthConfig): Promise<OidcClient> {
  if (!pending) {
    pending = createOidcClient(config).catch((err: unknown) => {
      pending = null;
      authLog("error", "auth.oidc.discovery_failed", { reason: errorReason(err) });
      throw err;
    });
  }
  return pending;
}

/** Test hook. */
export function resetOidcClient(): void {
  pending = null;
}
