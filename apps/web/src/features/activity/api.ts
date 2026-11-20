/**
 * Activity & Jobs client.
 *
 * STUB CONTRACT (the backend route does not exist yet; a read-only route is approved for later):
 *   GET /v1/orgs/{o}/workspaces/{w}/ingestion/jobs?limit=&cursor=&state=
 *   -> { items: [{document_id, title, version_id, state, stage, error_code, message, updated_at}],
 *        counts: {queued, processing, ready, failed, unsupported, no_text, cancelled},
 *        next_cursor }
 * While the route answers 404 the page derives a best-effort list from the documents list (each document carries
 * `ingestion`), clearly labelled as such.
 */
import { invalid, isRecord, requestJson, ApiError, type ApiPath, type Fetcher } from "@/features/organizations/api/http";
import { listDocuments } from "@/features/documents/api";
import type { Document } from "@/features/documents/types";
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
  updated_at: string | null;
};
export type JobCounts = Record<JobState, number>;
export type JobsPage = { items: JobItem[]; counts: JobCounts; next_cursor: string | null };
export type ActivityData = { source: "api" | "derived"; page: JobsPage; truncated: boolean };

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
    updated_at: optStr(v.updated_at),
  };
}

export function parseJobsPage(body: unknown): JobsPage {
  if (!isRecord(body) || !Array.isArray(body.items)) invalid();
  const counts = emptyCounts();
  if (isRecord(body.counts)) for (const s of JOB_STATES) if (typeof body.counts[s] === "number") counts[s] = body.counts[s];
  return { items: body.items.map(parseJobItem), counts, next_cursor: optStr(body.next_cursor) };
}

/** Best-effort list from documents' `ingestion` summaries; documents without a processed version are left out. */
export function deriveFromDocuments(docs: readonly Document[], nextCursor: string | null = null): JobsPage {
  const counts = emptyCounts();
  const items: JobItem[] = [];
  for (const d of docs) {
    const i = d.ingestion;
    if (!i || !isJobState(i.state)) continue; // "none" / unknown
    counts[i.state] += 1;
    items.push({
      document_id: d.id,
      title: d.title,
      version_id: d.latest_version?.id ?? null,
      state: i.state,
      stage: i.stage,
      error_code: i.error_code,
      message: i.message,
      updated_at: i.updated_at ?? d.updated_at,
    });
  }
  items.sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
  return { items, counts, next_cursor: nextCursor };
}

export const activityKey = (orgId: string, workspaceId: string) => ["org", orgId, "workspace", workspaceId, "activity"] as const;

const wsBase = (o: string, w: string) => `/v1/orgs/${encodeURIComponent(o)}/workspaces/${encodeURIComponent(w)}`;

/** Jobs route first; a 404 (route missing) falls back to the documents list. Other errors propagate. */
export async function loadActivity(fetcher: Fetcher, orgId: string, workspaceId: string, signal?: AbortSignal): Promise<ActivityData> {
  try {
    const page = await requestJson(fetcher, `${wsBase(orgId, workspaceId)}/ingestion/jobs?limit=100` as ApiPath, parseJobsPage, { signal });
    return { source: "api", page, truncated: page.next_cursor !== null };
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 404) throw err;
  }
  const docs = await listDocuments(fetcher, orgId, workspaceId, { limit: 200, sort: "updated_at", order: "desc" });
  return { source: "derived", page: deriveFromDocuments(docs.items, docs.next_cursor), truncated: docs.next_cursor !== null };
}

export function isActive(state: IngestionState): boolean {
  return state === "queued" || state === "processing";
}
