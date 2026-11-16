// M01.01.02 end-to-end: real login against real Dex, scripted over HTTP (no browser).
// Run through ./run.sh, which starts Dex (project ukos-m01-web), the stub API and `next start`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createPublicKey, createVerify } from "node:crypto";

const WEB = process.env.WEB_URL ?? "http://127.0.0.1:23300";
const ISSUER = process.env.OIDC_ISSUER_URL ?? "http://127.0.0.1:28380/dex";
const STUB = process.env.STUB_URL ?? "http://127.0.0.1:23301";
const AUDIENCE = "ukos-api";
const CLIENT_ID = "ukos-web";
// Test-only Dex users (infrastructure/compose/identity/README.md).
const ALICE = { login: "alice@ukos.test", password: "ukos-dev-alice-password" };
const SESSION_COOKIE = "ukos_session";
const TX_COOKIE = "ukos_auth_tx";
const JWT_RE = /eyJ[\w-]+\.[\w-]+\.[\w-]+/;

// Everything the web app returned to the "browser" (headers + bodies), to prove no token leaks.
const transcript = [];

async function webFetch(path, { cookies = {}, headers = {}, method = "GET", body } = {}) {
  const cookieHeader = Object.entries(cookies)
    .map(([k, v]) => `${k}=${v}`)
    .join("; ");
  const res = await fetch(path.startsWith("http") ? path : WEB + path, {
    method,
    body,
    redirect: "manual",
    headers: { ...(cookieHeader ? { cookie: cookieHeader } : {}), ...headers },
  });
  const text = await res.text();
  transcript.push({ path, status: res.status, headers: [...res.headers.entries()], body: text });
  return { res, text, setCookies: parseSetCookies(res.headers.getSetCookie()) };
}

function parseSetCookies(list) {
  const out = {};
  for (const raw of list) {
    const [pair, ...attrs] = raw.split(";").map((s) => s.trim());
    const eq = pair.indexOf("=");
    const attributes = {};
    for (const a of attrs) {
      const i = a.indexOf("=");
      attributes[(i < 0 ? a : a.slice(0, i)).toLowerCase()] = i < 0 ? true : a.slice(i + 1);
    }
    out[pair.slice(0, eq)] = { value: pair.slice(eq + 1), attributes, raw };
  }
  return out;
}

function unescapeHtml(s) {
  return s.replace(/&amp;/g, "&").replace(/&#43;/g, "+").replace(/&quot;/g, '"');
}

/** Follows Dex's redirects from the authorization URL to its login form. */
async function openDexLoginForm(authorizationUrl) {
  let url = authorizationUrl;
  for (let i = 0; i < 5; i++) {
    const res = await fetch(url, { redirect: "manual" });
    if (res.status >= 300 && res.status < 400) {
      url = new URL(res.headers.get("location"), url).href;
      continue;
    }
    const html = await res.text();
    assert.equal(res.status, 200, "Dex login page");
    const m = html.match(/<form method="post" action="([^"]+)"/);
    assert.ok(m, "Dex login form present");
    return new URL(unescapeHtml(m[1]), url).href;
  }
  throw new Error("too many Dex redirects");
}

/** Posts credentials to Dex; returns the callback URL (or the Dex response when login fails). */
async function submitDexCredentials(formAction, { login, password }) {
  let res = await fetch(formAction, {
    method: "POST",
    redirect: "manual",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ login, password }).toString(),
  });
  let url = formAction;
  for (let i = 0; i < 5 && res.status >= 300 && res.status < 400; i++) {
    const next = new URL(res.headers.get("location"), url).href;
    if (next.startsWith(`${WEB}/api/auth/callback`)) return { callbackUrl: next };
    url = next;
    res = await fetch(url, { redirect: "manual" });
  }
  return { callbackUrl: null, status: res.status, html: await res.text() };
}

