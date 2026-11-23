"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import { SurfaceCard } from "@/components/ui/surface";
import { Skeleton } from "@/components/ui/skeleton";
import { useApiFetcher } from "@/lib/query/provider";
import type { Workspace } from "@/features/workspaces/api";
import { MAX_MESSAGE_LENGTH, createThread, getThread, startRun, toChatErrorCode, type ChatMessage, type ThreadDetail } from "./api";
import { RememberButton } from "@/features/memory/remember-button";
import { AnswerBody, CopyButton, SourceList, VerificationBanner, type LinkCtx } from "./answer-view";
import { Composer } from "./composer";
import { FeedbackButtons } from "./feedback-buttons";
import { chatKeys, newIdempotencyKey } from "./keys";
import { runErrorMessage, sendErrorMessage } from "./messages";
import { isTerminalStatus, type RunView } from "./run-state";
import type { Citation } from "./api";
import type { RunStream } from "./sse";
import { threadTitle } from "./thread-list";
import { useChatRun, type Connection } from "./use-chat-run";

export type ConversationProps = {
  orgId: string;
  orgSlug: string;
  workspaces: readonly Workspace[];
  /** Null = a new chat (the thread is created with the first message). */
  threadId: string | null;
  stream?: RunStream;
  /** Select a citation into the evidence panel. */
  onCite?: (c: Citation) => void;
  activeChunkId?: string | null;
};

/** A send that failed for want of an answer (network): resending reuses the same key, so it can never run twice. */
type Pending = { content: string; key: string; threadId: string | null };

const STAGE_LABEL = {
  searching: "Searching your documents…",
  reading: "Reading sources…",
  writing: "Writing the answer…",
  verifying: "Verifying against sources…",
} as const;

const CONNECTION_NOTE: Partial<Record<Connection, string>> = {
  reconnecting: "Connection interrupted. Reconnecting…",
  polling: "Live updates are paused. Checking progress every few seconds…",
  stopped: "Live updates aren't available right now. The answer will appear here when it is ready; you can also reload.",
};

