/**
 * Memory API client (contract m07 §3). EVERY wire shape the memory UI relies on lives here.
 * Assumptions where the contract is open: list = `{items, next_cursor}` (same as chat threads); approve/reject return
 * the updated memory; DELETE returns 204; `evidence_status` (`ok|stale`) arrives on `GET /memory/{id}` (and on list items if present).
 */
import { ApiError, invalid, isRecord, jsonBody, oneOf, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";

export const MEMORY_STATES = ["suggested", "approved", "rejected", "expired", "invalidated"] as const;
export type MemoryState = (typeof MEMORY_STATES)[number];
export const MEMORY_KINDS = ["fact", "preference", "decision"] as const;
export type MemoryKind = (typeof MEMORY_KINDS)[number];
export const MAX_STATEMENT = 2000;

export type MemoryEvidence = {
  chunk_id: string;
  document_id: string;
  version_id: string;
  workspace_id: string | null;
  evidence_status: "ok" | "stale" | null;
};

export type Memory = {
  id: string;
  workspace_id: string | null;
  visibility: "private" | "workspace";
  kind: MemoryKind;
  statement: string;
  state: MemoryState;
  evidence: MemoryEvidence[];
  source: string;
  expires_at: string | null;
  invalidated_reason: string | null;
  created_at: string;
};

export type MemoryPage = { items: Memory[]; next_cursor: string | null };

function parseEvidence(v: unknown): MemoryEvidence {
  if (!isRecord(v)) invalid();
  const s = v.evidence_status;
  return {
    chunk_id: str(v, "chunk_id"),
    document_id: str(v, "document_id"),
    version_id: str(v, "version_id"),
    workspace_id: typeof v.workspace_id === "string" ? v.workspace_id : null,
    evidence_status: s === "ok" || s === "stale" ? s : null,
  };
}

export function parseMemory(v: unknown): Memory {
  if (!isRecord(v)) invalid();
  return {
    id: str(v, "id"),
    workspace_id: typeof v.workspace_id === "string" ? v.workspace_id : null,
    visibility: v.visibility === "workspace" ? "workspace" : "private",
    kind: oneOf(v, "kind", MEMORY_KINDS),
    statement: str(v, "statement"),
    state: oneOf(v, "state", MEMORY_STATES),
    evidence: Array.isArray(v.evidence) ? v.evidence.map(parseEvidence) : [],
    source: typeof v.source === "string" ? v.source : "user",
    expires_at: typeof v.expires_at === "string" ? v.expires_at : null,
    invalidated_reason: typeof v.invalidated_reason === "string" ? v.invalidated_reason : null,
    created_at: typeof v.created_at === "string" ? v.created_at : "",
  };
}

export function parseMemoryPage(v: unknown): MemoryPage {
  if (!isRecord(v) || !Array.isArray(v.items)) invalid();
  return { items: v.items.map(parseMemory), next_cursor: typeof v.next_cursor === "string" ? v.next_cursor : null };
}

const base = (orgId: string) => `/v1/orgs/${encodeURIComponent(orgId)}/memory`;
const one = (orgId: string, id: string) => `${base(orgId)}/${encodeURIComponent(id)}` as ApiPath;

export function listMemories(fetcher: Fetcher, orgId: string, opts: { state: MemoryState; after?: string | null; limit?: number }, signal?: AbortSignal) {
  const qs = new URLSearchParams({ state: opts.state, limit: String(opts.limit ?? 30) });
  if (opts.after) qs.set("after", opts.after);
  return requestJson(fetcher, `${base(orgId)}?${qs}` as ApiPath, parseMemoryPage, { signal });
}

export function getMemory(fetcher: Fetcher, orgId: string, id: string, signal?: AbortSignal) {
  return requestJson(fetcher, one(orgId, id), parseMemory, { signal });
}

export type SuggestionInput = {
  statement: string;
  kind: MemoryKind;
  workspace_id?: string | null;
  evidence: { chunk_id: string; document_id: string; version_id: string }[];
};

export function createSuggestion(fetcher: Fetcher, orgId: string, input: SuggestionInput) {
  const body: Record<string, unknown> = { statement: input.statement, kind: input.kind, evidence: input.evidence };
  if (input.workspace_id) body.workspace_id = input.workspace_id;
  return requestJson(fetcher, `${base(orgId)}/suggestions` as ApiPath, parseMemory, jsonBody(body));
}

export const approveMemory = (f: Fetcher, orgId: string, id: string) => requestJson(f, `${one(orgId, id)}/approve` as ApiPath, parseMemory, jsonBody({}));
export const rejectMemory = (f: Fetcher, orgId: string, id: string) => requestJson(f, `${one(orgId, id)}/reject` as ApiPath, parseMemory, jsonBody({}));

export async function deleteMemory(fetcher: Fetcher, orgId: string, id: string): Promise<void> {
  let res: Response;
  try {
    res = await fetcher(one(orgId, id), { method: "DELETE" });
  } catch {
    throw new ApiError(0, "network_error");
  }
  if (!res.ok) throw new ApiError(res.status, `http_${res.status}`);
}

export function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return "You don't have permission to do that.";
    if (err.status === 404) return "This memory no longer exists.";
    if (err.status === 422 || err.status === 400) return "The server rejected this memory (check the statement and its sources).";
    if (err.isUnavailable) return "The service is temporarily unavailable. Try again.";
  }
  return "Something went wrong. Try again.";
}

/** Evidence for a memory suggestion from an answer's citations (deduplicated by chunk). */
export function evidenceFromCitations(cs: readonly { chunk_id: string; document_id: string; version_id: string }[]): SuggestionInput["evidence"] {
  const seen = new Set<string>();
  const out: SuggestionInput["evidence"] = [];
  for (const c of cs) {
    if (seen.has(c.chunk_id)) continue;
    seen.add(c.chunk_id);
    out.push({ chunk_id: c.chunk_id, document_id: c.document_id, version_id: c.version_id });
  }
  return out;
}
