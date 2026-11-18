"use client";

import { useState } from "react";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import { FolderIcon } from "lucide-react";
import { queryKeys } from "@/lib/query/keys";
import { useApiFetcher } from "@/lib/query/provider";
import { canManageWorkspaces } from "@/features/organizations/api/orgs";
import { useOrgScope } from "@/features/organizations/components/org-scope";
import { SlugNameForm } from "@/features/shell/slug-name-form";
import { createWorkspace, type Workspace } from "../api";
import { useWorkspaces } from "../hooks";

/** `/o/{orgSlug}`: workspace cards + create form (owners/admins only; the API still enforces 403). */
export function WorkspaceOverview({ initialWorkspaces }: { initialWorkspaces: Workspace[] }) {
  const { org } = useOrgScope();
  const fetcher = useApiFetcher();
  const queryClient = useQueryClient();
  const { data: workspaces = [] } = useWorkspaces(org.id, initialWorkspaces);
  const [created, setCreated] = useState<string | null>(null);
  const base = `/o/${encodeURIComponent(org.slug)}`;

  return (
    <div className="grid gap-10">
      <section aria-labelledby="ws-heading" className="grid gap-3">
        <h2 id="ws-heading" className="text-lg font-semibold">
          Workspaces
        </h2>
        {workspaces.length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid="workspaces-empty">
            {canManageWorkspaces(org.role)
              ? "This organization has no workspaces yet. Create the first one below."
              : "You don't have access to any workspaces in this organization yet."}
          </p>
        ) : (
          <ul className="grid gap-2 sm:grid-cols-2" data-testid="workspace-list">
            {workspaces.map((w) => (
              <li key={w.id}>
                <Link
                  href={`${base}/w/${encodeURIComponent(w.slug)}`}
                  className="flex items-center gap-3 rounded-lg border border-border bg-card px-4 py-3 text-card-foreground outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 motion-safe:transition-colors"
                >
                  <FolderIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{w.name}</span>
                    <span className="block text-xs text-muted-foreground">{w.slug}</span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
        <p role="status" className="text-sm text-muted-foreground">
          {created ? `Workspace “${created}” created.` : ""}
        </p>
      </section>
      {canManageWorkspaces(org.role) && (
        <section aria-labelledby="create-ws-heading" className="grid gap-3">
          <h2 id="create-ws-heading" className="text-lg font-semibold">
            Create a workspace
          </h2>
          <SlugNameForm
            noun="workspace"
            submitLabel="Create workspace"
            onCreate={async (input) => {
              const ws = await createWorkspace(fetcher, org.id, input);
              queryClient.setQueryData<Workspace[]>(queryKeys.workspaces(org.id), (prev = []) => [...prev, ws]);
              setCreated(ws.name);
            }}
          />
        </section>
      )}
    </div>
  );
}
