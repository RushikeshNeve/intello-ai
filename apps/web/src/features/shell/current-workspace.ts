"use client";

/**
 * "Current workspace" for the flat sidebar. Order: the workspace in the URL, else the last one used in this org
 * (localStorage, every access guarded: storage can be blocked), else the first workspace. Never trusts storage:
 * a stored slug that is not in the org's current list is ignored. Hidden entirely while an org switch is in flight.
 */
import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import type { Workspace } from "@/features/workspaces/api";

export const lastWorkspaceKey = (orgId: string) => `intelo-last-workspace:${orgId}`;

function store(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function readLastWorkspace(storage: Pick<Storage, "getItem"> | null, orgId: string): string | null {
  try {
    return storage?.getItem(lastWorkspaceKey(orgId)) ?? null;
  } catch {
    return null;
  }
}

export function writeLastWorkspace(storage: Pick<Storage, "setItem"> | null, orgId: string, slug: string): void {
  try {
    storage?.setItem(lastWorkspaceKey(orgId), slug);
  } catch {
    // remembered for this page view only
  }
}

/** Pure resolution (exported for tests). */
export function pickWorkspace(workspaces: readonly Workspace[], urlSlug: string | null, storedSlug: string | null): Workspace | null {
  return (
    workspaces.find((w) => w.slug === urlSlug) ??
    workspaces.find((w) => w.slug === storedSlug) ??
    workspaces[0] ??
    null
  );
}

export function useCurrentWorkspace(orgId: string, workspaces: readonly Workspace[]) {
  const params = useParams<{ workspaceSlug?: string }>();
  const urlSlug = params?.workspaceSlug ? decodeURIComponent(params.workspaceSlug) : null;
  const [stored, setStored] = useState<{ orgId: string; slug: string | null } | null>(null);

  useEffect(() => {
    setStored({ orgId, slug: readLastWorkspace(store(), orgId) }); // eslint-disable-line react-hooks/set-state-in-effect -- storage is only readable after mount
  }, [orgId]);

  useEffect(() => {
    if (urlSlug && workspaces.some((w) => w.slug === urlSlug)) writeLastWorkspace(store(), orgId, urlSlug);
  }, [orgId, urlSlug, workspaces]);

  const storedSlug = stored && stored.orgId === orgId ? stored.slug : null;
  const current = pickWorkspace(workspaces, urlSlug, storedSlug);

  const choose = useCallback(
    (slug: string) => {
      writeLastWorkspace(store(), orgId, slug);
      setStored({ orgId, slug });
    },
    [orgId],
  );
  return { current, choose, onWorkspaceRoute: urlSlug !== null };
}
