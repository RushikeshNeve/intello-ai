/**
 * Evidence chunk API client (contract m04 §4). EVERY wire shape the evidence viewer relies on lives here.
 * `GET /v1/orgs/{o}/workspaces/{w}/documents/{d}/versions/{v}/chunks/{chunk}`.
 */
import { ApiError, invalid, isRecord, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";

export type EvidenceChunk = {
  chunk_id: string;
  text: string;
  page_start: number | null;
  page_end: number | null;
  heading_path: string[];
  version_number: number | null;
  title: string | null;
  prev_chunk_id: string | null;
  next_chunk_id: string | null;
};

function nullableInt(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? Math.trunc(v) : null;
}
function nullableId(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

export function parseChunk(v: unknown): EvidenceChunk {
  if (!isRecord(v)) invalid();
  const neighbors = isRecord(v.neighbors) ? v.neighbors : {};
  return {
    chunk_id: str(v, "chunk_id"),
    text: str(v, "text"),
    page_start: nullableInt(v.page_start),
    page_end: nullableInt(v.page_end),
    heading_path: Array.isArray(v.heading_path) ? v.heading_path.filter((h): h is string => typeof h === "string") : [],
    version_number: nullableInt(v.version_number),
    title: typeof v.title === "string" ? v.title : null,
    prev_chunk_id: nullableId(neighbors.prev_chunk_id),
    next_chunk_id: nullableId(neighbors.next_chunk_id),
  };
}

export function chunkPath(orgId: string, workspaceId: string, documentId: string, versionId: string, chunkId: string): ApiPath {
  const e = encodeURIComponent;
  return `/v1/orgs/${e(orgId)}/workspaces/${e(workspaceId)}/documents/${e(documentId)}/versions/${e(versionId)}/chunks/${e(chunkId)}`;
}

export function getChunk(
  fetcher: Fetcher,
  ids: { orgId: string; workspaceId: string; documentId: string; versionId: string; chunkId: string },
  signal?: AbortSignal,
): Promise<EvidenceChunk> {
  return requestJson(fetcher, chunkPath(ids.orgId, ids.workspaceId, ids.documentId, ids.versionId, ids.chunkId), parseChunk, { signal });
}

/** `gone`: 410 `evidence_unavailable` (the source text can no longer be read); `not_found`: 404/403 (concealed). */
export type EvidenceErrorCode = "not_found" | "gone" | "unavailable" | "network" | "unauthenticated" | "unknown";

export function toEvidenceErrorCode(err: unknown): EvidenceErrorCode {
  if (!(err instanceof ApiError)) return "unknown";
  if (err.status === 401) return "unauthenticated";
  if (err.status === 0 || err.code === "network_error") return "network";
  if (err.status === 410 || err.code === "evidence_unavailable") return "gone";
  if (err.status === 404 || err.status === 403) return "not_found";
  if (err.status === 502 || err.status === 503 || err.status === 504 || err.code === "storage_unavailable") return "unavailable";
  return "unknown";
}
