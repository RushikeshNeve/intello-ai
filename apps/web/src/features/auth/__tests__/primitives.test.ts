import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthConfigError, parseAuthConfig } from "../config";
import {
  clearSessionCookie,
  clearTxCookie,
  parseCookieHeader,
  sessionCookie,
  sessionCookieName,
  txCookie,
} from "../cookies";
import { checkCsrf } from "../csrf";
import { errorReason, redactFields } from "../log";
import { safeReturnTo } from "../return-to";
import { sealSessionRef, unsealSessionRef } from "../seal";
import { PUBLIC_ORIGIN, testConfig } from "./fake-idp";

const BASE = {
  OIDC_ISSUER_URL: "http://127.0.0.1:8380/dex",
  OIDC_CLIENT_ID: "ukos-web",
  OIDC_CLIENT_SECRET: "s",
  OIDC_AUDIENCE: "ukos-api",
  OIDC_REDIRECT_URIS: "http://127.0.0.1:3300/api/auth/callback,http://localhost:3300/api/auth/callback",
};

describe("config", () => {
  afterEach(() => vi.restoreAllMocks());

  it("requires WEB_SESSION_SECRET outside local/test/ci", () => {
    expect(() => parseAuthConfig({ ...BASE, UKOS_ENV: "production", OIDC_ISSUER_URL: "https://id.example/dex" })).toThrow(
      /WEB_SESSION_SECRET is required/,
    );
  });

  it("rejects a session secret shorter than 32 characters", () => {
    expect(() => parseAuthConfig({ ...BASE, UKOS_ENV: "local", WEB_SESSION_SECRET: "x".repeat(31) })).toThrow(
      AuthConfigError,
    );
  });

  it("falls back to a dev secret in local/test with a warning that never prints the secret", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const cfg = parseAuthConfig({ ...BASE, UKOS_ENV: "test" });
    expect(cfg.sessionSecret.length).toBeGreaterThanOrEqual(32);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).not.toContain(cfg.sessionSecret);
  });

  it("derives the public origin from OIDC_REDIRECT_URIS and defaults the internal URL to the issuer", () => {
    const cfg = parseAuthConfig({ ...BASE, UKOS_ENV: "local", WEB_SESSION_SECRET: "y".repeat(40) });
    expect(cfg.publicOrigin).toBe("http://127.0.0.1:3300");
    expect(cfg.redirectUri).toBe("http://127.0.0.1:3300/api/auth/callback");
    expect(cfg.internalUrl).toBe(cfg.issuerUrl);
    expect(cfg.secureCookies).toBe(false);
    expect(cfg.scope).toBe("openid email profile offline_access audience:server:client_id:ukos-api");
  });

  it("enables secure cookies for an https public origin and requires https OIDC in production", () => {
    const prod = parseAuthConfig({
      ...BASE,
      UKOS_ENV: "production",
      OIDC_ISSUER_URL: "https://id.example/dex",
      WEB_PUBLIC_URL: "https://app.example",
      WEB_SESSION_SECRET: "z".repeat(48),
    });
    expect(prod.secureCookies).toBe(true);
    expect(prod.allowInsecureHttp).toBe(false);
    expect(() => parseAuthConfig({ ...BASE, UKOS_ENV: "production", WEB_SESSION_SECRET: "z".repeat(48) })).toThrow(
      /https/,
    );
  });
});

function attrs(setCookie: string) {
  const [pair, ...rest] = setCookie.split("; ");
  return { pair, attrs: rest };
}

describe("cookies", () => {
  it("session cookie: HttpOnly, SameSite=Lax, Path=/, Max-Age=TTL, no Secure on http, no Domain", () => {
    const cfg = testConfig();
    const { pair, attrs: a } = attrs(sessionCookie(cfg, "sealed-value"));
    expect(pair).toBe("ukos_session=sealed-value");
    expect(a).toEqual(["Path=/", "Max-Age=43200", "HttpOnly", "SameSite=Lax"]);
  });

  it("session cookie on https: Secure and the __Host- prefix", () => {
    const cfg = testConfig({ WEB_PUBLIC_URL: "https://app.example" });
    expect(sessionCookieName(cfg)).toBe("__Host-ukos_session");
    const { attrs: a } = attrs(sessionCookie(cfg, "v"));
    expect(a).toContain("Secure");
    expect(a).toContain("Path=/");
    expect(a.some((x) => x.startsWith("Domain"))).toBe(false);
  });

  it("tx cookie: narrow path, 10 minutes, HttpOnly, Lax", () => {
    const { attrs: a } = attrs(txCookie(testConfig(), "v"));
    expect(a).toEqual(["Path=/api/auth", "Max-Age=600", "HttpOnly", "SameSite=Lax"]);
  });

  it("clearing cookies expires them with the same attributes", () => {
    const cfg = testConfig();
    expect(clearSessionCookie(cfg)).toBe(
      "ukos_session=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Lax",
    );
    expect(clearTxCookie(cfg)).toContain("Path=/api/auth; Max-Age=0");
  });

  it("rejects values that could inject attributes", () => {
    expect(() => sessionCookie(testConfig(), "a; Domain=evil.test")).toThrow();
  });

  it("parses Cookie headers", () => {
    const m = parseCookieHeader('a=1; ukos_session=abc*def~2; b="q"; a=2');
    expect(m.get("a")).toBe("1");
    expect(m.get("ukos_session")).toBe("abc*def~2");
    expect(m.get("b")).toBe("q");
  });
});

