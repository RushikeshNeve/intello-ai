"use client";

import { useState } from "react";
import { PlusIcon } from "lucide-react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { Skeleton } from "@/components/ui/skeleton";
import { useApiFetcher } from "@/lib/query/provider";
import type { Workspace } from "@/features/workspaces/api";
import { listResearch } from "./api";
import { NewBriefDialog } from "./new-brief-dialog";
import { ResearchBriefCard } from "./research-brief-card";

export { loweredLimits, MAX_QUESTION } from "./new-brief-dialog";

export function ResearchHome({ orgId, orgSlug, workspaces }: { orgId: string; orgSlug: string; workspaces: readonly Workspace[] }) {
  const fetcher = useApiFetcher();
  const [dialog, setDialog] = useState(false);
  const list = useInfiniteQuery({
    queryKey: ["org", orgId, "research", "list"],
    initialPageParam: null as string | null,
    retry: false,
    queryFn: ({ pageParam, signal }) => listResearch(fetcher, orgId, { after: pageParam }, signal),
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];
  const newBrief = (
    <Button type="button" onClick={() => setDialog(true)} data-testid="research-new">
      <PlusIcon aria-hidden="true" /> New brief
    </Button>
  );

  return (
    <div className="grid gap-6" data-testid="research-home">
      <PageHeader eyebrow="Deep research" title="Research" description="Saved investigations. Each brief searches your knowledge and writes a report with sources." actions={newBrief} />
      <section aria-label="Research sessions" className="grid gap-3">
        {list.isPending ? (
          <div role="status" className="grid gap-3 sm:grid-cols-2">
            <span className="sr-only">Loading…</span>
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-28 w-full" />
          </div>
        ) : list.isError ? (
          <ErrorState title="We couldn't load your research sessions." onRetry={() => void list.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState title="No research yet." description="Start a brief to get a sourced report on a question." action={newBrief} testId="research-empty" />
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2" data-testid="research-list">
            {items.map((s) => (
              <ResearchBriefCard key={s.id} item={s} href={`/o/${encodeURIComponent(orgSlug)}/research/${encodeURIComponent(s.id)}`} />
            ))}
          </ul>
        )}
        {list.hasNextPage && (
          <Button variant="outline" size="sm" className="w-fit" onClick={() => void list.fetchNextPage()}>
            Load more
          </Button>
        )}
      </section>
      <NewBriefDialog open={dialog} onClose={() => setDialog(false)} orgId={orgId} orgSlug={orgSlug} workspaces={workspaces} />
    </div>
  );
}
