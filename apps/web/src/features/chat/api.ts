/**
 * Chat API client + parsing (contract m04 §8 as described for the web agent; the "Chat API (as built)" section of the
 * contract does not exist yet). EVERY wire shape the chat UI relies on lives in this module, so a refinement is a
 * small change here. Everything is parsed defensively: malformed items are dropped, unknown event types ignored.
 */
import { ApiError, invalid, isRecord, jsonBody, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";

export const MAX_MESSAGE_LENGTH = 4000;
export const MIN_KEY_LENGTH = 8;

export type Thread = {
  id: string;
  workspace_id: string | null;
  title: string;
  created_at: string;
  updated_at: string;
};

export type Citation = {
  /** `"[1]"`: the marker as it appears in the answer text. */
  marker: string;
  chunk_id: string;
  document_id: string;
  version_id: string;
  workspace_id: string;
  title: string;
  version_number: number | null;
  page_start: number | null;
  page_end: number | null;
  excerpt: string;
};

export const VERIFICATION_STATUSES = ["grounded", "partial", "abstained", "unverified"] as const;
export type VerificationStatus = (typeof VERIFICATION_STATUSES)[number];
export type Verification = {
  status: VerificationStatus;
  withheld_count: number;
  flagged_count: number;
  /** Set when the answer was cut short (`unverified` only): the user cancelled, or the model timed out. */
  stopped?: "cancelled" | "timeout";
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  verification: Verification | null;
  run_id: string | null;
  created_at: string;
};

export const RUN_STATES = ["queued", "running", "completed", "failed", "cancelled"] as const;
export type RunState = (typeof RUN_STATES)[number];
export const isTerminalState = (s: RunState): boolean => s === "completed" || s === "failed" || s === "cancelled";

export type ThreadDetail = {
  thread: Thread;
  messages: ChatMessage[];
  active_run: { run_id: string; state: RunState } | null;
};

export type RunAccepted = { run_id: string; message_id: string; state: RunState };

export type RunInfo = {
  run_id: string;
  thread_id: string | null;
  state: RunState;
  error_code: string | null;
  finished_at: string | null;
};

function nullableInt(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? Math.trunc(v) : null;
}
function count(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) && v > 0 ? Math.trunc(v) : 0;
}
function parseRunState(v: unknown): RunState {
  if (typeof v !== "string" || !(RUN_STATES as readonly string[]).includes(v)) invalid();
  return v as RunState;
}

export function parseThread(v: unknown): Thread {
  if (!isRecord(v)) invalid();
  return {
    id: str(v, "id"),
    workspace_id: typeof v.workspace_id === "string" ? v.workspace_id : null,
    title: typeof v.title === "string" ? v.title : "",
    created_at: typeof v.created_at === "string" ? v.created_at : "",
    updated_at: typeof v.updated_at === "string" ? v.updated_at : "",
  };
}

export function parseCitation(v: unknown): Citation {
  if (!isRecord(v)) invalid();
  const marker = str(v, "marker");
  if (!/^\[\d{1,3}\]$/.test(marker)) invalid();
  return {
    marker,
    chunk_id: str(v, "chunk_id"),
    document_id: str(v, "document_id"),
    version_id: str(v, "version_id"),
    workspace_id: str(v, "workspace_id"),
    title: typeof v.title === "string" ? v.title : "",
    version_number: nullableInt(v.version_number),
    page_start: nullableInt(v.page_start),
    page_end: nullableInt(v.page_end),
    excerpt: typeof v.excerpt === "string" ? v.excerpt : "",
  };
}

export function parseCitations(v: unknown): Citation[] {
  if (!Array.isArray(v)) return [];
  const out: Citation[] = [];
  for (const c of v) {
    try {
      out.push(parseCitation(c));
    } catch {
      // a malformed citation is dropped; its marker then renders as plain text
    }
  }
  return out;
}

export function parseVerification(v: unknown): Verification | null {
  if (!isRecord(v)) return null;
  const status = v.status;
  if (typeof status !== "string" || !(VERIFICATION_STATUSES as readonly string[]).includes(status)) return null;
  const stopped = v.stopped === "cancelled" || v.stopped === "timeout" ? v.stopped : undefined;
  return { status: status as VerificationStatus, withheld_count: count(v.withheld_count), flagged_count: count(v.flagged_count), ...(stopped ? { stopped } : {}) };
}

