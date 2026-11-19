/**
 * Ingestion status API client + parsing (contract m03 §11 as implemented in §13; source of truth:
 * apps/api/app/modules/ingestion/schemas.py). EVERY wire shape the UI relies on lives in this module.
 */
import {
  ApiError,
  invalid,
  isRecord,
  requestJson,
  type ApiPath,
  type Fetcher,
} from "@/features/organizations/api/http";

export const INGESTION_STATES = ["none", "queued", "processing", "ready", "failed", "unsupported", "no_text", "cancelled"] as const;
export type IngestionState = (typeof INGESTION_STATES)[number];

export const STAGES = ["extract", "chunk", "embed", "index", "validate", "publish"] as const;
export type Stage = (typeof STAGES)[number];

/** `Document.ingestion` and the head of the status body. `message` is server-supplied and safe to show. */
export type IngestionSummary = {
  state: IngestionState;
  stage: Stage | null;
  attempts_label: string | null;
  error_code: string | null;
  message: string | null;
  updated_at: string | null;
  /** Server-computed: retryable state AND document.write. */
  can_retry: boolean;
};

export type IngestionEvent = {
  /** `ingestion_job_events.seq` (the SSE `id`), used to de-duplicate. */
  seq: number | null;
  type: string;
  label: string;
  stage: Stage | null;
  attempt: number | null;
  at: string | null;
};

/** `GET {ws}/documents/{id}/ingestion` */
export type IngestionStatus = IngestionSummary & {
  attempts: number | null;
  max_attempts: number | null;
  revision_id: string | null;
  published_at: string | null;
  events: IngestionEvent[];
};

export const isTerminal = (state: IngestionState): boolean => state !== "queued" && state !== "processing";

function nullableString(o: Record<string, unknown>, key: string): string | null {
  const v = o[key];
  if (v === null || v === undefined) return null;
  if (typeof v !== "string") invalid();
  return v;
}
function nullableNumber(o: Record<string, unknown>, key: string): number | null {
  const v = o[key];
  if (v === null || v === undefined) return null;
  if (typeof v !== "number" || !Number.isFinite(v)) invalid();
  return v;
}
function parseState(o: Record<string, unknown>): IngestionState {
  const v = o.state;
  if (typeof v !== "string" || !(INGESTION_STATES as readonly string[]).includes(v)) invalid();
  return v as IngestionState;
}
function parseStage(o: Record<string, unknown>): Stage | null {
  const v = o.stage;
  return typeof v === "string" && (STAGES as readonly string[]).includes(v) ? (v as Stage) : null;
}

export function parseSummary(v: unknown): IngestionSummary {
  if (!isRecord(v)) invalid();
  return {
    state: parseState(v),
    stage: parseStage(v),
    attempts_label: nullableString(v, "attempts_label"),
    error_code: nullableString(v, "error_code"),
    message: nullableString(v, "message"),
    updated_at: nullableString(v, "updated_at"),
    can_retry: v.can_retry === true,
  };
}

/** For `Document.ingestion`: a missing or malformed summary must never break the whole list. */
export function parseSummaryLoose(v: unknown): IngestionSummary | null {
  try {
    return parseSummary(v);
  } catch {
    return null;
  }
}

/** Timeline entry `{id, type, label, stage, attempt, at}`; SSE `job` frames use `seq` instead of `id`. */
export function parseEvent(v: unknown): IngestionEvent {
  if (!isRecord(v)) invalid();
  const id = typeof v.id === "number" ? v.id : typeof v.seq === "number" ? v.seq : null;
  return {
    seq: id !== null && Number.isFinite(id) ? id : null,
    type: typeof v.type === "string" ? v.type : "",
    label: typeof v.label === "string" ? v.label : "",
    stage: parseStage(v),
    attempt: typeof v.attempt === "number" ? v.attempt : null,
    at: nullableString(v, "at"),
  };
}

export function parseStatus(v: unknown): IngestionStatus {
  if (!isRecord(v)) invalid();
  const events: IngestionEvent[] = [];
  if (Array.isArray(v.events)) {
    for (const e of v.events) {
      try {
        events.push(parseEvent(e));
      } catch {
        // a malformed timeline entry is dropped, never fatal
      }
    }
  }
  return {
    ...parseSummary(v),
    attempts: nullableNumber(v, "attempts"),
    max_attempts: nullableNumber(v, "max_attempts"),
    revision_id: nullableString(v, "revision_id"),
    published_at: nullableString(v, "published_at"),
    events,
  };
}

export type RetryAccepted = { job_id: string; ingestion: IngestionSummary | null };

export function parseRetry(v: unknown): RetryAccepted {
  if (!isRecord(v) || typeof v.job_id !== "string") invalid();
  return { job_id: v.job_id, ingestion: parseSummaryLoose(v.ingestion) };
}

const docPath = (orgId: string, workspaceId: string, documentId: string): string =>
  `/v1/orgs/${encodeURIComponent(orgId)}/workspaces/${encodeURIComponent(workspaceId)}/documents/${encodeURIComponent(documentId)}`;

export const ingestionPath = (o: string, w: string, d: string): ApiPath => `${docPath(o, w, d)}/ingestion` as ApiPath;
export const ingestionEventsPath = (o: string, w: string, d: string): ApiPath => `${docPath(o, w, d)}/ingestion/events` as ApiPath;

export function getIngestion(fetcher: Fetcher, orgId: string, workspaceId: string, documentId: string): Promise<IngestionStatus> {
  return requestJson(fetcher, ingestionPath(orgId, workspaceId, documentId), parseStatus);
}

/** 202 `{job_id}`; 409 when a job is already active, 403/404 when the caller may not write. */
export function retryIngestion(fetcher: Fetcher, orgId: string, workspaceId: string, documentId: string): Promise<RetryAccepted> {
  return requestJson(fetcher, `${ingestionPath(orgId, workspaceId, documentId)}/retry` as ApiPath, parseRetry, { method: "POST" });
}

export type RetryOutcome = "started" | "already_running" | "not_allowed" | "not_found" | "unavailable" | "failed";

export function toRetryOutcome(err: unknown): RetryOutcome {
  if (!(err instanceof ApiError)) return "failed";
  if (err.status === 409) return "already_running";
  if (err.status === 403) return "not_allowed";
  if (err.status === 404 || err.status === 410) return "not_found";
  if (err.isUnavailable) return "unavailable";
  return "failed";
}

export type StreamFrame =
  | { kind: "snapshot"; summary: IngestionSummary }
  | { kind: "job"; summary: IngestionSummary; event: IngestionEvent }
  | { kind: "end"; reason: "terminal" | "timeout" | "unavailable" };

/** Parses one SSE frame (`snapshot` {cursor, document_id, version_id, ingestion}, `job` {seq, ..., ingestion}, `end` {reason}). */
export function parseStreamFrame(type: string, data: string): StreamFrame | null {
  try {
    const body: unknown = JSON.parse(data);
    if (!isRecord(body)) return null;
    if (type === "end") {
      const r = body.reason;
      return { kind: "end", reason: r === "terminal" || r === "timeout" ? r : "unavailable" };
    }
    const summary = parseSummary(body.ingestion);
    if (type === "snapshot") return { kind: "snapshot", summary };
    if (type === "job") return { kind: "job", summary, event: parseEvent(body) };
    return null;
  } catch {
    return null;
  }
}
