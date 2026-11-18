"use client";

import { useEffect } from "react";
import { buttonVariants } from "@/components/ui/button";
import { loginUrl } from "@/features/organizations/api/http";

/**
 * Rendered when the API rejected the session mid-render (the proxy already handles "no session" with a
 * real 307). Layouts don't know the full URL on the server, so the browser builds `returnTo` from the
 * exact deep link it is on; the link is the no-JS fallback.
 */
export function SessionRedirect({ fallbackPath }: { fallbackPath: string }) {
  useEffect(() => {
    window.location.replace(loginUrl(window.location.pathname + window.location.search));
  }, []);
  return (
    <main id="main" className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center" data-testid="session-ended">
      <h1 className="text-xl font-semibold tracking-tight">Your session has ended</h1>
      <p className="text-sm text-muted-foreground">Redirecting you to sign in…</p>
      <a href={loginUrl(fallbackPath)} className={buttonVariants()}>
        Sign in again
      </a>
    </main>
  );
}
