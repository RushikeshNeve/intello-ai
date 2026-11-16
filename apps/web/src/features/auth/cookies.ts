import type { AuthConfig } from "./config";

/**
 * Cookie names and attributes. Both cookies are HttpOnly + SameSite=Lax; Secure (and the `__Host-`
 * prefix, which forces Secure, Path=/ and no Domain) whenever the public origin is https.
 *
 * - Session cookie: Path=/, lives for the session's absolute TTL, holds a sealed session reference.
 * - Login-transaction cookie: Path=/api/auth, 10 minutes, holds the sealed state/nonce/PKCE verifier.
 */
export const TX_COOKIE_PATH = "/api/auth";
export const TX_TTL_SECONDS = 10 * 60;

export function sessionCookieName(config: Pick<AuthConfig, "secureCookies">): string {
  return config.secureCookies ? "__Host-ukos_session" : "ukos_session";
}

export function txCookieName(config: Pick<AuthConfig, "secureCookies">): string {
  // `__Host-` requires Path=/, so the narrower-path tx cookie uses `__Secure-` instead.
  return config.secureCookies ? "__Secure-ukos_auth_tx" : "ukos_auth_tx";
}

export type CookieAttributes = {
  path: string;
  maxAge: number;
  httpOnly: true;
  sameSite: "Lax";
  secure: boolean;
};

export function serializeCookie(name: string, value: string, attrs: CookieAttributes): string {
  if (!/^[\w!#$%&'*+\-.^`|~]+$/.test(name)) throw new Error("invalid cookie name");
  if (!/^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]*$/.test(value)) throw new Error("invalid cookie value");
  const parts = [`${name}=${value}`, `Path=${attrs.path}`, `Max-Age=${Math.max(0, Math.floor(attrs.maxAge))}`];
  if (attrs.maxAge <= 0) parts.push("Expires=Thu, 01 Jan 1970 00:00:00 GMT");
  parts.push("HttpOnly", `SameSite=${attrs.sameSite}`);
  if (attrs.secure) parts.push("Secure");
  return parts.join("; ");
}

export function sessionCookie(config: AuthConfig, value: string): string {
  return serializeCookie(sessionCookieName(config), value, {
    path: "/",
    maxAge: config.sessionTtlSeconds,
    httpOnly: true,
    sameSite: "Lax",
    secure: config.secureCookies,
  });
}

export function clearSessionCookie(config: AuthConfig): string {
  return serializeCookie(sessionCookieName(config), "", {
    path: "/",
    maxAge: 0,
    httpOnly: true,
    sameSite: "Lax",
    secure: config.secureCookies,
  });
}

export function txCookie(config: AuthConfig, value: string): string {
  return serializeCookie(txCookieName(config), value, {
    path: TX_COOKIE_PATH,
    maxAge: TX_TTL_SECONDS,
    httpOnly: true,
    sameSite: "Lax",
    secure: config.secureCookies,
  });
}

export function clearTxCookie(config: AuthConfig): string {
  return serializeCookie(txCookieName(config), "", {
    path: TX_COOKIE_PATH,
    maxAge: 0,
    httpOnly: true,
    sameSite: "Lax",
    secure: config.secureCookies,
  });
}

/** Minimal RFC 6265 Cookie-header parser (first occurrence wins). */
export function parseCookieHeader(header: string | null | undefined): Map<string, string> {
  const out = new Map<string, string>();
  if (!header) return out;
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq <= 0) continue;
    const name = part.slice(0, eq).trim();
    let value = part.slice(eq + 1).trim();
    if (value.startsWith('"') && value.endsWith('"') && value.length >= 2) value = value.slice(1, -1);
    if (name && !out.has(name)) out.set(name, value);
  }
  return out;
}

export function readCookie(request: Request, name: string): string | undefined {
  return parseCookieHeader(request.headers.get("cookie")).get(name);
}
