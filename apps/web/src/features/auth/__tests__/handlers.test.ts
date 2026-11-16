import { describe, expect, it, vi } from "vitest";
import { buildUpstreamHeaders, buildUpstreamUrl, filterResponseHeaders, proxyToApi } from "../bff";
import { parseCookieHeader } from "../cookies";
import { handleCallback, handleLogin, handleLogout, handleSession } from "../handlers";
import { getApiToken, REFRESH_MARGIN_MS, type AuthDeps } from "../session";
import type { SessionRecord } from "../session-store";
import { PUBLIC_ORIGIN, testDeps } from "./fake-idp";

vi.spyOn(console, "info").mockImplementation(() => {});
vi.spyOn(console, "warn").mockImplementation(() => {});
vi.spyOn(console, "error").mockImplementation(() => {});

function setCookies(res: Response): Map<string, string> {
  const out = new Map<string, string>();
  for (const raw of res.headers.getSetCookie()) {
    const [pair] = raw.split(";");
    const eq = pair.indexOf("=");
    out.set(pair.slice(0, eq), raw);
  }
  return out;
}
const value = (raw: string | undefined) => (raw ? raw.split(";")[0].split("=").slice(1).join("=") : undefined);

/** Drives login + callback through the real handlers; returns the session cookie value. */
async function signIn(ctx: Awaited<ReturnType<typeof testDeps>>, cookie = "") {
  const start = await handleLogin(ctx.deps, new Request(`${PUBLIC_ORIGIN}/api/auth/login?returnTo=%2Fhome`));
  expect(start.status).toBe(302);
  const authz = new URL(start.headers.get("location")!);
  ctx.idp.nonce = authz.searchParams.get("nonce")!;
  const tx = value(setCookies(start).get("ukos_auth_tx"))!;
  const cb = await handleCallback(
    ctx.deps,
    new Request(`${PUBLIC_ORIGIN}/api/auth/callback?code=c1&state=${authz.searchParams.get("state")}`, {
      headers: { cookie: [`ukos_auth_tx=${tx}`, cookie].filter(Boolean).join("; ") },
    }),
  );
  return { cb, session: value(setCookies(cb).get("ukos_session")) };
}

const withCookie = (sid: string | undefined, init: RequestInit & { url?: string } = {}) =>
  new Request(init.url ?? `${PUBLIC_ORIGIN}/api/auth/session`, {
    ...init,
    headers: { ...(init.headers as Record<string, string>), cookie: `ukos_session=${sid}` },
  });

describe("login/callback handlers", () => {
  it("sets the tx cookie on login and a rotated HttpOnly session cookie on callback", async () => {
    const ctx = await testDeps();
    const { cb, session } = await signIn(ctx);
    expect(cb.status).toBe(303);
    expect(cb.headers.get("location")).toBe(`${PUBLIC_ORIGIN}/home`);
    const cookies = setCookies(cb);
    expect(cookies.get("ukos_auth_tx")).toContain("Max-Age=0");
    expect(cookies.get("ukos_session")).toMatch(/; Path=\/; Max-Age=43200; HttpOnly; SameSite=Lax$/);
    expect(session).toBeTruthy();
    expect(session).not.toMatch(/eyJ/);
    expect(ctx.store.size).toBe(1);
  });

  it("rotation: a callback with an existing session destroys it and issues a new id", async () => {
    const ctx = await testDeps();
    const first = await signIn(ctx);
    const firstRecord = [...(ctx.store as unknown as { sessions: Map<string, SessionRecord> }).sessions.keys()];
    const second = await signIn(ctx, `ukos_session=${first.session}`);
    expect(second.session).not.toBe(first.session);
    expect(ctx.store.size).toBe(1);
    const remaining = [...(ctx.store as unknown as { sessions: Map<string, SessionRecord> }).sessions.keys()];
    expect(remaining).not.toEqual(firstRecord);
    const old = await handleSession(ctx.deps, withCookie(first.session));
    expect(await old.json()).toEqual({ authenticated: false });
  });

  it("a failed callback sets no session cookie and redirects with a stable error code", async () => {
    const ctx = await testDeps();
    const cb = await handleCallback(ctx.deps, new Request(`${PUBLIC_ORIGIN}/api/auth/callback?code=x&state=y`));
    expect(cb.status).toBe(303);
    expect(cb.headers.get("location")).toBe(`${PUBLIC_ORIGIN}/?auth_error=missing_transaction`);
    expect(setCookies(cb).has("ukos_session")).toBe(false);
    expect(ctx.store.size).toBe(0);
  });

  it("session endpoint exposes the user but never a token", async () => {
    const ctx = await testDeps();
    const { session } = await signIn(ctx);
    const res = await handleSession(ctx.deps, withCookie(session));
    const text = await res.text();
    expect(JSON.parse(text)).toMatchObject({ authenticated: true, user: { email: "alice@ukos.test" } });
    expect(text).not.toMatch(/eyJ|rt-0/);
    expect(res.headers.get("cache-control")).toBe("no-store");
  });
});