/** Starts a login at the web app and drives Dex up to (not including) the callback. */
async function beginLogin(returnTo = "/home", creds = ALICE) {
  const start = await webFetch(`/api/auth/login?returnTo=${encodeURIComponent(returnTo)}`);
  assert.equal(start.res.status, 302);
  const authorizationUrl = start.res.headers.get("location");
  const tx = start.setCookies[TX_COOKIE];
  assert.ok(tx, "tx cookie set");
  const formAction = await openDexLoginForm(authorizationUrl);
  const result = await submitDexCredentials(formAction, creds);
  return { start, authorizationUrl, tx, ...result };
}

async function login(creds = ALICE, extraCookies = {}) {
  const b = await beginLogin("/home", creds);
  assert.ok(b.callbackUrl, "Dex redirected to the callback");
  const cb = await webFetch(b.callbackUrl, { cookies: { ...extraCookies, [TX_COOKIE]: b.tx.value } });
  return { ...b, cb, session: cb.setCookies[SESSION_COOKIE] };
}

async function sessionStatus(cookieValue) {
  const r = await webFetch("/api/auth/session", { cookies: cookieValue ? { [SESSION_COOKIE]: cookieValue } : {} });
  return JSON.parse(r.text);
}

function b64urlJson(part) {
  return JSON.parse(Buffer.from(part, "base64url").toString("utf8"));
}

/** Verifies an RS256 JWT against Dex's JWKS with node:crypto (independent of the app's libraries). */
async function verifyDexJwt(token) {
  const [h, p, s] = token.split(".");
  const header = b64urlJson(h);
  assert.equal(header.alg, "RS256");
  const jwks = await (await fetch(`${ISSUER}/keys`)).json();
  const jwk = jwks.keys.find((k) => k.kid === header.kid);
  assert.ok(jwk, "signing key published by Dex");
  const ok = createVerify("RSA-SHA256").update(`${h}.${p}`).verify(createPublicKey({ key: jwk, format: "jwk" }), Buffer.from(s, "base64url"));
  assert.ok(ok, "valid RS256 signature");
  return b64urlJson(p);
}

async function stubLog() {
  return (await fetch(`${STUB}/__log`)).json();
}

const forwardedTokens = [];

test("unauthenticated: protected page redirects, BFF returns 401 JSON", async () => {
  const page = await webFetch("/home");
  assert.equal(page.res.status, 307);
  // Next.js emits same-origin redirects from the proxy as relative Locations.
  assert.equal(new URL(page.res.headers.get("location"), WEB).href, `${WEB}/api/auth/login?returnTo=%2Fhome`);

  const api = await webFetch("/api/v1/me");
  assert.equal(api.res.status, 401);
  assert.match(api.res.headers.get("content-type"), /application\/json/);
  assert.deepEqual(JSON.parse(api.text), { detail: "unauthenticated" });

  assert.deepEqual(await sessionStatus(null), { authenticated: false });
});

