/**
 * Search API client + parsing (contract m04 §3). EVERY wire shape the search UI relies on lives in this module.
 * `POST /v1/orgs/{o}/search`.
 */
import { ApiError, invalid, isRecord, jsonBody, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";

export const SEARCH_MODES = ["hybrid", "keyword", "vector"] as const;
export type SearchMode = (typeof SEARCH_MODES)[number];
export const MAX_QUERY_LENGTH = 500;

export type SearchRequest = {
  query: string;
  workspace_ids?: string[];
  content_types?: string[];
  mode?: SearchMode;
  limit?: number;
};

export type SearchSnippet = {
  /** Per-request evidence label (`e1`...). */
  evidence_id: string | null;
  /**
   * The chunk id the snippet came from. NOT in the frozen contract (§3 lists only `evidence_id`); it is read when
   * the API sends it (or when `evidence_id` is itself an id) and is what lets a snippet open the exact passage.
   */
  chunk_id: string | null;
  text: string;
  page_start: number | null;
  page_end: number | null;
  /** `[start, end)` code-point offsets into `text`, as sent (sanitised, not yet merged). */
  highlights: [number, number][];
};

export type SearchResult = {
  document_id: string;
  title: string;
  workspace_id: string;
  workspace_slug: string;
  content_type: string;
  version_id: string;
  version_number: number;
  score: number;
  snippets: SearchSnippet[];
};

export type SearchResponse = {
  query: string;
  results: SearchResult[];
  degraded: string[];
  took_ms: number | null;
  workspace_count: number | null;
};

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function nullableInt(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? Math.trunc(v) : null;
}

export function parseHighlights(v: unknown): [number, number][] {
  if (!Array.isArray(v)) return [];
  const out: [number, number][] = [];
  for (const pair of v) {
    if (!Array.isArray(pair) || pair.length < 2) continue;
    const [a, b] = pair;
    if (typeof a === "number" && typeof b === "number" && Number.isFinite(a) && Number.isFinite(b)) out.push([Math.trunc(a), Math.trunc(b)]);
  }
  return out;
}

export function parseSnippet(v: unknown): SearchSnippet {
  if (!isRecord(v)) invalid();
  const evidenceId = typeof v.evidence_id === "string" ? v.evidence_id : null;
  const chunk =
    typeof v.chunk_id === "string" && v.chunk_id ? v.chunk_id : evidenceId !== null && UUID_RE.test(evidenceId) ? evidenceId : null;
  return {
    evidence_id: evidenceId,
    chunk_id: chunk,
    text: str(v, "text"),
    page_start: nullableInt(v.page_start),
    page_end: nullableInt(v.page_end),
    highlights: parseHighlights(v.highlights),
  };
}

export function parseResult(v: unknown): SearchResult {
  if (!isRecord(v)) invalid();
  const snippets: SearchSnippet[] = [];
  if (Array.isArray(v.snippets)) {
    for (const s of v.snippets) {
      try {
        snippets.push(parseSnippet(s));
      } catch {
        // a malformed snippet is dropped, never fatal
      }
    }
  }
  const version = v.version_number;
  const score = v.score;
  return {
    document_id: str(v, "document_id"),
    title: str(v, "title"),
    workspace_id: str(v, "workspace_id"),
    workspace_slug: str(v, "workspace_slug"),
    content_type: typeof v.content_type === "string" ? v.content_type : "",
    version_id: str(v, "version_id"),
    version_number: typeof version === "number" && Number.isFinite(version) ? version : 1,
    score: typeof score === "number" && Number.isFinite(score) ? score : 0,
    snippets,
  };
}

export function parseSearchResponse(v: unknown): SearchResponse {
  if (!isRecord(v) || !Array.isArray(v.results)) invalid();
  const results: SearchResult[] = [];
  for (const r of v.results) {
    try {
      results.push(parseResult(r));
    } catch {
      // one malformed hit never hides the others
    }
  }
  const scope = isRecord(v.scope) ? v.scope : {};
  return {
    query: typeof v.query === "string" ? v.query : "",
    results,
    degraded: Array.isArray(v.degraded) ? v.degraded.filter((d): d is string => typeof d === "string") : [],
    took_ms: nullableInt(v.took_ms),
    workspace_count: nullableInt(scope.workspace_count),
  };
}

export const searchPath = (orgId: string): ApiPath => `/v1/orgs/${encodeURIComponent(orgId)}/search`;

export function search(fetcher: Fetcher, orgId: string, request: SearchRequest, signal?: AbortSignal): Promise<SearchResponse> {
  return requestJson(fetcher, searchPath(orgId), parseSearchResponse, { ...jsonBody(request), signal });
}

/** The small set of failures the UI words differently. */
export type SearchErrorCode = "rate_limited" | "unavailable" | "invalid" | "not_found" | "network" | "unauthenticated" | "unknown";

export function toSearchErrorCode(err: unknown): SearchErrorCode {
  if (!(err instanceof ApiError)) return "unknown";
  if (err.status === 401) return "unauthenticated";
  if (err.status === 0 || err.code === "network_error") return "network";
  if (err.status === 429 || err.code === "rate_limited") return "rate_limited";
  if (err.status === 422 || err.code === "invalid_request") return "invalid";
  if (err.status === 404 || err.status === 403) return "not_found";
  if (err.status === 502 || err.status === 503 || err.status === 504 || err.code === "search_unavailable") return "unavailable";
  return "unknown";
}

/** Server `degraded` codes, in words. Unknown codes get a generic note rather than being hidden. */
export function degradedMessage(codes: readonly string[]): string | null {
  if (codes.length === 0) return null;
  if (codes.every((c) => c === "embedding_unavailable")) {
    return "Showing keyword matches only, because the embedding model isn't available right now. Meaning-based results will return when it is.";
  }
  return "Some search features are unavailable right now, so these results may be less complete.";
}
