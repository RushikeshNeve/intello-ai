"use client";

import { useState } from "react";
import Link from "next/link";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useApiFetcher } from "@/lib/query/provider";
import { evidenceHref, workspaceRefFor } from "@/features/evidence/links";
import type { Workspace } from "@/features/workspaces/api";
import { approveMemory, deleteMemory, errorText, getMemory, listMemories, rejectMemory, type Memory, type MemoryState } from "./api";

export const TABS: { id: string; label: string; state: MemoryState }[] = [
  { id: "suggestions", label: "Suggestions", state: "suggested" },
  { id: "approved", label: "Approved", state: "approved" },
  { id: "rejected", label: "Rejected", state: "rejected" },
];

export const memoryKeys = {
  list: (orgId: string, state: MemoryState) => ["org", orgId, "memory", "list", state] as const,
  one: (orgId: string, id: string) => ["org", orgId, "memory", "one", id] as const,
};

export function MemoryApp({ orgId, orgSlug, workspaces }: { orgId: string; orgSlug: string; workspaces: readonly Workspace[] }) {
  const [tab, setTab] = useState(TABS[0]);
  const fetcher = useApiFetcher();
  const q = useInfiniteQuery({
    queryKey: memoryKeys.list(orgId, tab.state),
    initialPageParam: null as string | null,
    retry: false,
    queryFn: ({ pageParam, signal }) => listMemories(fetcher, orgId, { state: tab.state, after: pageParam }, signal),
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="mx-auto grid max-w-4xl gap-4" data-testid="memory-app">
      <h1 tabIndex={-1} className="text-xl font-semibold tracking-tight outline-none">
        Memory
      </h1>
      <p className="text-sm text-muted-foreground">Memories are only used in answers after you approve them, and only while their sources still exist.</p>
      <div role="tablist" aria-label="Memory state" className="flex flex-wrap gap-1">
        {TABS.map((t) => (
          <Button
            key={t.id}
            role="tab"
            aria-selected={t.id === tab.id}
            variant={t.id === tab.id ? "secondary" : "ghost"}
            size="sm"
            onClick={() => setTab(t)}
            data-testid={`tab-${t.id}`}
          >
            {t.label}
          </Button>
        ))}
      </div>
      <div role="tabpanel" className="grid gap-3" data-testid="memory-list">
        {q.isPending ? (
          <p role="status" className="text-sm text-muted-foreground">
            Loading…
          </p>
        ) : q.isError ? (
          <div role="alert" className="grid justify-items-start gap-2 text-sm">
            <p>We couldn&apos;t load memories.</p>
            <Button variant="outline" size="sm" onClick={() => void q.refetch()}>
              Try again
            </Button>
          </div>
        ) : items.length === 0 ? (
          <p className="rounded-lg border border-dashed border-border p-4 text-sm text-muted-foreground" data-testid="memory-empty">
            Nothing here yet.
          </p>
        ) : (
          items.map((m) => <MemoryCard key={m.id} memory={m} orgId={orgId} orgSlug={orgSlug} workspaces={workspaces} state={tab.state} />)
        )}
        {q.hasNextPage && (
          <Button variant="outline" size="sm" className="w-fit" disabled={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
            Load more
          </Button>
        )}
      </div>
    </div>
  );
}

function MemoryCard({ memory: m, orgId, orgSlug, workspaces, state }: { memory: Memory; orgId: string; orgSlug: string; workspaces: readonly Workspace[]; state: MemoryState }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const [showEvidence, setShowEvidence] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const detail = useQuery({ queryKey: memoryKeys.one(orgId, m.id), enabled: showEvidence, retry: false, queryFn: ({ signal }) => getMemory(fetcher, orgId, m.id, signal) });
  const evidence = detail.data?.evidence ?? m.evidence;
  const anyStale = evidence.some((e) => e.evidence_status === "stale");

  const act = useMutation({
    mutationFn: async (a: "approve" | "reject" | "delete"): Promise<unknown> =>
      a === "approve" ? approveMemory(fetcher, orgId, m.id) : a === "reject" ? rejectMemory(fetcher, orgId, m.id) : deleteMemory(fetcher, orgId, m.id),
    onSuccess: () => {
      setError(null);
      void qc.invalidateQueries({ queryKey: ["org", orgId, "memory"] });
    },
    onError: (e) => setError(errorText(e)),
  });

  return (
    <article className="grid gap-2 rounded-lg border border-border p-3" data-testid="memory-item" data-state={m.state}>
      <p className="text-sm break-words whitespace-pre-wrap">{m.statement}</p>
      <p className="text-xs text-muted-foreground">
        {m.kind} · {m.visibility}
        {m.state === "invalidated" && m.invalidated_reason ? ` · invalidated (${m.invalidated_reason.replace(/_/g, " ")})` : ""}
        {m.expires_at ? ` · expires ${m.expires_at.slice(0, 10)}` : ""}
      </p>
      {anyStale && (
        <p role="note" className="w-fit rounded-md border border-border bg-muted px-2 py-0.5 text-xs" data-testid="stale-badge">
          Stale evidence: a source changed or was removed; this memory is not used.
        </p>
      )}
      {m.evidence.length > 0 && (
        <div className="grid gap-1">
          <Button variant="ghost" size="xs" className="w-fit" aria-expanded={showEvidence} onClick={() => setShowEvidence((v) => !v)} data-testid="toggle-evidence">
            Evidence ({m.evidence.length})
          </Button>
          {showEvidence && (
            <ul className="grid gap-1 text-xs" data-testid="evidence-list">
              {evidence.map((e) => (
                <li key={e.chunk_id} className="flex flex-wrap items-center gap-2">
                  <Link
                    className="underline underline-offset-2"
                    data-testid="evidence-link"
                    href={evidenceHref({
                      orgSlug,
                      workspaceRef: workspaceRefFor(workspaces, e.workspace_id ?? m.workspace_id ?? ""),
                      documentId: e.document_id,
                      versionId: e.version_id,
                      chunkId: e.chunk_id,
                    })}
                  >
                    Open source passage
                  </Link>
                  {e.evidence_status === "stale" && <span data-testid="evidence-stale">stale</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {state === "suggested" && (
          <>
            <Button size="sm" disabled={act.isPending} onClick={() => act.mutate("approve")} data-testid="approve">
              Approve
            </Button>
            <Button size="sm" variant="outline" disabled={act.isPending} onClick={() => act.mutate("reject")} data-testid="reject">
              Reject
            </Button>
          </>
        )}
        <Button size="sm" variant="ghost" disabled={act.isPending} onClick={() => act.mutate("delete")} data-testid="delete">
          Delete
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-destructive" data-testid="memory-error">
          {error}
        </p>
      )}
    </article>
  );
}