describe("logout handler", () => {
  it("rejects cross-site logout and keeps the session", async () => {
    const ctx = await testDeps();
    const { session } = await signIn(ctx);
    const res = await handleLogout(
      ctx.deps,
      withCookie(session, { url: `${PUBLIC_ORIGIN}/api/auth/logout`, method: "POST", headers: { origin: "http://evil.test" } }),
    );
    expect(res.status).toBe(403);
    expect(ctx.store.size).toBe(1);
  });

  it("destroys the server-side session, clears the cookie, records unsupported revocation", async () => {
    const ctx = await testDeps();
    const info = vi.spyOn(console, "info");
    const { session } = await signIn(ctx);
    const res = await handleLogout(
      ctx.deps,
      withCookie(session, { url: `${PUBLIC_ORIGIN}/api/auth/logout`, method: "POST", headers: { origin: PUBLIC_ORIGIN } }),
    );
    expect(res.status).toBe(303);
    expect(setCookies(res).get("ukos_session")).toMatch(/^ukos_session=; Path=\/; Max-Age=0;/);
    expect(ctx.store.size).toBe(0);
    expect(info.mock.calls.map((c) => String(c[0])).some((l) => l.includes('"revocation":"unsupported"'))).toBe(true);
    const after = await handleSession(ctx.deps, withCookie(session));
    expect(await after.json()).toEqual({ authenticated: false });
  });
});

describe("BFF header/URL hygiene", () => {
  it("allow-lists request headers: strips Cookie, Authorization, Host, X-Forwarded-*", () => {
    const incoming = new Headers({
      cookie: "ukos_session=abc",
      authorization: "Bearer attacker",
      host: "evil.test",
      "x-forwarded-for": "1.2.3.4",
      "x-forwarded-host": "evil.test",
      origin: PUBLIC_ORIGIN,
      accept: "application/json",
      "content-type": "application/json",
      "x-request-id": "r1",
    });
    const out = buildUpstreamHeaders(incoming, "the-token");
    expect([...out.keys()].sort()).toEqual(["accept", "authorization", "content-type", "x-request-id"]);
    expect(out.get("authorization")).toBe("Bearer the-token");
  });

  it("drops Set-Cookie and hop-by-hop headers from API responses", () => {
    const up = new Headers({ "set-cookie": "a=b", "content-type": "application/json", connection: "close", etag: '"1"' });
    const out = filterResponseHeaders(up);
    expect(out.has("set-cookie")).toBe(false);
    expect(out.has("connection")).toBe(false);
    expect(out.get("etag")).toBe('"1"');
    expect(out.get("cache-control")).toBe("no-store");
  });

  it("builds /v1 URLs and refuses traversal or odd segments", () => {
    expect(buildUpstreamUrl("http://api:8000", ["orgs", "123"], "?limit=5")?.href).toBe("http://api:8000/v1/orgs/123?limit=5");
    for (const bad of [[".."], ["orgs", ".."], ["a/b"], [""], ["%2e%2e"], ["a b"], []]) {
      expect(buildUpstreamUrl("http://api:8000", bad, "")).toBeNull();
    }
  });
});

async function proxied(ctx: Awaited<ReturnType<typeof testDeps>>, req: Request, segments: string[], upstream: typeof fetch) {
  return proxyToApi(ctx.deps, req, segments, upstream as never);
}

