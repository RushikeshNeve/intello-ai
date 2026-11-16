import { NextResponse, type NextRequest } from "next/server";
import { clearSessionCookie, sessionCookieName } from "@/features/auth/cookies";
import { defaultAuthDeps, loadSession } from "@/features/auth/session";

/**
 * Gate for protected pages. Unauthenticated (or revoked) sessions get a real 307 to the login route
 * before anything renders. The check is local: unseal the cookie and look the id up in the
 * in-process session store (no network). Pages still re-check through the session DAL.
 */
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const deps = defaultAuthDeps();
  const cookieValue = request.cookies.get(sessionCookieName(deps.config))?.value;
  const record = await loadSession(deps, cookieValue);
  if (record) return NextResponse.next();

  // A router prefetch must not start a login (it would overwrite an in-flight login transaction).
  if (request.headers.has("next-router-prefetch")) {
    return new NextResponse(null, { status: 204, headers: { "cache-control": "no-store" } });
  }

  const returnTo = request.nextUrl.pathname + request.nextUrl.search;
  const login = new URL("/api/auth/login", deps.config.publicOrigin);
  login.searchParams.set("returnTo", returnTo);
  const res = NextResponse.redirect(login, 307);
  res.headers.set("cache-control", "no-store");
  if (cookieValue) res.headers.append("set-cookie", clearSessionCookie(deps.config));
  return res;
}

export const config = {
  matcher: ["/home/:path*", "/o/:path*"],
};