test("login: PKCE S256 + state + nonce request, HttpOnly session cookie, no tokens exposed", async () => {
  const r = await login();
  const authz = new URL(r.authorizationUrl);
  assert.equal(authz.origin + authz.pathname, `${ISSUER}/auth`);
  assert.equal(authz.searchParams.get("client_id"), CLIENT_ID);
  assert.equal(authz.searchParams.get("response_type"), "code");
  assert.equal(authz.searchParams.get("redirect_uri"), `${WEB}/api/auth/callback`);
  assert.equal(authz.searchParams.get("scope"), `openid email profile offline_access audience:server:client_id:${AUDIENCE}`);
  assert.equal(authz.searchParams.get("code_challenge_method"), "S256");
  assert.match(authz.searchParams.get("code_challenge"), /^[\w-]{43}$/);
  assert.ok((authz.searchParams.get("state") ?? "").length >= 32);
  assert.ok((authz.searchParams.get("nonce") ?? "").length >= 32);

  // tx cookie: HttpOnly, Lax, narrow path, short-lived, and its secrets are sealed.
  assert.equal(r.tx.attributes.httponly, true);
  assert.equal(r.tx.attributes.samesite, "Lax");
  assert.equal(r.tx.attributes.path, "/api/auth");
  assert.equal(r.tx.attributes["max-age"], "600");
  assert.ok(!r.tx.value.includes(authz.searchParams.get("state")), "state not readable in tx cookie");

  // Callback: 303 to returnTo, rotates in a new session cookie, clears the tx cookie.
  assert.equal(r.cb.res.status, 303);
  assert.equal(r.cb.res.headers.get("location"), `${WEB}/home`);
  const s = r.session;
  assert.ok(s, "session cookie set");
  assert.equal(s.attributes.httponly, true);
  assert.equal(s.attributes.samesite, "Lax");
  assert.equal(s.attributes.path, "/");
  assert.equal(s.attributes.secure, undefined, "no Secure flag on plain-http origin");
  assert.equal(s.attributes["max-age"], "43200");
  assert.equal(s.attributes.domain, undefined);
  assert.ok(!JWT_RE.test(s.value), "cookie holds no JWT");
  assert.equal(r.cb.setCookies[TX_COOKIE]?.attributes["max-age"], "0");

  const home = await webFetch("/home", { cookies: { [SESSION_COOKIE]: s.value } });
  assert.equal(home.res.status, 200);
  assert.match(home.text, /alice@ukos\.test/);

  const root = await webFetch("/", { cookies: { [SESSION_COOKIE]: s.value } });
  assert.equal(root.res.status, 200);

  const status = await sessionStatus(s.value);
  assert.equal(status.authenticated, true);
  assert.equal(status.user.email, "alice@ukos.test");
  assert.deepEqual(Object.keys(status.user).sort(), ["email", "name", "subject"]);
});

test("BFF: forwards a Dex bearer token (aud ∋ ukos-api), strips Cookie/Authorization, never sets cookies", async () => {
  const { session } = await login();
  const cookies = { [SESSION_COOKIE]: session.value, other: "x" };

  const get = await webFetch("/api/v1/orgs?limit=5", {
    cookies,
    headers: { authorization: "Bearer attacker-token", "x-forwarded-for": "6.6.6.6", "x-request-id": "rid-1" },
  });
  assert.equal(get.res.status, 200);
  assert.equal(get.res.headers.get("cache-control"), "no-store");
  const echoed = JSON.parse(get.text);
  assert.equal(echoed.path, "/v1/orgs");
  assert.equal(echoed.search, "?limit=5");
  assert.equal(echoed.headers.cookie, undefined, "no cookie upstream");
  assert.equal(echoed.headers["x-forwarded-for"], undefined);
  assert.equal(echoed.headers["x-request-id"], "rid-1");
  const auth = echoed.headers.authorization;
  assert.match(auth, /^Bearer eyJ/);
  const token = auth.slice("Bearer ".length);
  assert.notEqual(token, "attacker-token");
  const claims = await verifyDexJwt(token);
  assert.equal(claims.iss, ISSUER);
  assert.ok(claims.aud.includes(AUDIENCE), "aud contains ukos-api");
  assert.ok(claims.exp * 1000 > Date.now());
  forwardedTokens.push(token);

  const post = await webFetch("/api/v1/orgs", {
    method: "POST",
    cookies,
    headers: { origin: WEB, "sec-fetch-site": "same-origin", "content-type": "application/json" },
    body: JSON.stringify({ slug: "acme", name: "Acme" }),
  });
  assert.equal(post.res.status, 200);
  assert.deepEqual(JSON.parse(JSON.parse(post.text).body), { slug: "acme", name: "Acme" });

  const sc = await webFetch("/api/v1/set-cookie", { cookies });
  assert.equal(sc.res.status, 200);
  assert.equal(sc.res.headers.getSetCookie().length, 0, "API Set-Cookie stripped");
});