describe("session reference seal", () => {
  it("round-trips the opaque id and rejects tampering, other secrets and garbage", async () => {
    const cfg = testConfig();
    const sid = "x".repeat(43);
    const seal = await sealSessionRef(cfg, sid);
    expect(seal).not.toContain(sid);
    await expect(unsealSessionRef(cfg, seal)).resolves.toBe(sid);
    const mid = Math.floor(seal.length / 2);
    const tampered = seal.slice(0, mid) + (seal[mid] === "A" ? "B" : "A") + seal.slice(mid + 1);
    await expect(unsealSessionRef(cfg, tampered)).resolves.toBeNull();
    const other = testConfig({ WEB_SESSION_SECRET: "another-secret-another-secret-12345" });
    await expect(unsealSessionRef(other, seal)).resolves.toBeNull();
    await expect(unsealSessionRef(cfg, "not-a-seal")).resolves.toBeNull();
    await expect(unsealSessionRef(cfg, undefined)).resolves.toBeNull();
  });
});

function req(method: string, headers: Record<string, string>) {
  return new Request(`${PUBLIC_ORIGIN}/api/v1/orgs`, { method, headers });
}

describe("csrf", () => {
  it("allows safe methods regardless of headers", () => {
    for (const m of ["GET", "HEAD", "OPTIONS"]) {
      expect(checkCsrf(req(m, { origin: "http://evil.test", "sec-fetch-site": "cross-site" }), PUBLIC_ORIGIN).ok).toBe(true);
    }
  });

  it.each([
    [{ "sec-fetch-site": "same-origin", origin: PUBLIC_ORIGIN }, true],
    [{ "sec-fetch-site": "same-origin" }, true],
    [{ origin: PUBLIC_ORIGIN }, true],
    [{ "sec-fetch-site": "cross-site", origin: PUBLIC_ORIGIN }, false],
    [{ "sec-fetch-site": "same-site" }, false],
    [{ "sec-fetch-site": "none" }, false],
    [{ origin: "http://evil.test" }, false],
    [{ origin: "null" }, false],
    [{ "sec-fetch-site": "same-origin", origin: "http://evil.test" }, false],
    [{}, false],
  ])("POST with %j -> ok=%s", (headers, ok) => {
    for (const m of ["POST", "PUT", "PATCH", "DELETE"]) {
      expect(checkCsrf(req(m, headers), PUBLIC_ORIGIN).ok).toBe(ok);
    }
  });
});

describe("returnTo", () => {
  it.each([
    ["/home", "/home"],
    ["/o/acme/w/docs?x=1#y", "/o/acme/w/docs?x=1#y"],
    [null, "/home"],
    ["https://evil.test", "/home"],
    ["//evil.test/x", "/home"],
    ["/\\evil.test", "/home"],
    ["/%0d%0aSet-Cookie:x", "/%0d%0aSet-Cookie:x"],
    ["/a\nb", "/home"],
    ["/api/auth/login", "/home"],
  ])("%s -> %s", (input, out) => expect(safeReturnTo(input)).toBe(out));
});

describe("log redaction", () => {
  it("redacts sensitive field names and JWT-shaped strings in error reasons", () => {
    const out = redactFields({ access_token: "eyJ...", refresh_token: "r", code: "c", cookie: "k", reason: "ok", n: 1 });
    expect(out).toEqual({
      access_token: "[REDACTED]",
      refresh_token: "[REDACTED]",
      code: "[REDACTED]",
      cookie: "[REDACTED]",
      reason: "ok",
      n: 1,
    });
    const jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2lnbmF0dXJlLXNpZ25hdHVyZQ";
    expect(errorReason(new Error(`bad token ${jwt}`))).toBe("bad token [jwt]");
    expect(errorReason({ error: "invalid_grant" })).toBe("oauth:invalid_grant");
  });
});
