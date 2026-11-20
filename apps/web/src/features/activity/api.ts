/**
 * Activity & Jobs client: `GET /v1/orgs/{o}/workspaces/{w}/ingestion/jobs?state=&cursor=&limit=` ->
 * `{items: [{document_id, title, version_id, state, stage, error_code, message, attempts, updated_at}],
 *   counts: {queued, ..., cancelled}, next_cursor}`. Counts and items are permission-filtered by the server.
 */
import { invalid, isRecord, requestJson, type ApiPath, type Fetcher } from "@/features/organizations/api/http";
import { type IngestionState } from "@/features/documents/ingestion/api";

export const JOB_STATES = ["queued", "processing", "ready", "failed", "unsupported", "no_text", "cancelled"] as const;
export type JobState = (typeof JOB_STATES)[number];

export type JobItem = {
  document_id: string;
  title: string;
  version_id: string | null;
  state: JobState;
  stage: string | null;
  error_code: string | null;
  message: string | null;
  attempts: number;
  updated_at: string | null;
};
export type JobCounts = Record<JobState, number>;
export type JobsPage = { items: JobItem[]; counts: JobCounts; next_cursor: string | null };

export const emptyCounts = (): JobCounts => ({ queued: 0, processing: 0, ready: 0, failed: 0, unsupported: 0, no_text: 0, cancelled: 0 });

const optStr = (v: unknown): string | null => (typeof v === "string" ? v : null);
const isJobState = (s: unknown): s is JobState => typeof s === "string" && (JOB_STATES as readonly string[]).includes(s);

export function parseJobItem(v: unknown): JobItem {
  if (!isRecord(v) || typeof v.document_id !== "string" || !isJobState(v.state)) invalid();
  return {
    document_id: v.document_id,
    title: typeof v.title === "string" ? v.title : "Untitled",
    version_id: optStr(v.version_id),
    state: v.state,
    stage: optStr(v.stage),
    error_code: optStr(v.error_code),
    message: optStr(v.message),
    attempts: typeof v.attempts === "number" ? v.attempts : 0,
    updated_at: optStr(v.updated_at),
  };
}

export function parseJobsPage(body: unknown): JobsPage {
  if (!isRecord(body) || !Array.isArray(body.items)) invalid();
  const counts = emptyCounts();
  if (isRecord(body.counts)) for (const s of JOB_STATES) if (typeof body.counts[s] === "number") counts[s] = body.counts[s];
  return { items: body.items.map(parseJobItem), counts, next_cursor: optStr(body.next_cursor) };
}

export const activityKey = (orgId: string, workspaceId: string) => ["org", orgId, "workspace", workspaceId, "activity"] as const;

const wsBase = (o: string, w: string) => `/v1/orgs/${encodeURIComponent(o)}/workspaces/${encodeURIComponent(w)}`;

export const PAGE_SIZE = 50;

export function loadJobs(fetcher: Fetcher, orgId: string, workspaceId: string, state: JobState | "all", cursor: string | null, signal?: AbortSignal): Promise<JobsPage> {
  const q = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (state !== "all") q.set("state", state);
  if (cursor) q.set("cursor", cursor);
  return requestJson(fetcher, `${wsBase(orgId, workspaceId)}/ingestion/jobs?${q.toString()}` as ApiPath, parseJobsPage, { signal });
}

export function isActive(state: IngestionState): boolean {
  return state === "queued" || state === "processing";
}