export function parseMessage(v: unknown): ChatMessage {
  if (!isRecord(v)) invalid();
  const role = v.role;
  if (role !== "user" && role !== "assistant") invalid();
  return {
    id: str(v, "id"),
    role,
    content: typeof v.content === "string" ? v.content : "",
    citations: parseCitations(v.citations),
    verification: parseVerification(v.verification),
    run_id: typeof v.run_id === "string" ? v.run_id : null,
    created_at: typeof v.created_at === "string" ? v.created_at : "",
  };
}

export function parseThreadDetail(v: unknown): ThreadDetail {
  if (!isRecord(v)) invalid();
  const messages: ChatMessage[] = [];
  if (Array.isArray(v.messages)) {
    for (const m of v.messages) {
      try {
        messages.push(parseMessage(m));
      } catch {
        // dropped
      }
    }
  }
  let active: ThreadDetail["active_run"] = null;
  if (isRecord(v.active_run) && typeof v.active_run.run_id === "string") {
    try {
      active = { run_id: v.active_run.run_id, state: parseRunState(v.active_run.state) };
    } catch {
      active = null;
    }
  }
  return { thread: parseThread(v.thread), messages, active_run: active };
}

export type ThreadPage = { items: Thread[]; next_cursor: string | null };

export function parseThreadPage(v: unknown): ThreadPage {
  if (!isRecord(v) || !Array.isArray(v.items)) invalid();
  const items: Thread[] = [];
  for (const t of v.items) {
    try {
      items.push(parseThread(t));
    } catch {
      // dropped
    }
  }
  return { items, next_cursor: typeof v.next_cursor === "string" ? v.next_cursor : null };
}

export function parseRunAccepted(v: unknown): RunAccepted {
  if (!isRecord(v)) invalid();
  return { run_id: str(v, "run_id"), message_id: typeof v.message_id === "string" ? v.message_id : "", state: parseRunState(v.state ?? "queued") };
}

export function parseRunInfo(v: unknown): RunInfo {
  if (!isRecord(v)) invalid();
  return {
    run_id: str(v, "run_id"),
    thread_id: typeof v.thread_id === "string" ? v.thread_id : null,
    state: parseRunState(v.state),
    error_code: typeof v.error_code === "string" ? v.error_code : null,
    finished_at: typeof v.finished_at === "string" ? v.finished_at : null,
  };
}

// --- paths ---

const orgBase = (orgId: string) => `/v1/orgs/${encodeURIComponent(orgId)}/chat`;
export const threadsPath = (orgId: string): ApiPath => `${orgBase(orgId)}/threads` as ApiPath;
export const threadPath = (orgId: string, threadId: string): ApiPath => `${threadsPath(orgId)}/${encodeURIComponent(threadId)}` as ApiPath;
export const runsPath = (orgId: string, threadId: string): ApiPath => `${threadPath(orgId, threadId)}/runs` as ApiPath;
export const runPath = (orgId: string, runId: string): ApiPath => `${orgBase(orgId)}/runs/${encodeURIComponent(runId)}` as ApiPath;
/** The SSE URL the browser opens (same-origin BFF). */
export const runEventsUrl = (orgId: string, runId: string): string => `/api${runPath(orgId, runId)}/events`;

// --- endpoints ---

export function createThread(fetcher: Fetcher, orgId: string, input: { workspace_id?: string | null; title?: string }): Promise<Thread> {
  const body: Record<string, unknown> = {};
  if (input.workspace_id) body.workspace_id = input.workspace_id;
  if (input.title) body.title = input.title;
  return requestJson(fetcher, threadsPath(orgId), parseThread, jsonBody(body));
}

export function listThreads(fetcher: Fetcher, orgId: string, opts: { limit?: number; after?: string | null } = {}, signal?: AbortSignal): Promise<ThreadPage> {
  const qs = new URLSearchParams({ limit: String(opts.limit ?? 30) });
  if (opts.after) qs.set("after", opts.after);
  return requestJson(fetcher, `${threadsPath(orgId)}?${qs}` as ApiPath, parseThreadPage, { signal });
}

export function getThread(fetcher: Fetcher, orgId: string, threadId: string, signal?: AbortSignal): Promise<ThreadDetail> {
  return requestJson(fetcher, threadPath(orgId, threadId), parseThreadDetail, { signal });
}

