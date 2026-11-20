"use client";

import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useApiFetcher } from "@/lib/query/provider";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { Skeleton } from "@/components/ui/skeleton";
import type { Workspace } from "@/features/workspaces/api";
import { listMemories, type MemoryState } from "./api";
import { ApprovedMemoryCard, MemorySuggestionCard, memoryKeys } from "./memory-cards";

export const TABS: { id: string; label: string; state: MemoryState }[] = [
  { id: "suggestions", label: "Suggestions", state: "suggested" },
  { id: "approved", label: "Approved", state: "approved" },
  { id: "rejected", label: "Rejected", state: "rejected" },
];

export { memoryKeys };

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
    <div className="grid gap-5" data-testid="memory-app">
      <PageHeader eyebrow="What Intello remembers" title="Memory" description="Memories are only used in answers after you approve them, and only while their sources still exist." />
      <div role="tablist" aria-label="Memory state" className="flex flex-wrap gap-1 rounded-control bg-muted p-1 w-fit">
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
          <div role="status" className="grid gap-3">
            <span className="sr-only">Loading…</span>
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : q.isError ? (
          <ErrorState title="We couldn't load memories." onRetry={() => void q.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            title="Nothing here yet."
            description={tab.state === "suggested" ? 'Use "Remember this" on a cited chat answer to propose a memory.' : undefined}
            testId="memory-empty"
          />
        ) : (
          items.map((m) =>
            tab.state === "suggested" ? (
              <MemorySuggestionCard key={m.id} memory={m} orgId={orgId} orgSlug={orgSlug} workspaces={workspaces} />
            ) : (
              <ApprovedMemoryCard key={m.id} memory={m} orgId={orgId} orgSlug={orgSlug} workspaces={workspaces} />
            ),
          )
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
