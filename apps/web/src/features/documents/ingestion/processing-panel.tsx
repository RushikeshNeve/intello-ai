"use client";

import { useState } from "react";
import { RotateCcwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useApiFetcher } from "@/lib/query/provider";
import { retryIngestion, toRetryOutcome, type IngestionEvent, type IngestionSummary } from "./api";
import {
  PROGRESS_STEPS,
  STAGE_LABELS,
  RETRY_RESULT,
  announcement,
  displayKind,
  failureMessage,
  stepIndex,
} from "./messages";
import { IngestionBadge } from "./status-badge";
import { useIngestionStatus, type EventSourceFactory } from "./use-ingestion-status";

export type ProcessingPanelProps = {
  orgId: string;
  workspaceId: string;
  documentId: string;
  /** Latest version id (status is tracked for the latest version). */
  versionRef: string | null;
  initial: IngestionSummary | null;
  /** `document.permissions.write`: only writers see the Retry button. */
  canWrite: boolean;
  /** Test seam. */
  eventSource?: EventSourceFactory | null;
  pollMs?: number;
};

export function formatDateTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 19)} UTC`;
}

function stepStatus(i: number, summary: IngestionSummary): "done" | "current" | "failed" | "todo" {
  const kind = displayKind(summary);
  if (kind === "ready") return "done";
  const at = stepIndex(summary.stage);
  if (kind === "processing") return i < at ? "done" : i === at ? "current" : "todo";
  if (kind === "failed" || kind === "unsupported" || kind === "no_text") return at >= 0 ? (i < at ? "done" : i === at ? "failed" : "todo") : "todo";
  return "todo";
}

const STEP_TEXT = { done: "done", current: "in progress", failed: "stopped", todo: "waiting" } as const;

function eventLabel(e: IngestionEvent): string {
  const stage = e.stage ? ` (${STAGE_LABELS[e.stage]})` : "";
  return `${e.label || "Update"}${stage}`;
}

export function ProcessingPanel(props: ProcessingPanelProps) {
  const { orgId, workspaceId, documentId, versionRef, canWrite } = props;
  const fetcher = useApiFetcher();
  const live = useIngestionStatus({
    orgId,
    workspaceId,
    documentId,
    versionRef,
    initial: props.initial,
    eventSource: props.eventSource,
    pollMs: props.pollMs,
  });
  const summary = live.summary;
  const [retrying, setRetrying] = useState(false);
  const [retryMessage, setRetryMessage] = useState<{ forKey: string; text: string } | null>(null);
  const messageKey = `${documentId}/${versionRef ?? ""}`;
  const shownMessage = retryMessage && retryMessage.forKey === messageKey ? retryMessage.text : "";

  async function onRetry() {
    setRetrying(true);
    setRetryMessage(null);
    let outcome: keyof typeof RETRY_RESULT = "started";
    try {
      await retryIngestion(fetcher, orgId, workspaceId, documentId);
    } catch (err) {
      outcome = toRetryOutcome(err);
    }
    setRetryMessage({ forKey: messageKey, text: RETRY_RESULT[outcome] });
    if (outcome === "started" || outcome === "already_running") await live.refresh();
    setRetrying(false);
  }

  if (!summary) {
    return (
      <section aria-labelledby="processing-heading" className="grid gap-2" data-testid="processing-panel">
        <h2 id="processing-heading" className="text-sm font-medium">
          Processing
        </h2>
        <p className="text-sm text-muted-foreground">Processing status isn&apos;t available right now.</p>
      </section>
    );
  }

  const kind = displayKind(summary);
  const failed = kind === "failed" || kind === "unsupported" || kind === "no_text";
  const timeline = [...live.events].reverse();

  return (
    <section aria-labelledby="processing-heading" className="grid gap-3" data-testid="processing-panel" data-state={kind}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="processing-heading" className="text-sm font-medium">
          Processing
        </h2>
        <IngestionBadge summary={summary} />
      </div>

      <p className="sr-only" role="status" aria-live="polite" data-testid="processing-live">
        {announcement(summary)}
      </p>

      {failed && (
        <p className="rounded-md bg-muted px-3 py-2 text-sm" data-testid="processing-failure">
          {failureMessage(summary.error_code, summary.message)}
        </p>
      )}
      {summary.attempts_label && (kind === "queued" || kind === "processing") && (
        <p className="text-xs text-muted-foreground" data-testid="attempts-label">
          {summary.attempts_label}
        </p>
      )}
      {live.connection === "stopped" && (
        <p className="text-xs text-muted-foreground" data-testid="live-stopped">
          Live updates are paused. Reload the page to check again.
        </p>
      )}
      {kind === "ready" && <p className="text-sm text-muted-foreground">This document is searchable.</p>}
      {kind === "none" && <p className="text-sm text-muted-foreground">This document hasn&apos;t been processed yet.</p>}

      <ol aria-label="Processing steps" className="grid gap-1 text-sm" data-testid="processing-steps">
        {PROGRESS_STEPS.map((step, i) => {
          const st = stepStatus(i, summary);
          return (
            <li
              key={step.key}
              aria-current={st === "current" ? "step" : undefined}
              data-step={step.key}
              data-status={st}
              className={st === "todo" ? "text-muted-foreground" : st === "failed" ? "font-medium" : undefined}
            >
              {step.label} <span className="text-xs text-muted-foreground">({STEP_TEXT[st]})</span>
            </li>
          );
        })}
      </ol>

      {canWrite && summary.can_retry && (
        <div className="grid justify-items-start gap-1">
          <Button variant="outline" size="sm" onClick={() => void onRetry()} disabled={retrying} aria-busy={retrying}>
            <RotateCcwIcon aria-hidden="true" />
            {retrying ? "Retrying…" : summary.state === "none" ? "Process now" : "Retry"}
          </Button>
        </div>
      )}
      <p role="status" aria-live="polite" className="text-sm text-muted-foreground empty:hidden" data-testid="retry-result">
        {shownMessage}
      </p>

      {timeline.length > 0 && (
        <div className="grid gap-1">
          <h3 className="text-xs font-medium text-muted-foreground">History</h3>
          <ol className="grid gap-0.5 text-xs" data-testid="processing-timeline">
            {timeline.map((e, i) => (
              <li key={`${e.seq ?? "x"}-${i}`} className="flex flex-wrap justify-between gap-2">
                <span>{eventLabel(e)}</span>
                {e.at && <time dateTime={e.at}>{formatDateTime(e.at)}</time>}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
