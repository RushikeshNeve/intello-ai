"use client";

import { useState } from "react";
import Link from "next/link";
import { ActivityIcon } from "lucide-react";
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Drawer } from "@/components/ui/overlay";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { MetricCard, SurfaceCard } from "@/components/ui/surface";
import { useApiFetcher } from "@/lib/query/provider";
import type { OrgSummary } from "@/features/organizations/api/orgs";
import type { Workspace } from "@/features/workspaces/api";
import { getIngestion, isTerminal, type IngestionSummary, type Stage } from "@/features/documents/ingestion/api";
import { IngestionBadge } from "@/features/documents/ingestion/status-badge";
import { displayKind, failureMessage, PROGRESS_STEPS, STAGE_LABELS, stepIndex } from "@/features/documents/ingestion/messages";
import { actionError, formatWhen } from "@/features/settings/permissions";
import { activityKey, JOB_STATES, loadJobs, type JobItem, type JobState } from "./api";

const COUNT_LABEL: Record<JobState, string> = {
  queued: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
  unsupported: "Not supported",
  no_text: "No text",
  cancelled: "Cancelled",
};

const summaryOf = (j: JobItem): IngestionSummary => ({
  state: j.state,
  stage: (j.stage as Stage | null) ?? null,
  attempts_label: null,
  error_code: j.error_code,
  message: j.message,
  updated_at: j.updated_at,
  can_retry: false,
});

