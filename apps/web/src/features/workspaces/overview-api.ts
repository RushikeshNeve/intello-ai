/** `GET /v1/orgs/{o}/workspaces/{w}/overview`: permission-filtered counts only (M09 backend gap 1). */
import { invalid, isRecord, requestJson, type ApiPath, type Fetcher } from "@/features/organizations/api/http";

const STATES = ["queued", "processing", "ready", "failed", "unsupported", "no_text", "cancelled"] as const;
export type DocState = (typeof STATES)[number];

export type WorkspaceOverview = {
  documents_total: number;
  documents: Record<DocState, number>;
  sources: number;
  pending_memories: number;
  research: { total: number; by_state: Record<string, number> };
  recent_chat_threads: number;
};

const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : invalid());

export function parseOverview(body: unknown): WorkspaceOverview {
  if (!isRecord(body) || !isRecord(body.documents) || !isRecord(body.research)) invalid();
  const documents = Object.fromEntries(STATES.map((s) => [s, num(body.documents && (body.documents as Record<string, unknown>)[s])])) as Record<DocState, number>;
  const by_state: Record<string, number> = {};
  if (isRecord(body.research.by_state)) for (const [k, v] of Object.entries(body.research.by_state)) by_state[k] = num(v);
  return {
    documents_total: num(body.documents_total),
    documents,
    sources: num(body.sources),
    pending_memories: num(body.pending_memories),
    research: { total: num(body.research.total), by_state },
    recent_chat_threads: num(body.recent_chat_threads),
  };
}

export const overviewKey = (orgId: string, workspaceId: string) => ["org", orgId, "workspace", workspaceId, "overview"] as const;

export function getOverview(fetcher: Fetcher, orgId: string, workspaceId: string, signal?: AbortSignal): Promise<WorkspaceOverview> {
  const path = `/v1/orgs/${encodeURIComponent(orgId)}/workspaces/${encodeURIComponent(workspaceId)}/overview` as ApiPath;
  return requestJson(fetcher, path, parseOverview, { signal });
}
