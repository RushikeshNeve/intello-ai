import type { ReactNode } from "react";
import Link from "next/link";
import { ThemeToggle } from "@/features/theme/theme-toggle";
import { SignOutButton } from "@/features/auth/components/auth-controls";

/** Top bar: brand, optional scope controls (the org switcher), the signed-in user and sign-out. */
export function AppHeader({ userLabel, scope }: { userLabel: string; scope?: ReactNode }) {
  return (
    <header className="border-b border-border bg-background">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-background focus:px-3 focus:py-2 focus:ring-3 focus:ring-ring/50"
      >
        Skip to content
      </a>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5">
        <Link
          href="/home"
          className="rounded-md text-sm font-semibold tracking-tight outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          UKOS
        </Link>
        {scope}
        <div className="ml-auto flex items-center gap-3">
          <ThemeToggle />
          <span className="hidden text-sm text-muted-foreground sm:inline" data-testid="session-user">
            {userLabel}
          </span>
          <SignOutButton />
        </div>
      </div>
    </header>
  );
}

export function userLabelOf(user: { email?: string | null; name?: string | null }): string {
  return user.email ?? user.name ?? "Signed in";
}
