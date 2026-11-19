/**
 * Ingestion status API client + parsing (contract m03 §11). EVERY wire shape the UI assumes lives in this one
 * module, so a refinement of the contract is a change here and nowhere else.
 *
 * Assumptions beyond the written contract (flagged for reconciliation):
 *  - state may also be "cancelled" (deleted-document cancellation, m03 §9); unknown states are rejected.
 *  - the status body / SSE `job` payloads may carry an optional `events: [{seq, state, stage, error_code, at}]`
 *    timeline and an optional `at` / `created_at` timestamp per event; absent fields are tolerated.
 *  - stage is one of extract|chunk|embed|index|validate|publish; anything else is shown without a stage.
 */
import {
  ApiError,
  invalid,
  isRecord,
  requestJson,
  type ApiPath,
  type Fetcher,
} from "@/features/organizations/api/http";

export const INGESTION_STATES = ["none", "queued", "processing", "indexed", "failed", "unsupported", "cancelled"] as const;
export type IngestionState = (typeof INGESTION_STATES)[number];

export const STAGES = ["extract", "chunk", "embed", "index", "validate", "publish"] as const;
export type Stage = (typeof STAGES)[number];

/** The `Document.ingestion` summary (list items and single document). */
export type IngestionSummary = {
  state: IngestionState;
  stage: Stage | null;
  updated_at: string | null;
  error_code: string | null;
};

export type IngestionEvent = {
  /** `ingestion_job_events.seq` when known (SSE `id`), used to de-duplicate. */
  seq: number | null;
  state: IngestionState;
  stage: Stage | null;
  error_code: string | null;
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
  return { state: parseState(v), stage: parseStage(v), updated_at: nullableString(v, "updated_at"), error_code: nullableString(v, "error_code") };
}

/** For `Document.ingestion`: a missing or malformed summary must never break the whole list. */
export function parseSummaryLoose(v: unknown): IngestionSummary | null {
  try {
    return parseSummary(v);
  } catch {
    return null;
  }
}

export function parseEvent(v: unknown): IngestionEvent {
  if (!isRecord(v)) invalid();
  const seq = v.seq;
  return {
    seq: typeof seq === "number" && Number.isFinite(seq) ? seq : null,
    state: parseState(v),
    stage: parseStage(v),
    error_code: nullableString(v, "error_code"),
    at: nullableString(v, "at") ?? nullableString(v, "created_at") ?? nullableString(v, "updated_at"),
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

export function parseRetry(v: unknown): { job_id: string } {
  if (!isRecord(v) || typeof v.job_id !== "string") invalid();
  return { job_id: v.job_id };
}

const docPath = (orgId: string, workspaceId: string, documentId: string): string =>
  `/v1/orgs/${encodeURIComponent(orgId)}/workspaces/${encodeURIComponent(workspaceId)}/documents/${encodeURIComponent(documentId)}`;

export const ingestionPath = (o: string, w: string, d: string): ApiPath => `${docPath(o, w, d)}/ingestion` as ApiPath;
export const ingestionEventsPath = (o: string, w: string, d: string): ApiPath => `${docPath(o, w, d)}/ingestion/events` as ApiPath;

export function getIngestion(fetcher: Fetcher, orgId: string, workspaceId: string, documentId: string): Promise<IngestionStatus> {
  return requestJson(fetcher, ingestionPath(orgId, workspaceId, documentId), parseStatus);
}

/** 202 `{job_id}`; 409 when a job is already active, 403/404 when the caller may not write. */
export function retryIngestion(fetcher: Fetcher, orgId: string, workspaceId: string, documentId: string): Promise<{ job_id: string }> {
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

/** Parses one SSE `data:` payload (`snapshot` = status shape, `job` = event shape); null when unusable. */
export function parseStreamPayload(data: string): { summary: IngestionSummary; event: IngestionEvent | null; status: IngestionStatus | null } | null {
  try {
    const body: unknown = JSON.parse(data);
    const summary = parseSummary(body);
    let event: IngestionEvent | null = null;
    try {
      event = parseEvent(body);
    } catch {
      event = null;
    }
    let status: IngestionStatus | null = null;
    try {
      status = parseStatus(body);
    } catch {
      status = null;
    }
    return { summary, event, status };
  } catch {
    return null;
  }
}
