/**
 * Graph read API client + parsing (contract m05 §5). EVERY wire shape the explorer relies on lives here.
 * `GET /v1/orgs/{o}/workspaces/{w}/graph/{neighborhood,entities,claims/{id}}`.
 */
import { ApiError, invalid, isRecord, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";

export type GraphNode = { id: string; type: string; name: string; degree_visible: number };
export type GraphEdge = {
  id: string;
  predicate: string;
  subject_id: string;
  object_id: string;
  claim_id: string;
  confidence: number;
  evidence_count: number;
};
export type Neighborhood = { nodes: GraphNode[]; edges: GraphEdge[]; truncated: boolean; next_cursor: string | null };
export type EntityHit = { id: string; type: string; name: string };
export type EntityPage = { items: EntityHit[]; next_cursor: string | null };
export type ClaimEntity = { id: string; type: string; name: string };
export type ClaimEvidence = {
  chunk_id: string;
  document_id: string;
  version_id: string;
  title: string;
  page_start: number | null;
  page_end: number | null;
  excerpt: string;
};
export type ClaimDetail = {
  claim: { id: string; predicate: string; subject: ClaimEntity; object: ClaimEntity | null; object_text: string | null; confidence: number };
  evidence: ClaimEvidence[];
};

const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const nullableInt = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? Math.trunc(v) : null);
const cursorOf = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

function list<T>(v: unknown, item: (x: unknown) => T): T[] {
  if (!Array.isArray(v)) invalid();
  const out: T[] = [];
  for (const x of v) {
    try {
      out.push(item(x));
    } catch {
      // one malformed record never hides the others
    }
  }
  return out;
}

export function parseNode(v: unknown): GraphNode {
  if (!isRecord(v)) invalid();
  return { id: str(v, "id"), type: str(v, "type"), name: str(v, "name"), degree_visible: num(v.degree_visible) };
}

export function parseEdge(v: unknown): GraphEdge {
  if (!isRecord(v)) invalid();
  return {
    id: str(v, "id"),
    predicate: str(v, "predicate"),
    subject_id: str(v, "subject_id"),
    object_id: str(v, "object_id"),
    claim_id: str(v, "claim_id"),
    confidence: num(v.confidence),
    evidence_count: num(v.evidence_count),
  };
}

export function parseNeighborhood(v: unknown): Neighborhood {
  if (!isRecord(v)) invalid();
  return { nodes: list(v.nodes, parseNode), edges: list(v.edges, parseEdge), truncated: v.truncated === true, next_cursor: cursorOf(v.next_cursor) };
}

export function parseEntityPage(v: unknown): EntityPage {
  if (!isRecord(v)) invalid();
  return {
    items: list(v.items, (x) => {
      if (!isRecord(x)) invalid();
      return { id: str(x, "id"), type: str(x, "type"), name: str(x, "name") };
    }),
    next_cursor: cursorOf(v.next_cursor),
  };
}

function parseClaimEntity(v: unknown): ClaimEntity {
  if (!isRecord(v)) invalid();
  return { id: str(v, "id"), type: str(v, "type"), name: str(v, "name") };
}

export function parseClaimDetail(v: unknown): ClaimDetail {
  if (!isRecord(v) || !isRecord(v.claim)) invalid();
  const c = v.claim;
  return {
    claim: {
      id: str(c, "id"),
      predicate: str(c, "predicate"),
      subject: parseClaimEntity(c.subject),
      object: c.object == null ? null : parseClaimEntity(c.object),
      object_text: typeof c.object_text === "string" ? c.object_text : null,
      confidence: num(c.confidence),
    },
    evidence: list(v.evidence, (x) => {
      if (!isRecord(x)) invalid();
      return {
        chunk_id: str(x, "chunk_id"),
        document_id: str(x, "document_id"),
        version_id: str(x, "version_id"),
        title: typeof x.title === "string" ? x.title : "",
        page_start: nullableInt(x.page_start),
        page_end: nullableInt(x.page_end),
        excerpt: typeof x.excerpt === "string" ? x.excerpt : "",
      };
    }),
  };
}

const base = (orgId: string, workspaceId: string) => `/v1/orgs/${encodeURIComponent(orgId)}/workspaces/${encodeURIComponent(workspaceId)}/graph`;

export function searchEntities(
  fetcher: Fetcher,
  ids: { orgId: string; workspaceId: string },
  q: { q: string; type?: string; limit?: number; cursor?: string | null },
  signal?: AbortSignal,
): Promise<EntityPage> {
  const sp = new URLSearchParams({ q: q.q, limit: String(q.limit ?? 25) });
  if (q.type) sp.set("type", q.type);
  if (q.cursor) sp.set("cursor", q.cursor);
  return requestJson(fetcher, `${base(ids.orgId, ids.workspaceId)}/entities?${sp}` as ApiPath, parseEntityPage, { signal });
}

export function getNeighborhood(
  fetcher: Fetcher,
  ids: { orgId: string; workspaceId: string },
  q: { entityId: string; depth?: number; limit?: number; cursor?: string | null },
  signal?: AbortSignal,
): Promise<Neighborhood> {
  const sp = new URLSearchParams({ entity_id: q.entityId, depth: String(q.depth ?? 1), limit: String(q.limit ?? 50) });
  if (q.cursor) sp.set("cursor", q.cursor);
  return requestJson(fetcher, `${base(ids.orgId, ids.workspaceId)}/neighborhood?${sp}` as ApiPath, parseNeighborhood, { signal });
}

export function getClaim(fetcher: Fetcher, ids: { orgId: string; workspaceId: string }, claimId: string, signal?: AbortSignal): Promise<ClaimDetail> {
  return requestJson(fetcher, `${base(ids.orgId, ids.workspaceId)}/claims/${encodeURIComponent(claimId)}` as ApiPath, parseClaimDetail, { signal });
}

export type GraphErrorCode = "not_found" | "unavailable" | "unauthenticated" | "invalid" | "unknown";

/** 404 (concealed not-found / not published) and 503 (`graph_unavailable`) get calm, separate states. */
export function toGraphErrorCode(err: unknown): GraphErrorCode {
  if (!(err instanceof ApiError)) return "unknown";
  if (err.isUnauthenticated) return "unauthenticated";
  if (err.status === 404 || err.status === 403) return "not_found";
  if (err.isUnavailable) return "unavailable";
  if (err.status === 422 || err.code === "invalid_response") return "invalid";
  return "unknown";
}
