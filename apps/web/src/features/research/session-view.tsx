"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import { StatusBadge } from "@/components/ui/badges";
import { SurfaceCard } from "@/components/ui/surface";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { Skeleton } from "@/components/ui/skeleton";
import { CheckIcon, CircleDashedIcon, LoaderIcon, XIcon } from "lucide-react";
import { useApiFetcher } from "@/lib/query/provider";
import type { Workspace } from "@/features/workspaces/api";
import { ApiError } from "@/features/organizations/api/http";
import { cancelResearch, errorCopy, exportUrl, getResearch, isTerminal, pollInterval, type ResearchSession, type Step } from "./api";
import { STATE_LABEL, STATE_TONE } from "./research-brief-card";
import { ReportViewer } from "./report-viewer";

export const researchKey = (orgId: string, id: string) => ["org", orgId, "research", "one", id] as const;

/**
 * One research session. State lives on the server, so a reload (or another tab) resumes the same view: the page
 * polls `GET research/{id}` every 2 s until the session is terminal.
 */
export function ResearchSessionView({ orgId, orgSlug, sessionId, workspaces }: { orgId: string; orgSlug: string; sessionId: string; workspaces: readonly Workspace[] }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const [cancelError, setCancelError] = useState(false);
  const q = useQuery({
    queryKey: researchKey(orgId, sessionId),
    retry: false,
    staleTime: 0,
    queryFn: ({ signal }) => getResearch(fetcher, orgId, sessionId, signal),
    refetchInterval: (query) => pollInterval(query.state.data, query.state.status === "error" && query.state.data === undefined),
  });
  const cancel = useMutation({
    mutationFn: () => cancelResearch(fetcher, orgId, sessionId),
    onSuccess: () => {
      setCancelError(false);
      void qc.invalidateQueries({ queryKey: researchKey(orgId, sessionId) });
    },
    onError: () => setCancelError(true),
  });
  const back = (
    <Link href={`/o/${encodeURIComponent(orgSlug)}/research`} className="focus-ring w-fit text-sm text-text-secondary underline underline-offset-2">
      All research
    </Link>
  );

  if (q.isPending) {
    return (
      <div className="grid gap-3" data-testid="research-loading">
        {back}
        <div role="status" className="grid gap-3">
          <span className="sr-only">Loading…</span>
          <Skeleton className="h-8 w-2/3" />
          <Skeleton className="h-24 w-full" />
        </div>
      </div>
    );
  }
  if (q.isError || !q.data) {
    const gone = q.error instanceof ApiError && (q.error.status === 404 || q.error.status === 403);
    return (
      <div className="grid gap-3" data-testid="research-error">
        {back}
        {gone ? (
          <EmptyState title="This research session isn't available." description="It may have been removed, or you may not have access." />
        ) : (
          <ErrorState title="We couldn't load this research session." onRetry={() => void q.refetch()} />
        )}
      </div>
    );
  }
  const s = q.data;
  return (
    <div className="grid gap-5" data-testid="research-session" data-state={s.state}>
      {back}
      <PageHeader
        eyebrow="Research brief"
        title={<span className="break-words">{s.question}</span>}
        actions={
          <>
            {!isTerminal(s.state) && (
              <Button variant="outline" size="sm" disabled={cancel.isPending} onClick={() => cancel.mutate()} data-testid="research-cancel">
                Cancel research
              </Button>
            )}
            {s.report && (
              <a href={exportUrl(orgId, sessionId)} className="focus-ring inline-flex h-8 items-center rounded-control border border-border bg-card px-3 text-sm font-medium hover:bg-muted" data-testid="research-export">
                Export as Markdown
              </a>
            )}
          </>
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge tone={STATE_TONE[s.state]}>{STATE_LABEL[s.state]}</StatusBadge>
        <p role="status" className="text-sm text-text-secondary" data-testid="research-state">
          {s.state === "planned" || s.state === "running" ? "Researching… this page updates by itself." : s.state === "completed" ? "Done." : s.state === "cancelled" ? "Cancelled." : errorCopy(s.error_code)}
        </p>
      </div>
      {cancelError && (
        <p role="alert" className="text-sm text-danger" data-testid="research-cancel-error">
          We couldn&apos;t cancel that. It may already be finished.
        </p>
      )}
      {s.state === "failed" && s.message && (
        <p role="alert" className="rounded-control bg-danger-soft px-3 py-2 text-sm text-danger" data-testid="research-failed">
          {s.message}
        </p>
      )}
      <Steps session={s} />
      {s.report && <ReportViewer report={s.report} orgSlug={orgSlug} workspaces={workspaces} />}
    </div>
  );
}

const STEP_ICON: Record<Step["state"], typeof CheckIcon> = { done: CheckIcon, running: LoaderIcon, pending: CircleDashedIcon, failed: XIcon };

function Steps({ session: s }: { session: ResearchSession }) {
  if (s.steps.length === 0) return null;
  const done = s.steps.filter((st) => st.state === "done").length;
  return (
    <SurfaceCard as="section" aria-label="Steps" className="grid gap-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-card-title font-semibold text-brand-indigo dark:text-foreground">Steps</h2>
        <span className="text-meta text-text-secondary" data-testid="research-progress-text">
          {done} of {s.steps.length} done
        </span>
      </div>
      <div role="progressbar" aria-label="Research progress" aria-valuemin={0} aria-valuemax={s.steps.length} aria-valuenow={done} className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-brand-blue transition-[width] duration-[var(--duration-base)]" style={{ width: `${(done / s.steps.length) * 100}%` }} />
      </div>
      <ol className="grid gap-1.5 text-sm" data-testid="research-steps">
        {s.steps.map((st) => {
          const Icon = STEP_ICON[st.state];
          return (
            <li key={st.seq} className="flex items-start gap-2" data-state={st.state}>
              <Icon aria-hidden="true" className={`mt-0.5 size-4 shrink-0 ${st.state === "done" ? "text-success" : st.state === "failed" ? "text-danger" : st.state === "running" ? "text-brand-blue" : "text-text-secondary"}`} />
              <span className="min-w-0 break-words">
                {st.kind}
                {st.query ? `: ${st.query}` : ""}
              </span>
              <span className="ml-auto shrink-0 text-meta text-text-secondary">{st.state}</span>
            </li>
          );
        })}
      </ol>
    </SurfaceCard>
  );
}