test("BFF CSRF: cross-site state-changing requests are rejected before reaching the API", async () => {
  const { session } = await login();
  const cookies = { [SESSION_COOKIE]: session.value };
  const before = (await stubLog()).length;
  const cases = [
    { origin: "http://evil.test" },
    { "sec-fetch-site": "cross-site", origin: "http://evil.test" },
    { "sec-fetch-site": "same-site" },
    {},
  ];
  for (const headers of cases) {
    const r = await webFetch("/api/v1/orgs", {
      method: "POST",
      cookies,
      headers: { ...headers, "content-type": "application/json" },
      body: "{}",
    });
    assert.equal(r.res.status, 403, JSON.stringify(headers));
    assert.deepEqual(JSON.parse(r.text), { detail: "csrf_rejected" });
  }
  assert.equal((await stubLog()).length, before, "no rejected request reached the API");
});

test("logout: CSRF-checked, clears cookie, kills the old cookie server-side", async () => {
  const { session } = await login();
  const cookies = { [SESSION_COOKIE]: session.value };

  const forged = await webFetch("/api/auth/logout", { method: "POST", cookies, headers: { origin: "http://evil.test" } });
  assert.equal(forged.res.status, 403);
  assert.equal((await sessionStatus(session.value)).authenticated, true, "forged logout did nothing");

  const getLogout = await webFetch("/api/auth/logout", { cookies });
  assert.equal(getLogout.res.status, 405, "GET logout not allowed");

  const out = await webFetch("/api/auth/logout", {
    method: "POST",
    cookies,
    headers: { origin: WEB, "sec-fetch-site": "same-origin" },
  });
  assert.equal(out.res.status, 303);
  assert.equal(out.res.headers.get("location"), `${WEB}/`);
  const cleared = out.setCookies[SESSION_COOKIE];
  assert.equal(cleared.value, "");
  assert.equal(cleared.attributes["max-age"], "0");
  assert.equal(cleared.attributes.httponly, true);
  assert.equal(cleared.attributes.path, "/");

  // Replaying the old cookie value: everything treats it as signed out.
  const page = await webFetch("/home", { cookies });
  assert.equal(page.res.status, 307);
  assert.match(page.res.headers.get("location"), /\/api\/auth\/login\?returnTo=%2Fhome$/);
  assert.equal((await webFetch("/api/v1/me", { cookies })).res.status, 401);
  assert.equal((await sessionStatus(session.value)).authenticated, false);
});

test("rotation: logging in again issues a new session id and kills the previous one", async () => {
  const first = await login();
  const second = await login(ALICE, { [SESSION_COOKIE]: first.session.value });
  assert.notEqual(second.session.value, first.session.value);
  assert.equal((await sessionStatus(first.session.value)).authenticated, false, "old session dead");
  assert.equal((await sessionStatus(second.session.value)).authenticated, true, "new session alive");
});

test("tampered state is rejected and creates no session", async () => {
  const b = await beginLogin();
  const tampered = new URL(b.callbackUrl);
  tampered.searchParams.set("state", tampered.searchParams.get("state").slice(0, -2) + "xx");
  const cb = await webFetch(tampered.href, { cookies: { [TX_COOKIE]: b.tx.value } });
  assert.equal(cb.res.status, 303);
  assert.equal(cb.res.headers.get("location"), `${WEB}/?auth_error=invalid_state`);
  assert.equal(cb.setCookies[SESSION_COOKIE], undefined);
});

test("replayed callback is rejected and creates no session", async () => {
  const r = await login();
  assert.ok(r.session);
  // Same URL + same tx cookie (an attacker who captured both).
  const replay = await webFetch(r.callbackUrl, { cookies: { [TX_COOKIE]: r.tx.value } });
  assert.equal(replay.res.status, 303);
  assert.equal(replay.res.headers.get("location"), `${WEB}/?auth_error=replayed`);
  assert.equal(replay.setCookies[SESSION_COOKIE], undefined);
  // Without the tx cookie (the browser already dropped it).
  const bare = await webFetch(r.callbackUrl);
  assert.equal(bare.res.headers.get("location"), `${WEB}/?auth_error=missing_transaction`);
  assert.equal(bare.setCookies[SESSION_COOKIE], undefined);
});

test("wrong password: Dex refuses, no callback, no session", async () => {
  const b = await beginLogin("/home", { login: ALICE.login, password: "definitely-wrong" });
  assert.equal(b.callbackUrl, null);
  assert.equal(b.status, 401);
  assert.match(b.html, /Invalid Email Address and password/i);
  assert.deepEqual(await sessionStatus(null), { authenticated: false });
});