export function ActivityApp({ org, workspace }: { org: OrgSummary; workspace: Workspace }) {
  const fetcher = useApiFetcher();
  const [filter, setFilter] = useState<JobState | "all">("all");
  const [open, setOpen] = useState<JobItem | null>(null);
  const q = useInfiniteQuery({
    queryKey: [...activityKey(org.id, workspace.id), filter],
    retry: false,
    placeholderData: keepPreviousData,
    initialPageParam: null as string | null,
    queryFn: ({ signal, pageParam }) => loadJobs(fetcher, org.id, workspace.id, filter, pageParam, signal),
    getNextPageParam: (last) => last.next_cursor,
    refetchInterval: (query) => ((query.state.data?.pages[0]?.counts.queued ?? 0) + (query.state.data?.pages[0]?.counts.processing ?? 0) > 0 ? 5000 : false),
  });
  const counts = q.data?.pages[0]?.counts;
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  const docBase = `/o/${encodeURIComponent(org.slug)}/w/${encodeURIComponent(workspace.slug)}/documents`;

  return (
    <div className="mx-auto grid max-w-5xl gap-6" data-workspace-scope={workspace.id} data-testid="activity-app">
      <PageHeader eyebrow={`${org.name} · ${workspace.name}`} title="Activity and jobs" description="What is being processed and what needs attention." />
      {q.isPending ? (
        <div role="status" aria-label="Loading activity" className="grid gap-3">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : q.isError ? (
        <ErrorState testId="activity-error" title="We couldn't load activity" description={actionError(q.error)} onRetry={() => void q.refetch()} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7" data-testid="activity-counts">
            {JOB_STATES.map((s) => (
              <MetricCard key={s} label={COUNT_LABEL[s]} value={counts?.[s] ?? 0} />
            ))}
          </div>
          <div role="group" aria-label="Filter by state" className="flex flex-wrap gap-1">
            {(["all", ...JOB_STATES] as const).map((s) => (
              <Button key={s} size="sm" variant={filter === s ? "secondary" : "ghost"} aria-pressed={filter === s} onClick={() => setFilter(s)}>
                {s === "all" ? "All" : COUNT_LABEL[s]}
              </Button>
            ))}
          </div>
          <JobList items={items} total={filter === "all" ? items.length : JOB_STATES.reduce((n, s) => n + (counts?.[s] ?? 0), 0)} onOpen={setOpen} />
          {q.hasNextPage && (
            <Button variant="outline" className="justify-self-start" disabled={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
              {q.isFetchingNextPage ? "Loading..." : "Load more"}
            </Button>
          )}
        </>
      )}
      <Drawer open={open !== null} onClose={() => setOpen(null)} title={open?.title ?? "Job details"} description="Processing checkpoints for the latest version.">
        {open && <JobDetail orgId={org.id} workspaceId={workspace.id} job={open} href={`${docBase}/${encodeURIComponent(open.document_id)}`} />}
      </Drawer>
    </div>
  );
}

function JobList({ items, total, onOpen }: { items: JobItem[]; total: number; onOpen: (j: JobItem) => void }) {
  if (items.length === 0) {
    return (
      <EmptyState
        testId="activity-empty"
        icon={<ActivityIcon className="size-5" />}
        title={total === 0 ? "Nothing has been processed yet" : "No jobs in this state"}
        description={total === 0 ? "Upload a document or sync a source and its processing will show up here." : "Choose another state to see more."}
      />
    );
  }
  return (
    <ul className="grid gap-2" data-testid="job-list">
      {items.map((j) => (
        <SurfaceCard as="li" key={`${j.document_id}-${j.version_id ?? ""}`} className="flex flex-wrap items-center gap-3 p-3" data-testid="job-row">
          <div className="min-w-0 grow">
            <p className="truncate text-card-title font-medium">{j.title}</p>
            <p className="text-meta text-text-secondary">
              {formatWhen(j.updated_at)}
              {j.state === "failed" || j.state === "unsupported" || j.state === "no_text" ? ` · ${failureMessage(j.error_code, j.message)}` : ""}
            </p>
          </div>
          <IngestionBadge summary={summaryOf(j)} />
          <Button variant="outline" size="sm" onClick={() => onOpen(j)} aria-label={`Details for ${j.title}`}>
            Details
          </Button>
        </SurfaceCard>
      ))}
    </ul>
  );
}

export function stepState(i: number, s: IngestionSummary): "done" | "current" | "stopped" | "waiting" {
  const kind = displayKind(s);
  if (kind === "ready") return "done";
  const at = stepIndex(s.stage);
  if (kind === "processing") return i < at ? "done" : i === at ? "current" : "waiting";
  if (kind === "failed" || kind === "unsupported" || kind === "no_text") return at >= 0 ? (i < at ? "done" : i === at ? "stopped" : "waiting") : "waiting";
  return "waiting";
}

function JobDetail({ orgId, workspaceId, job, href }: { orgId: string; workspaceId: string; job: JobItem; href: string }) {
  const fetcher = useApiFetcher();
  const q = useQuery({
    queryKey: ["org", orgId, "workspace", workspaceId, "document", job.document_id, "ingestion"],
    retry: false,
    queryFn: () => getIngestion(fetcher, orgId, workspaceId, job.document_id),
    refetchInterval: (query) => (query.state.data && !isTerminal(query.state.data.state) ? 4000 : false),
  });
  const summary: IngestionSummary = q.data ?? summaryOf(job);
  return (
    <div className="grid gap-4" data-testid="job-detail">
      <div className="flex flex-wrap items-center gap-2">
        <IngestionBadge summary={summary} />
        <Link href={href} className="text-sm underline underline-offset-2">
          Open document
        </Link>
      </div>
      <ol aria-label="Pipeline checkpoints" className="grid gap-1" data-testid="checkpoint-timeline">
        {PROGRESS_STEPS.map((step, i) => {
          const st = stepState(i, summary);
          return (
            <li key={step.key} className="flex items-center gap-2 text-sm" data-step-state={st}>
              <span aria-hidden="true" className={`size-2.5 rounded-full ${st === "done" ? "bg-success" : st === "current" ? "bg-info" : st === "stopped" ? "bg-danger" : "bg-border"}`} />
              <span className="grow">{step.label}</span>
              <span className="text-meta text-text-secondary">{st === "done" ? "done" : st === "current" ? "in progress" : st === "stopped" ? "stopped" : "waiting"}</span>
            </li>
          );
        })}
      </ol>
      {q.isPending ? (
        <Skeleton role="status" aria-label="Loading events" className="h-16 w-full" />
      ) : q.isError ? (
        <ErrorState title="We couldn't load the event history" description={actionError(q.error)} onRetry={() => void q.refetch()} />
      ) : q.data.events.length === 0 ? (
        <p className="text-sm text-text-secondary">No events recorded yet.</p>
      ) : (
        <ol aria-label="Events" className="grid gap-1 text-sm" data-testid="event-list">
          {q.data.events.map((e, idx) => (
            <li key={e.seq ?? idx} className="flex justify-between gap-3">
              <span>
                {e.label || "Update"}
                {e.stage ? ` (${STAGE_LABELS[e.stage]})` : ""}
              </span>
              <span className="text-meta text-text-secondary">{formatWhen(e.at)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