export async function deleteThread(fetcher: Fetcher, orgId: string, threadId: string): Promise<void> {
  let res: Response;
  try {
    res = await fetcher(threadPath(orgId, threadId), { method: "DELETE" });
  } catch {
    throw new ApiError(0, "network_error");
  }
  if (res.status !== 204 && !res.ok) throw new ApiError(res.status, `http_${res.status}`);
}

export type StartRunInput = { content: string; idempotency_key: string; workspace_ids?: string[] };

/** 202 `{run_id, message_id, state}`. The same `idempotency_key` yields the same run (never a duplicate). */
export function startRun(fetcher: Fetcher, orgId: string, threadId: string, input: StartRunInput): Promise<RunAccepted> {
  return requestJson(fetcher, runsPath(orgId, threadId), parseRunAccepted, jsonBody(input));
}

export function getRun(fetcher: Fetcher, orgId: string, runId: string, signal?: AbortSignal): Promise<RunInfo> {
  return requestJson(fetcher, runPath(orgId, runId), parseRunInfo, { signal });
}

export function cancelRun(fetcher: Fetcher, orgId: string, runId: string): Promise<{ state: RunState | null }> {
  return requestJson(
    fetcher,
    `${runPath(orgId, runId)}/cancel` as ApiPath,
    (v) => ({ state: isRecord(v) && typeof v.state === "string" && (RUN_STATES as readonly string[]).includes(v.state) ? (v.state as RunState) : null }),
    { method: "POST" },
  );
}

// --- run events (SSE) ---

export type RunEvent =
  | { type: "started" }
  | { type: "retrieval"; evidence_count: number; dropped: number; degraded: string[] }
  | { type: "delta"; text: string }
  | { type: "citations"; citations: Citation[] }
  | { type: "verification"; verification: Verification }
  | { type: "completed"; message_id: string | null; answer_text: string }
  | { type: "failed"; error_code: string; message: string }
  | { type: "cancelled" }
  | { type: "stage"; stage: string };

export type StreamEnd = { type: "end"; reason: "terminal" | "timeout" | "unavailable" };

/** Parses one SSE frame body. Unknown types and malformed data are `null` (ignored by the client). */
export function parseRunEvent(type: string, data: string): RunEvent | StreamEnd | null {
  let body: unknown;
  try {
    body = data === "" ? {} : JSON.parse(data);
  } catch {
    return null;
  }
  if (!isRecord(body)) return null;
  switch (type) {
    case "started":
      return { type: "started" };
    case "retrieval":
      return {
        type: "retrieval",
        evidence_count: count(body.evidence_count),
        dropped: count(body.dropped),
        degraded: Array.isArray(body.degraded) ? body.degraded.filter((d): d is string => typeof d === "string") : [],
      };
    case "delta":
      return typeof body.text === "string" ? { type: "delta", text: body.text } : null;
    case "citations":
      return { type: "citations", citations: parseCitations(body.citations) };
    case "verification": {
      const verification = parseVerification(body);
      return verification ? { type: "verification", verification } : null;
    }
    case "completed":
      return typeof body.answer_text === "string"
        ? { type: "completed", message_id: typeof body.message_id === "string" ? body.message_id : null, answer_text: body.answer_text }
        : null;
    case "failed":
      return {
        type: "failed",
        error_code: typeof body.error_code === "string" ? body.error_code : "internal_error",
        message: typeof body.message === "string" ? body.message : "",
      };
    case "cancelled":
      return { type: "cancelled" };
    case "stage":
      return typeof body.stage === "string" ? { type: "stage", stage: body.stage } : null;
    case "end": {
      const r = body.reason;
      return { type: "end", reason: r === "terminal" || r === "timeout" ? r : "unavailable" };
    }
    default:
      return null;
  }
}

// --- errors ---

export type ChatErrorCode = "rate_limited" | "too_many_runs" | "not_found" | "invalid" | "unavailable" | "network" | "unauthenticated" | "unknown";

export function toChatErrorCode(err: unknown): ChatErrorCode {
  if (!(err instanceof ApiError)) return "unknown";
  if (err.status === 401) return "unauthenticated";
  if (err.status === 0 || err.code === "network_error") return "network";
  if (err.code === "too_many_runs") return "too_many_runs";
  if (err.status === 429 || err.code === "rate_limited") return "rate_limited";
  if (err.status === 422 || err.code === "invalid_request") return "invalid";
  if (err.status === 404 || err.status === 403) return "not_found";
  if (err.status === 409) return "too_many_runs";
  if (err.status === 502 || err.status === 503 || err.status === 504) return "unavailable";
  return "unknown";
}
