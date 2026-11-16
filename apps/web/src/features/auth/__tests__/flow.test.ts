import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { completeLogin, LoginError, startLogin } from "../flow";
import { rewriteToInternal } from "../oidc";
import { unsealTransaction, type LoginTransaction } from "../seal";
import { INTERNAL, ISSUER, PUBLIC_ORIGIN, SUBJECT, testDeps } from "./fake-idp";

async function begin() {
  const ctx = await testDeps();
  const { authorizationUrl, txSeal, tx } = await startLogin(ctx.config, ctx.oidc, "/o/acme");
  ctx.idp.nonce = tx.nonce;
  const params = new URLSearchParams({ code: "auth-code-1", state: tx.state });
  return { ...ctx, authorizationUrl, txSeal, tx, params };
}

async function expectLoginError(p: Promise<unknown>, code: string) {
  const err = await p.then(
    () => null,
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(LoginError);
  expect((err as LoginError).code).toBe(code);
}

describe("startLogin", () => {
  it("builds an S256 PKCE request with fresh state and nonce against the PUBLIC issuer", async () => {
    const { authorizationUrl, tx, config } = await begin();
    expect(authorizationUrl.origin + authorizationUrl.pathname).toBe(`${ISSUER}/auth`);
    const q = authorizationUrl.searchParams;
    expect(q.get("client_id")).toBe("ukos-web");
    expect(q.get("response_type")).toBe("code");
    expect(q.get("redirect_uri")).toBe(`${PUBLIC_ORIGIN}/api/auth/callback`);
    expect(q.get("scope")).toBe("openid email profile offline_access audience:server:client_id:ukos-api");
    expect(q.get("code_challenge_method")).toBe("S256");
    const expected = createHash("sha256").update(tx.codeVerifier).digest("base64url");
    expect(q.get("code_challenge")).toBe(expected);
    expect(q.get("state")).toBe(tx.state);
    expect(q.get("nonce")).toBe(tx.nonce);
    expect(tx.codeVerifier.length).toBeGreaterThanOrEqual(43);
    expect(tx.state.length).toBeGreaterThanOrEqual(32);
    expect(tx.nonce.length).toBeGreaterThanOrEqual(32);
    expect(q.has("client_secret")).toBe(false);
    expect(config.scope).toBe(q.get("scope"));
  });

  it("uses a new state, nonce and verifier for every login", async () => {
    const { config, oidc } = await testDeps();
    const seen = new Set<string>();
    for (let i = 0; i < 20; i++) {
      const { tx } = await startLogin(config, oidc, null);
      for (const v of [tx.state, tx.nonce, tx.codeVerifier]) {
        expect(seen.has(v)).toBe(false);
        seen.add(v);
      }
    }
  });

  it("seals the transaction (verifier/state/nonce not readable, tamper-evident)", async () => {
    const { txSeal, tx, config } = await begin();
    for (const secret of [tx.codeVerifier, tx.state, tx.nonce]) expect(txSeal).not.toContain(secret);
    await expect(unsealTransaction(config, txSeal)).resolves.toEqual(tx);
    const mid = Math.floor(txSeal.length / 2);
    const tampered = txSeal.slice(0, mid) + (txSeal[mid] === "A" ? "B" : "A") + txSeal.slice(mid + 1);
    await expect(unsealTransaction(config, tampered)).resolves.toBeNull();
  });

  it("sanitises returnTo (no open redirect)", async () => {
    const { config, oidc } = await testDeps();
    for (const bad of ["https://evil.test/", "//evil.test", "/\\evil.test", "javascript:alert(1)", "/api/auth/logout"]) {
      const { tx } = await startLogin(config, oidc, bad);
      expect(tx.returnTo).toBe("/home");
    }
  });
});

describe("completeLogin", () => {
  it("exchanges the code at the INTERNAL token endpoint with the PKCE verifier and builds a session", async () => {
    const { config, oidc, store, params, tx, idp } = await begin();
    const { record, returnTo } = await completeLogin(config, oidc, store, params, tx);
    expect(returnTo).toBe("/o/acme");
    expect(idp.tokenRequests).toHaveLength(1);
    const req = idp.tokenRequests[0];
    expect(req.url).toBe(`${INTERNAL}/token`);
    expect(req.params.get("grant_type")).toBe("authorization_code");
    expect(req.params.get("code")).toBe("auth-code-1");
    expect(req.params.get("code_verifier")).toBe(tx.codeVerifier);
    expect(req.params.get("redirect_uri")).toBe(`${PUBLIC_ORIGIN}/api/auth/callback`);
    expect(req.authorization).toMatch(/^Basic /);
    expect(record.user).toEqual({ issuer: ISSUER, subject: SUBJECT, email: "alice@ukos.test", name: "alice" });
    expect(record.id).toMatch(/^[\w-]{43}$/);
    expect(record.refreshToken).toBe("rt-0");
    expect(record.apiToken.split(".")).toHaveLength(3);
  });

  it("rejects a tampered state before any network call", async () => {
    const { config, oidc, store, tx, idp } = await begin();
    const params = new URLSearchParams({ code: "c", state: tx.state.slice(0, -1) + "x" });
    await expectLoginError(completeLogin(config, oidc, store, params, tx), "invalid_state");
    expect(idp.tokenRequests).toHaveLength(0);
  });

  it("rejects a missing state", async () => {
    const { config, oidc, store, tx } = await begin();
    await expectLoginError(completeLogin(config, oidc, store, new URLSearchParams({ code: "c" }), tx), "invalid_state");
  });

  it("rejects a missing or expired transaction", async () => {
    const { config, oidc, store, params, tx } = await begin();
    await expectLoginError(completeLogin(config, oidc, store, params, null), "missing_transaction");
    const old: LoginTransaction = { ...tx, createdAt: Date.now() - 11 * 60 * 1000 };
    await expectLoginError(completeLogin(config, oidc, store, params, old), "missing_transaction");
  });

  it("rejects a replayed callback (state is single use)", async () => {
    const { config, oidc, store, params, tx, idp } = await begin();
    await completeLogin(config, oidc, store, params, tx);
    await expectLoginError(completeLogin(config, oidc, store, params, tx), "replayed");
    expect(idp.tokenRequests).toHaveLength(1);
  });

  it("rejects a nonce mismatch", async () => {
    const { config, oidc, store, params, tx, idp } = await begin();
    idp.nonce = "some-other-nonce-value-000000000000000";
    await expect(completeLogin(config, oidc, store, params, tx)).rejects.toBeInstanceOf(LoginError);
  });

  it("rejects an ID token signed with an unknown key", async () => {
    const { config, oidc, store, params, tx, idp } = await begin();
    idp.signWithOtherKey = true;
    await expectLoginError(completeLogin(config, oidc, store, params, tx), "invalid_id_token");
  });

  it("rejects an ID token for another audience or issuer, or expired", async () => {
    for (const claims of [
      { aud: ["someone-else"] },
      { iss: "https://evil.test/dex" },
      { exp: Math.floor(Date.now() / 1000) - 120 },
    ]) {
      const { config, oidc, store, params, tx, idp } = await begin();
      idp.idClaims = claims;
      await expect(completeLogin(config, oidc, store, params, tx)).rejects.toBeInstanceOf(LoginError);
    }
  });

  it("refuses a session when no token carries the API audience", async () => {
    const { config, oidc, store, params, tx, idp } = await begin();
    idp.accessClaims = { aud: ["ukos-web"] };
    idp.idClaims = { aud: ["ukos-web"] };
    await expectLoginError(completeLogin(config, oidc, store, params, tx), "audience_missing");
  });

  it("maps a provider error response and an exchange failure", async () => {
    const a = await begin();
    const errParams = new URLSearchParams({ state: a.tx.state, error: "access_denied" });
    await expectLoginError(completeLogin(a.config, a.oidc, a.store, errParams, a.tx), "provider_error");

    const b = await begin();
    b.idp.nextTokenResponse = { status: 400, body: { error: "invalid_grant" } };
    await expectLoginError(completeLogin(b.config, b.oidc, b.store, b.params, b.tx), "exchange_failed");
  });
});

describe("split-horizon discovery", () => {
  it("rewrites only server-side endpoints to the internal base", () => {
    const out = rewriteToInternal(
      {
        issuer: ISSUER,
        authorization_endpoint: `${ISSUER}/auth`,
        token_endpoint: `${ISSUER}/token`,
        jwks_uri: `${ISSUER}/keys`,
        userinfo_endpoint: "https://elsewhere.test/userinfo",
      },
      ISSUER,
      INTERNAL,
    );
    expect(out.issuer).toBe(ISSUER);
    expect(out.authorization_endpoint).toBe(`${ISSUER}/auth`);
    expect(out.token_endpoint).toBe(`${INTERNAL}/token`);
    expect(out.jwks_uri).toBe(`${INTERNAL}/keys`);
    expect(out.userinfo_endpoint).toBe("https://elsewhere.test/userinfo");
  });
});
