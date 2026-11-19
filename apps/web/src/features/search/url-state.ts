/**
 * Search view state lives ONLY in the URL (`q`, `ws`, `type`, `mode`); this module is the one place that maps
 * URL <-> state <-> API request. `ws` is a workspace SLUG (stable and readable in a shared link); absent = every
 * workspace the caller may read. Pure.
 */
import { MAX_QUERY_LENGTH, SEARCH_MODES, type SearchMode, type SearchRequest } from "./api";

export const TYPE_FILTERS = [
  { value: "all", label: "All types", contentTypes: undefined },
  { value: "pdf", label: "PDF", contentTypes: ["application/pdf"] },
  { value: "text", label: "Plain text", contentTypes: ["text/plain"] },
  { value: "markdown", label: "Markdown", contentTypes: ["text/markdown"] },
  { value: "html", label: "HTML", contentTypes: ["text/html"] },
] as const;
export type TypeFilter = (typeof TYPE_FILTERS)[number]["value"];
const TYPE_VALUES: readonly string[] = TYPE_FILTERS.map((t) => t.value);

export const MODE_LABELS: Record<SearchMode, string> = {
  hybrid: "Best match (keywords + meaning)",
  keyword: "Keywords only",
  vector: "Meaning only",
};

export type SearchState = {
  q: string;
  /** Workspace slug, or null for every workspace. */
  ws: string | null;
  type: TypeFilter;
  mode: SearchMode;
};

export const DEFAULT_SEARCH_STATE: SearchState = { q: "", ws: null, type: "all", mode: "hybrid" };

type ParamSource = { get(name: string): string | null };

const SLUG_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

export function parseSearchState(sp: ParamSource): SearchState {
  const q = (sp.get("q") ?? "").trim().slice(0, MAX_QUERY_LENGTH);
  const ws = sp.get("ws");
  const type = sp.get("type");
  const mode = sp.get("mode");
  return {
    q,
    ws: ws && SLUG_RE.test(ws) ? ws : null,
    type: (type && TYPE_VALUES.includes(type) ? type : "all") as TypeFilter,
    mode: (mode && (SEARCH_MODES as readonly string[]).includes(mode) ? mode : "hybrid") as SearchMode,
  };
}

/** Canonical query string: defaults are omitted, so equal views have equal URLs. */
export function toSearchParams(state: SearchState): URLSearchParams {
  const sp = new URLSearchParams();
  if (state.q) sp.set("q", state.q);
  if (state.ws) sp.set("ws", state.ws);
  if (state.type !== "all") sp.set("type", state.type);
  if (state.mode !== "hybrid") sp.set("mode", state.mode);
  return sp;
}

/**
 * The API request for a state, or null when there is nothing to search. `workspaceId` is the id of `state.ws`
 * (resolved by the caller from the workspace list); an unknown slug is passed as `undefined` and means "all".
 */
export function toSearchRequest(state: SearchState, workspaceId: string | undefined, limit = 20): SearchRequest | null {
  if (!state.q) return null;
  const req: SearchRequest = { query: state.q, limit };
  if (workspaceId) req.workspace_ids = [workspaceId];
  const contentTypes = TYPE_FILTERS.find((t) => t.value === state.type)?.contentTypes;
  if (contentTypes) req.content_types = [...contentTypes];
  if (state.mode !== "hybrid") req.mode = state.mode;
  return req;
}

/** Stable cache key for one API request. */
export function requestKey(state: SearchState, workspaceId: string | undefined): string {
  return JSON.stringify([state.q, workspaceId ?? null, state.type, state.mode]);
}

// --- last scope per org (a convenience: every access is guarded) ---

const SCOPE_PREFIX = "ukos.search.scope.";
export const ALL_SCOPE = "all";

export function loadScope(orgId: string, storage: Pick<Storage, "getItem"> | undefined = safeStorage()): string | null {
  try {
    const v = storage?.getItem(SCOPE_PREFIX + orgId) ?? null;
    return v && (v === ALL_SCOPE || SLUG_RE.test(v)) ? v : null;
  } catch {
    return null;
  }
}

export function saveScope(orgId: string, scope: string | null, storage: Pick<Storage, "setItem"> | undefined = safeStorage()): void {
  try {
    storage?.setItem(SCOPE_PREFIX + orgId, scope ?? ALL_SCOPE);
  } catch {
    // private mode / blocked storage: the scope simply is not remembered
  }
}

function safeStorage(): Storage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}