export function Conversation({ orgId, orgSlug, workspaces, threadId, stream, onCite, activeChunkId }: ConversationProps) {
  const fetcher = useApiFetcher();
  const router = useRouter();
  const queryClient = useQueryClient();
  const base = `/o/${encodeURIComponent(orgSlug)}/chat`;
  const ctx: LinkCtx = { orgSlug, workspaces, onCite, activeChunkId };

  const threadQuery = useQuery({
    queryKey: chatKeys.thread(orgId, threadId ?? "new"),
    enabled: threadId !== null,
    retry: false,
    staleTime: 5_000,
    queryFn: ({ signal }) => getThread(fetcher, orgId, threadId!, signal),
  });
  const detail = threadQuery.data ?? null;
  const loadError = threadQuery.isError ? toChatErrorCode(threadQuery.error) : null;

  // --- which run is shown: the one just sent here, else the one the server says is active (resumed after reload) ---
  const [localRunId, setLocalRunId] = useState<string | null>(null);
  const [stickyRunId, setStickyRunId] = useState<string | null>(null);
  const serverRunId = detail?.active_run?.run_id ?? null;
  if (serverRunId && serverRunId !== stickyRunId && localRunId === null) setStickyRunId(serverRunId);
  const runId = localRunId ?? stickyRunId;

  const onSettled = useCallback(() => {
    if (threadId) void queryClient.invalidateQueries({ queryKey: chatKeys.thread(orgId, threadId) });
    void queryClient.invalidateQueries({ queryKey: chatKeys.threads(orgId) });
  }, [queryClient, orgId, threadId]);
  const live = useChatRun({ orgId, threadId: threadId ?? "new", runId, stream, onSettled });
  const view = live.view;
  const running = view !== null && !isTerminalStatus(view.status);

  // `?cite=<chunk id>` reopens a passage in the panel once the history that cites it has loaded.
  const citeRestored = useRef(false);
  useEffect(() => {
    if (citeRestored.current || !detail || !onCite) return;
    citeRestored.current = true;
    const want = new URLSearchParams(window.location.search).get("cite");
    if (!want) return;
    const hit = detail.messages.flatMap((m) => m.citations).find((c) => c.chunk_id === want);
    if (hit) onCite(hit);
  }, [detail, onCite]);

  // --- composer ---
  const [draft, setDraft] = useState("");
  // Home quick-Ask links here as /chat?q=<text>: prefill the composer once on the client.
  useEffect(() => {
    try {
      const q = new URLSearchParams(window.location.search).get("q");
      // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time client-only prefill; reading the URL during render would mismatch server HTML
      if (q) setDraft((cur) => cur || q.slice(0, MAX_MESSAGE_LENGTH));
    } catch {
      /* no URL access: ignore */
    }
  }, []);
  const [scopeChoice, setScopeChoice] = useState<string | null | undefined>(undefined);
  const scope = scopeChoice !== undefined ? scopeChoice : (detail?.thread.workspace_id ?? null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [cancelError, setCancelError] = useState(false);
  const draftRef = useRef<HTMLDivElement>(null);

  async function send(content: string, again?: Pending) {
    if (sending) return;
    setSending(true);
    setSendError(null);
    const key = again?.key ?? newIdempotencyKey();
    let tid = again?.threadId ?? threadId;
    try {
      let created = null;
      if (!tid) {
        created = await createThread(fetcher, orgId, { workspace_id: scope });
        tid = created.id;
      }
      const accepted = await startRun(fetcher, orgId, tid, { content, idempotency_key: key, ...(scope ? { workspace_ids: [scope] } : {}) });
      const message: ChatMessage = {
        id: accepted.message_id || `local-${key}`,
        role: "user",
        content,
        citations: [],
        verification: null,
        run_id: accepted.run_id,
        created_at: new Date().toISOString(),
      };
      const finalTid = tid;
      queryClient.setQueryData<ThreadDetail>(chatKeys.thread(orgId, finalTid), (old) => {
        const seed: ThreadDetail = old ?? {
          thread: created ?? { id: finalTid, workspace_id: scope, title: "", created_at: "", updated_at: "" },
          messages: [],
          active_run: null,
        };
        const messages = seed.messages.some((m) => m.id === message.id) ? seed.messages : [...seed.messages, message];
        return { ...seed, messages, active_run: { run_id: accepted.run_id, state: accepted.state } };
      });
      void queryClient.invalidateQueries({ queryKey: chatKeys.threads(orgId) });
      setDraft("");
      setPending(null);
      setCancelError(false);
      if (threadId === null) {
        // The conversation for the new thread mounts at its own URL and resumes this run from the seeded cache.
        router.push(`${base}/${encodeURIComponent(finalTid)}`);
      } else {
        setLocalRunId(accepted.run_id);
      }
    } catch (err) {
      const code = toChatErrorCode(err);
      setPending(code === "network" ? { content, key, threadId: tid } : null);
      setSendError(sendErrorMessage(code));
    } finally {
      setSending(false);
    }
  }

  const lastUser = [...(detail?.messages ?? [])].reverse().find((m) => m.role === "user") ?? null;
  const retry = () => {
    const content = lastUser?.content ?? pending?.content;
    if (content) void send(content); // a new run needs a new idempotency key
  };

  async function cancel() {
    setCancelError(false);
    if (!(await live.cancel())) setCancelError(true);
  }

  // --- scrolling and announcements ---
  const endRef = useRef<HTMLDivElement>(null);
  const messageCount = detail?.messages.length ?? 0;
  const streamed = view?.text.length ?? 0;
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [messageCount, streamed]);
  const announcement = !view
    ? ""
    : view.status === "completed"
      ? "Answer ready."
      : view.status === "failed"
        ? "The answer failed."
        : view.status === "cancelled"
          ? "Answer cancelled."
          : "Writing an answer…";

  if (threadId !== null && threadQuery.isPending) {
    return (
      <div role="status" aria-label="Loading conversation" className="grid gap-3" data-testid="conversation-loading">
        <Skeleton className="h-16 w-2/3" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }
  if (threadId !== null && loadError) {
    const gone = loadError === "not_found";
    return (
      <div role="alert" className="grid justify-items-start gap-3 rounded-card border border-dashed border-border bg-surface p-6" data-testid="conversation-error" data-code={loadError}>
        <h1 tabIndex={-1} className="text-lg font-semibold tracking-tight outline-none">
          {gone ? "This conversation isn't available" : "Temporarily unavailable"}
        </h1>
        <p className="max-w-md text-sm text-muted-foreground">
          {gone ? "It may have been deleted, or it may belong to someone else." : "We couldn't load this conversation. Your data is safe; try again in a moment."}
        </p>
        {gone ? (
          <Link href={base} className="text-sm underline underline-offset-2">
            Start a new chat
          </Link>
        ) : (
          <Button variant="outline" onClick={() => void threadQuery.refetch()}>
            Try again
          </Button>
        )}
      </div>
    );
  }

  const messages = detail?.messages ?? [];
  const persistedAnswer = runId !== null && messages.some((m) => m.role === "assistant" && m.run_id === runId);
  const showLive = view !== null && !persistedAnswer;
  // A last question with no answer at all (e.g. the run failed before a reload): offer a retry.
  const unanswered = !running && !showLive && !sending && messages.at(-1)?.role === "user";

  return (
    <div className="grid min-w-0 gap-4" data-testid="conversation" data-thread-id={threadId ?? ""}>
      <PageHeader eyebrow="Ask Intello" title={detail ? threadTitle(detail.thread) : "New chat"} titleTestId="conversation-title" />
      {/* Announces state changes only (writing / ready / failed / cancelled), never the streamed tokens. */}
      <p role="status" aria-live="polite" aria-atomic="true" className="sr-only" data-testid="chat-status">
        {announcement}
      </p>

      <div className="grid gap-4" data-testid="messages">
        {messages.length === 0 && !showLive && (
          <p className="rounded-card border border-dashed border-border bg-surface p-6 text-sm text-text-secondary" data-testid="chat-empty">
            Ask a question about your documents. Answers cite their sources, and say so plainly when your documents don&apos;t contain the answer.
          </p>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} ctx={ctx} orgId={orgId} />
        ))}
        {showLive && view && (
          <LiveAnswer
            view={view}
            connection={live.connection}
            ctx={ctx}
            onRetry={retry}
            canRetry={lastUser !== null && !sending}
          />
        )}
        {unanswered && (
          <div className="grid justify-items-start gap-2 rounded-card border border-border bg-surface p-3 text-sm" data-testid="unanswered">
            <p>This question has no answer.</p>
            <Button variant="outline" size="sm" onClick={retry}>
              Retry
            </Button>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {sendError && (
        <div role="alert" className="grid justify-items-start gap-2 rounded-control border border-border bg-warning-soft p-3 text-sm" data-testid="send-error">
          <p>{sendError}</p>
          {pending && (
            <Button variant="outline" size="sm" onClick={() => void send(pending.content, pending)} data-testid="resend">
              Retry sending
            </Button>
          )}
        </div>
      )}
      {cancelError && (
        <p role="alert" className="text-sm text-destructive" data-testid="cancel-error">
          We couldn&apos;t cancel that. It may already be finished.
        </p>
      )}

      <div ref={draftRef}>
        <Composer
          value={draft}
          onChange={setDraft}
          onSend={() => void send(draft.trim())}
          busy={running || sending}
          onCancel={running ? () => void cancel() : undefined}
          cancelling={live.cancelling}
          workspaces={workspaces}
          scope={scope}
          onScope={setScopeChoice}
        />
      </div>
    </div>
  );
}

function MessageBubble({ message, ctx, orgId }: { message: ChatMessage; ctx: LinkCtx; orgId: string }) {
  if (message.role === "user") {
    return (
      <div className="ml-auto max-w-[85%] rounded-card rounded-br-control bg-brand-indigo px-4 py-2.5 text-sm break-words whitespace-pre-wrap text-white" data-testid="user-message">
        {message.content}
      </div>
    );
  }
  return (
    <SurfaceCard as="article" className="grid max-w-[95%] gap-3 p-4" data-testid="assistant-message" data-run-id={message.run_id ?? ""}>
      <VerificationBanner verification={message.verification} />
      <AnswerBody text={message.content} citations={message.citations} ctx={ctx} />
      <SourceList citations={message.citations} ctx={ctx} />
      <div className="flex flex-wrap items-center gap-1">
        <CopyButton text={message.content} />
        <RememberButton orgId={orgId} orgSlug={ctx.orgSlug} answer={message.content} citations={message.citations} />
        {message.run_id ? <FeedbackButtons orgId={orgId} messageId={message.id} /> : null}
      </div>
    </SurfaceCard>
  );
}

function LiveAnswer({
  view,
  connection,
  ctx,
  onRetry,
  canRetry,
}: {
  view: RunView;
  connection: Connection;
  ctx: LinkCtx;
  onRetry: () => void;
  canRetry: boolean;
}) {
  const note = CONNECTION_NOTE[connection];
  if (view.status === "failed") {
    return (
      <div role="alert" className="grid justify-items-start gap-2 rounded-card border border-border bg-danger-soft p-3 text-sm" data-testid="run-failed" data-code={view.error?.code ?? ""}>
        <p>{runErrorMessage(view.error?.code)}</p>
        <Button variant="outline" size="sm" onClick={onRetry} disabled={!canRetry} data-testid="retry-run">
          Retry
        </Button>
      </div>
    );
  }
  if (view.status === "cancelled") {
    return (
      <div className="grid gap-2" data-testid="run-cancelled">
        {view.text && (
          <SurfaceCard className="grid max-w-[95%] gap-2 p-4" data-testid="cancelled-partial">
            <p className="whitespace-pre-wrap text-sm">{view.text}</p>
            <p className="text-xs text-muted-foreground">Unverified partial answer: no sources.</p>
          </SurfaceCard>
        )}
        <p className="text-sm text-muted-foreground">
          Cancelled.{" "}
          <Button variant="ghost" size="sm" onClick={onRetry} disabled={!canRetry}>
            Ask again
          </Button>
        </p>
      </div>
    );
  }
  const done = view.status === "completed";
  return (
    <SurfaceCard className="grid max-w-[95%] gap-3 p-4" data-testid="live-answer" data-status={view.status} aria-busy={!done}>
      {done && <VerificationBanner verification={view.verification} />}
      {view.text ? (
        <AnswerBody text={view.text} citations={view.citations} ctx={ctx} />
      ) : (
        <p className="text-sm text-muted-foreground" data-testid="run-waiting">
          {STAGE_LABEL[view.stage]}
        </p>
      )}
      {!done && view.text && (
        <p className="text-xs text-muted-foreground" data-testid="run-stage" data-stage={view.stage}>
          {STAGE_LABEL[view.stage]}
        </p>
      )}
      {done && <SourceList citations={view.citations} ctx={ctx} />}
      {done && view.text && (
        <div>
          <CopyButton text={view.text} />
        </div>
      )}
      {!done && note && (
        <p role="status" className="text-xs text-muted-foreground" data-testid="connection-note">
          {note}
        </p>
      )}
    </SurfaceCard>
  );
}
