"use client";

import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useState, type ReactNode } from "react";
import { flushSync } from "react-dom";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { dropOtherOrgQueries } from "@/lib/query/provider";
import type { OrgSummary } from "../api/orgs";

type OrgScope = {
  /** The org the current URL resolved to (server-authorized). */
  org: OrgSummary;
  /** The caller's memberships, for the switcher. */
  orgs: readonly OrgSummary[];
  /** What the header shows: the switch target while a switch is in flight, otherwise `org`. */
  displayedOrg: OrgSummary;
  switching: boolean;
  switchOrg: (target: OrgSummary) => void;
};

const OrgScopeContext = createContext<OrgScope | null>(null);

export function useOrgScope(): OrgScope {
  const ctx = useContext(OrgScopeContext);
  if (!ctx) throw new Error("useOrgScope must be used inside <OrgScopeProvider>");
  return ctx;
}

/**
 * Owns the org scope of everything below it (ADR-044/047).
 *
 * Switching org: (1) the header shows the target org at once and the scoped content is unmounted
 * (flushSync, so no previous-org data stays on screen), (2) the whole TanStack Query cache is cleared,
 * (3) then the router navigates to `/o/{slug}`. Any other way of arriving at a different org (back
 * button, typed URL, link) is covered by the guard effect, which drops every other org's queries.
 */
export function OrgScopeProvider({
  org,
  orgs,
  children,
  navigate,
}: {
  org: OrgSummary;
  orgs: readonly OrgSummary[];
  children: ReactNode;
  /** Test seam; defaults to `router.push`. */
  navigate?: (href: string) => void;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<{ target: OrgSummary; from: string } | null>(null);

  // Stale switch state (navigation landed somewhere else) never hides content: it only counts while the
  // URL still resolves to the org we switched away from.
  const switching = pending !== null && pending.from === org.id && pending.target.id !== org.id;

  useLayoutEffect(() => {
    dropOtherOrgQueries(queryClient, org.id);
  }, [queryClient, org.id]);

  const switchOrg = useCallback(
    (target: OrgSummary) => {
      if (target.id === org.id) return;
      flushSync(() => setPending({ target, from: org.id }));
      queryClient.clear();
      (navigate ?? router.push)(`/o/${encodeURIComponent(target.slug)}`);
    },
    [org.id, queryClient, navigate, router],
  );

  const value = useMemo<OrgScope>(
    () => ({ org, orgs, displayedOrg: switching ? pending!.target : org, switching, switchOrg }),
    [org, orgs, pending, switching, switchOrg],
  );
  return <OrgScopeContext.Provider value={value}>{children}</OrgScopeContext.Provider>;
}

/** Renders org-scoped content only while it matches the displayed scope. */
export function ScopedContent({ children }: { children: ReactNode }) {
  const { switching, displayedOrg } = useOrgScope();
  if (switching) {
    return (
      <div role="status" aria-live="polite" className="p-6 text-sm text-muted-foreground">
        Switching to {displayedOrg.name}…
      </div>
    );
  }
  return <>{children}</>;
}
