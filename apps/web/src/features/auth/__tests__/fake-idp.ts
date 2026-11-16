import { createLocalJWKSet, exportJWK, generateKeyPair, SignJWT, type JWK } from "jose";
import { parseAuthConfig, type AuthConfig } from "../config";
import { createOidcClient, type OidcClient } from "../oidc";
import { MemorySessionStore } from "../session-store";
import type { AuthDeps } from "../session";

/**
 * In-process OpenID provider for unit tests: discovery + token endpoint served through an injected
 * fetch, tokens signed with a real RS256 key. Tests tweak `next*` fields to simulate misbehaviour.
 */
export const ISSUER = "https://idp.test/dex";
export const INTERNAL = "http://dex.internal:5556/dex";
export const PUBLIC_ORIGIN = "http://127.0.0.1:23300";
export const SUBJECT = "CiQ2YjFj-alice";

export function testConfig(overrides: Record<string, string | undefined> = {}): AuthConfig {
  return parseAuthConfig({
    UKOS_ENV: "test",
    OIDC_ISSUER_URL: ISSUER,
    OIDC_INTERNAL_URL: INTERNAL,
    OIDC_CLIENT_ID: "ukos-web",
    OIDC_CLIENT_SECRET: "test-client-secret",
    OIDC_AUDIENCE: "ukos-api",
    WEB_PUBLIC_URL: PUBLIC_ORIGIN,
    WEB_SESSION_SECRET: "unit-test-session-secret-0123456789abcdef",
    API_INTERNAL_BASE_URL: "http://api.internal:8000",
    ...overrides,
  });
}

export type TokenRequest = { url: string; params: URLSearchParams; authorization: string | null };

export class FakeIdp {
  readonly tokenRequests: TokenRequest[] = [];
  requests: string[] = [];
  /** Claims merged into the next issued tokens. */
  idClaims: Record<string, unknown> = {};
  accessClaims: Record<string, unknown> = {};
  /** Overrides the next token response entirely (status + JSON). */
  nextTokenResponse: { status: number; body: unknown } | null = null;
  /** Makes the token endpoint throw (network failure). */
  tokenEndpointDown = false;
  /** The nonce to embed in the ID token (normally copied from the authorization request). */
  nonce = "";
  signWithOtherKey = false;
  refreshCounter = 0;

  private constructor(
    private readonly key: CryptoKey,
    private readonly otherKey: CryptoKey,
    readonly jwk: JWK,
  ) {}

  static async create(): Promise<FakeIdp> {
    const { privateKey, publicKey } = await generateKeyPair("RS256");
    const other = await generateKeyPair("RS256");
    const jwk = { ...(await exportJWK(publicKey)), kid: "k1", alg: "RS256", use: "sig" };
    return new FakeIdp(privateKey, other.privateKey, jwk);
  }

  async sign(claims: Record<string, unknown>, ttlSeconds = 900): Promise<string> {
    const now = Math.floor(Date.now() / 1000);
    return new SignJWT({ iss: ISSUER, sub: SUBJECT, iat: now, exp: now + ttlSeconds, ...claims })
      .setProtectedHeader({ alg: "RS256", kid: "k1" })
      .sign(this.signWithOtherKey ? this.otherKey : this.key);
  }

  fetch = async (input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    this.requests.push(url);
    if (url === `${INTERNAL}/.well-known/openid-configuration`) {
      return Response.json({
        issuer: ISSUER,
        authorization_endpoint: `${ISSUER}/auth`,
        token_endpoint: `${ISSUER}/token`,
        jwks_uri: `${ISSUER}/keys`,
        userinfo_endpoint: `${ISSUER}/userinfo`,
        response_types_supported: ["code"],
        id_token_signing_alg_values_supported: ["RS256"],
        code_challenge_methods_supported: ["S256", "plain"],
      });
    }
    if (url === `${INTERNAL}/token`) {
      if (this.tokenEndpointDown) throw new TypeError("fetch failed");
      const params = new URLSearchParams(String(init?.body ?? ""));
      const headers = new Headers(init?.headers);
      this.tokenRequests.push({ url, params, authorization: headers.get("authorization") });
      if (this.nextTokenResponse) {
        const { status, body } = this.nextTokenResponse;
        this.nextTokenResponse = null;
        return Response.json(body, { status });
      }
      const isRefresh = params.get("grant_type") === "refresh_token";
      if (isRefresh) this.refreshCounter++;
      const aud = ["ukos-api", "ukos-web"];
      const common = { aud, azp: "ukos-web", email: "alice@ukos.test", name: "alice" };
      return Response.json({
        access_token: await this.sign({ ...common, ...this.accessClaims }),
        token_type: "bearer",
        expires_in: 899,
        refresh_token: `rt-${this.refreshCounter}`,
        id_token: await this.sign({ ...common, ...(isRefresh ? {} : { nonce: this.nonce }), ...this.idClaims }),
      });
    }
    return new Response("not found", { status: 404 });
  };

  async client(config: AuthConfig): Promise<OidcClient> {
    return createOidcClient(config, { fetch: this.fetch, jwks: createLocalJWKSet({ keys: [this.jwk] }) });
  }
}

export async function testDeps(overrides: Partial<AuthDeps> = {}) {
  const idp = await FakeIdp.create();
  const config = testConfig();
  const oidc = await idp.client(config);
  const store = new MemorySessionStore();
  const deps: AuthDeps = { config, store, oidc: async () => oidc, now: Date.now, ...overrides };
  return { idp, config, oidc, store, deps };
}
