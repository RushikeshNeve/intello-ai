import type { ReactNode } from "react";
import Link from "next/link";
import { BrandLockup } from "@/components/brand/brand";
import { ThemeToggle } from "@/features/theme/theme-toggle";
import { SkipLink } from "./skip-link";
import { UserMenu } from "./topbar";

/**
 * Slim top bar for pages that have no org shell (`/home`, and org error states): brand, optional controls,
 * theme and the account menu. Org pages use `AppShell` instead.
 */
export function AppHeader({ userLabel, scope }: { userLabel: string; scope?: ReactNode }) {
  return (
    <header className="flex h-[61px] items-center gap-4 border-b border-border bg-background px-[17px] min-[721px]:h-[69px] min-[721px]:px-7 min-[1101px]:px-[42px]">
      <SkipLink />
      <Link href="/home" className="focus-ring rounded-md" aria-label="Intello home">
        <BrandLockup />
      </Link>
      {scope}
      <div className="ml-auto flex items-center gap-2">
        <ThemeToggle />
        <UserMenu userLabel={userLabel} />
      </div>
    </header>
  );
}

export function userLabelOf(user: { email?: string | null; name?: string | null }): string {
  return user.email ?? user.name ?? "Signed in";
}