describe("BFF proxy", () => {
  it("401 JSON without a session; never calls the API", async () => {
    const ctx = await testDeps();
    const upstream = vi.fn();
    const res = await proxied(ctx, new Request(`${PUBLIC_ORIGIN}/api/v1/me`), ["me"], upstream as never);
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ detail: "unauthenticated" });
    expect(upstream).not.toHaveBeenCalled();
  });

  it("forwards with the bearer token and without the cookie", async () => {
    const ctx = await testDeps();
    const { session } = await signIn(ctx);
    const record = [...(ctx.store as unknown as { sessions: Map<string, SessionRecord> }).sessions.values()][0];
    const upstream = vi.fn(async () => Response.json({ ok: true }, { headers: { "set-cookie": "x=y" } }));
    const res = await proxied(
      ctx,
      withCookie(session, { url: `${PUBLIC_ORIGIN}/api/v1/orgs?limit=2`, headers: { authorization: "Bearer evil" } }),
      ["orgs"],
      upstream as never,
    );
    expect(res.status).toBe(200);
    expect(res.headers.getSetCookie()).toEqual([]);
    const [url, init] = upstream.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.href).toBe("http://api.internal:8000/v1/orgs?limit=2");
    const h = new Headers(init.headers);
    expect(h.get("authorization")).toBe(`Bearer ${record.apiToken}`);
    expect(h.has("cookie")).toBe(false);
    expect(init.redirect).toBe("manual");
  });

  it("rejects cross-site writes with 403 before calling the API", async () => {
    const ctx = await testDeps();
    const { session } = await signIn(ctx);
    const upstream = vi.fn();
    const res = await proxied(
      ctx,
      withCookie(session, {
        url: `${PUBLIC_ORIGIN}/api/v1/orgs`,
        method: "POST",
        headers: { "sec-fetch-site": "cross-site", origin: "http://evil.test" },
        body: "{}",
      }),
      ["orgs"],
      upstream as never,
    );
    expect(res.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("an API 401 ends the session and clears the cookie", async () => {
    const ctx = await testDeps();
    const { session } = await signIn(ctx);
    const upstream = vi.fn(async () => Response.json({ detail: "invalid_token" }, { status: 401 }));
    const res = await proxied(ctx, withCookie(session, { url: `${PUBLIC_ORIGIN}/api/v1/me` }), ["me"], upstream as never);
    expect(res.status).toBe(401);
    expect(parseCookieHeader(res.headers.get("set-cookie")).get("ukos_session")).toBe("");
    expect(ctx.store.size).toBe(0);
  });

  it("maps an unreachable API to 502", async () => {
    const ctx = await testDeps();
    const { session } = await signIn(ctx);
    const upstream = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const res = await proxied(ctx, withCookie(session, { url: `${PUBLIC_ORIGIN}/api/v1/me` }), ["me"], upstream as never);
    expect(res.status).toBe(502);
  });
});

describe("server-side refresh", () => {
  async function nearExpiry() {
    const ctx = await testDeps();
    const { session } = await signIn(ctx);
    const record = [...(ctx.store as unknown as { sessions: Map<string, SessionRecord> }).sessions.values()][0];
    // Pretend the token has 30 s left: inside the refresh margin, still forwardable.
    const deps: AuthDeps = { ...ctx.deps, now: () => record.apiTokenExpiresAt - REFRESH_MARGIN_MS / 2 };
    return { ...ctx, deps, session, record };
  }

  it("refreshes at the INTERNAL token endpoint, updates the record, keeps the session id", async () => {
    const { deps, idp, record, store } = await nearExpiry();
    const result = await getApiToken(deps, record);
    expect(result.ok).toBe(true);
    const refresh = idp.tokenRequests.at(-1)!;
    expect(refresh.params.get("grant_type")).toBe("refresh_token");
    expect(refresh.params.get("refresh_token")).toBe("rt-0");
    const updated = await store.get(record.id);
    expect(updated?.refreshToken).toBe("rt-1");
    expect(result.ok && result.token).toBe(updated?.apiToken);
  });

  it("single flight: concurrent callers share one refresh", async () => {
    const { deps, idp, record } = await nearExpiry();
    const before = idp.tokenRequests.length;
    await Promise.all([getApiToken(deps, record), getApiToken(deps, record), getApiToken(deps, record)]);
    expect(idp.tokenRequests.length - before).toBe(1);
  });

  it("a rejected refresh (invalid_grant) ends the session", async () => {
    const { deps, idp, record, store } = await nearExpiry();
    idp.nextTokenResponse = { status: 400, body: { error: "invalid_grant" } };
    expect(await getApiToken(deps, record)).toEqual({ ok: false, reason: "ended" });
    expect(await store.get(record.id)).toBeNull();
  });

  it("an unreachable provider keeps the still-valid token and backs off", async () => {
    const { deps, idp, record, store } = await nearExpiry();
    idp.tokenEndpointDown = true;
    const before = idp.requests.length;
    expect(await getApiToken(deps, record)).toEqual({ ok: true, token: record.apiToken });
    expect(await getApiToken(deps, record)).toEqual({ ok: true, token: record.apiToken });
    expect(idp.requests.length - before).toBe(1);
    expect(await store.get(record.id)).not.toBeNull();
  });
});