test("API 401 ends the session and clears the cookie", async () => {
  const { session } = await login();
  const cookies = { [SESSION_COOKIE]: session.value };
  const r = await webFetch("/api/v1/force-401", { cookies });
  assert.equal(r.res.status, 401);
  assert.deepEqual(JSON.parse(r.text), { detail: "unauthenticated" });
  assert.equal(r.setCookies[SESSION_COOKIE]?.attributes["max-age"], "0");
  assert.equal((await sessionStatus(session.value)).authenticated, false);
  assert.equal((await webFetch("/home", { cookies })).res.status, 307);
});

// Runs LAST among the Dex-dependent tests: Dex v2.46.1 currently hangs on refresh_token grants and then
// stops answering (see the M01.01.02 report). Both outcomes are asserted precisely:
//  - provider refreshes   -> new verified token, aud ∋ ukos-api, same cookie;
//  - provider hangs/fails -> the BFF keeps forwarding the still-valid token (session survives, 200).
// E2E_REQUIRE_REFRESH=1 makes the second outcome a failure (use once Dex refresh works).
test("server-side refresh: near-expiry token is refreshed, or the valid token survives a provider outage", { timeout: 120_000 }, async (t) => {
  const ttl = Number(process.env.OIDC_ID_TOKEN_TTL_SECONDS ?? "0");
  if (!ttl) return t.skip("run.sh sets a short token TTL for this test");
  const { session } = await login();
  const cookies = { [SESSION_COOKIE]: session.value };
  const first = JSON.parse((await webFetch("/api/v1/me", { cookies })).text).headers.authorization.slice(7);
  const firstClaims = await verifyDexJwt(first);
  // The web server refreshes once less than 60 s remain.
  const waitMs = Math.max(0, firstClaims.exp * 1000 - 60_000 - Date.now()) + 2_000;
  await new Promise((r) => setTimeout(r, waitMs));
  const t0 = Date.now();
  const again = await webFetch("/api/v1/me", { cookies });
  assert.equal(again.res.status, 200);
  assert.equal(again.setCookies[SESSION_COOKIE], undefined, "cookie unchanged");
  const second = JSON.parse(again.text).headers.authorization.slice(7);
  forwardedTokens.push(first, second);
  if (second !== first) {
    const claims = await verifyDexJwt(second);
    assert.ok(claims.aud.includes(AUDIENCE));
    assert.equal(claims.sub, firstClaims.sub);
    assert.ok(claims.exp > firstClaims.exp);
    t.diagnostic("provider refreshed the token server-side");
  } else {
    assert.ok(!process.env.E2E_REQUIRE_REFRESH, "refresh required but the provider did not refresh");
    assert.ok(firstClaims.exp * 1000 > Date.now(), "fallback token still valid");
    t.diagnostic(`provider refresh unavailable (${Date.now() - t0} ms, bounded by the 5 s OIDC timeout); still-valid token kept`);
    // Within the backoff window the next call does not wait on the provider again.
    const t1 = Date.now();
    assert.equal((await webFetch("/api/v1/me", { cookies })).res.status, 200);
    assert.ok(Date.now() - t1 < 2_000, "backoff: no second provider wait");
  }
  assert.equal((await sessionStatus(session.value)).authenticated, true, "session survived");
});

test("no token ever reached the browser (headers or bodies)", () => {
  assert.ok(forwardedTokens.length > 0);
  for (const entry of transcript) {
    // BFF echo responses legitimately contain what the stub API saw; skip only those bodies.
    const isEcho = entry.path.includes("/api/v1/") && entry.status === 200;
    const haystack = JSON.stringify(entry.headers) + (isEcho ? "" : entry.body);
    assert.ok(!JWT_RE.test(haystack), `JWT-looking value in response to ${entry.path}`);
    for (const t of forwardedTokens) assert.ok(!haystack.includes(t), `token leaked in ${entry.path}`);
  }
});
