"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useApiFetcher } from "@/lib/query/provider";
import { citationHref, citationLabel } from "@/features/chat/citations";
import type { Workspace } from "@/features/workspaces/api";
import { ApiError } from "@/features/organizations/api/http";
import { cancelResearch, errorCopy, exportUrl, getResearch, isTerminal, pollInterval, type ResearchSession } from "./api";

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
    <Link href={`/o/${encodeURIComponent(orgSlug)}/research`} className="w-fit text-sm text-muted-foreground underline underline-offset-2">
      All research
    </Link>
  );

  if (q.isPending) {
    return (
      <div className="mx-auto grid max-w-4xl gap-3" data-testid="research-loading">
        {back}
        <p role="status" className="text-sm text-muted-foreground">
          Loading…
        </p>
      </div>
    );
  }
  if (q.isError || !q.data) {
    const gone = q.error instanceof ApiError && (q.error.status === 404 || q.error.status === 403);
    return (
      <div className="mx-auto grid max-w-4xl gap-3" data-testid="research-error">
        {back}
        <p role="alert" className="text-sm">
          {gone ? "This research session isn't available." : "We couldn't load this research session."}
        </p>
        {!gone && (
          <Button variant="outline" size="sm" className="w-fit" onClick={() => void q.refetch()}>
            Try again
          </Button>
        )}
      </div>
    );
  }
  const s = q.data;
  return (
    <div className="mx-auto grid max-w-4xl gap-4" data-testid="research-session" data-state={s.state}>
      {back}
      <h1 tabIndex={-1} className="text-xl font-semibold tracking-tight break-words outline-none">
        {s.question}
      </h1>
      <p role="status" className="text-sm text-muted-foreground" data-testid="research-state">
        {s.state === "planned" || s.state === "running" ? "Researching… this page updates by itself." : s.state === "completed" ? "Done." : s.state === "cancelled" ? "Cancelled." : errorCopy(s.error_code)}
      </p>
      <div className="flex flex-wrap gap-2">
        {!isTerminal(s.state) && (
          <Button variant="outline" size="sm" disabled={cancel.isPending} onClick={() => cancel.mutate()} data-testid="research-cancel">
            Cancel research
          </Button>
        )}
        {s.report && (
          <a href={exportUrl(orgId, sessionId)} className="inline-flex h-7 items-center rounded-lg border border-border px-2.5 text-[0.8rem] font-medium" data-testid="research-export">
            Export as Markdown
          </a>
        )}
      </div>
      {cancelError && (
        <p role="alert" className="text-sm text-destructive" data-testid="research-cancel-error">
          We couldn&apos;t cancel that. It may already be finished.
        </p>
      )}
      {s.state === "failed" && s.message && (
        <p role="alert" className="text-sm" data-testid="research-failed">
          {s.message}
        </p>
      )}
      <Steps session={s} />
      {s.report && <ReportView report={s.report} orgSlug={orgSlug} workspaces={workspaces} />}
    </div>
  );
}

function Steps({ session: s }: { session: ResearchSession }) {
  if (s.steps.length === 0) return null;
  return (
    <section aria-label="Steps" className="grid gap-1">
      <h2 className="text-sm font-medium">Steps</h2>
      <ol className="grid gap-1 text-sm" data-testid="research-steps">
        {s.steps.map((st) => (
          <li key={st.seq} className="flex flex-wrap gap-2" data-state={st.state}>
            <span className="text-xs text-muted-foreground">{st.state}</span>
            <span className="min-w-0 break-words">
              {st.kind}
              {st.query ? `: ${st.query}` : ""}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function ReportView({ report, orgSlug, workspaces }: { report: NonNullable<ResearchSession["report"]>; orgSlug: string; workspaces: readonly Workspace[] }) {
  return (
    <section aria-label="Report" className="grid gap-4" data-testid="research-report">
      <h2 className="text-lg font-semibold tracking-tight">Report</h2>
      {report.withheld > 0 && (
        <p role="note" className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm" data-testid="report-withheld">
          {report.withheld} statement{report.withheld === 1 ? " was" : "s were"} withheld because the sources didn&apos;t support {report.withheld === 1 ? "it" : "them"}.
        </p>
      )}
      {report.sections.map((sec, i) => (
        <div key={i} className="grid gap-2" data-testid="report-section">
          <h3 className="font-medium break-words">{sec.heading}</h3>
          {sec.statements.map((st, j) => (
            <p key={j} className="text-sm leading-relaxed break-words" data-testid="report-statement">
              {st.text}{" "}
              {st.citations.map((c) => (
                <Link
                  key={c.marker + c.chunk_id}
                  href={citationHref(c, orgSlug, workspaces)}
                  aria-label={citationLabel(c)}
                  title={citationLabel(c)}
                  className="mx-0.5 inline-flex min-w-5 items-center justify-center rounded-md border border-border bg-muted px-1 text-xs font-medium no-underline hover:bg-accent"
                  data-testid="citation-chip"
                  data-chunk-id={c.chunk_id}
                >
                  {c.marker}
                </Link>
              ))}
            </p>
          ))}
        </div>
      ))}
    </section>
  );
}
