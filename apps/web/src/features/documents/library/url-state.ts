/**
 * The library's view state lives ONLY in the URL query string (`q`, `type`, `sort`, `order`, `cursor`, plus
 * `prev`, the stack of earlier page cursors that makes "Previous" possible with keyset pagination).
 * This module is the one place that maps URL <-> state <-> API parameters. It is pure.
 */
import { ORDERS, SORTS, type ListDocumentsParams, type Order, type Sort } from "../types";

export const PAGE_SIZE = 25;
export const MAX_QUERY_LENGTH = 200;

export const TYPE_FILTERS = [
  { value: "all", label: "All types", contentType: undefined },
  { value: "pdf", label: "PDF", contentType: "application/pdf" },
  { value: "text", label: "Text", contentType: "text/" },
  { value: "image", label: "Images", contentType: "image/" },
  // Prefix of the OOXML spreadsheet type; the API filters by an exact type or a prefix only.
  { value: "spreadsheet", label: "Spreadsheets", contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml." },
  // The generic fallback type: the API cannot express "everything else" as a single prefix.
  { value: "other", label: "Other", contentType: "application/octet-stream" },
] as const;
export type TypeFilter = (typeof TYPE_FILTERS)[number]["value"];
const TYPE_VALUES: readonly string[] = TYPE_FILTERS.map((t) => t.value);

export const SORT_LABELS: Record<Sort, string> = { updated_at: "Last updated", created_at: "Created", title: "Title" };

export type LibraryState = {
  q: string;
  type: TypeFilter;
  sort: Sort;
  order: Order;
  /** Cursor of the CURRENT page (null = first page). */
  cursor: string | null;
  /** Cursors of the pages before the current one, oldest first, excluding the first page. */
  prev: string[];
};

export const DEFAULT_SORT: Sort = "updated_at";
export const defaultOrder = (sort: Sort): Order => (sort === "title" ? "asc" : "desc");

type ParamSource = { get(name: string): string | null };

const CURSOR_RE = /^[A-Za-z0-9_\-=.+/]{1,512}$/;
const isCursor = (v: string | null | undefined): v is string => typeof v === "string" && CURSOR_RE.test(v);

export function parseLibraryState(sp: ParamSource): LibraryState {
  const q = (sp.get("q") ?? "").trim().slice(0, MAX_QUERY_LENGTH);
  const rawType = sp.get("type");
  const type = (rawType && TYPE_VALUES.includes(rawType) ? rawType : "all") as TypeFilter;
  const rawSort = sp.get("sort");
  const sort = (rawSort && (SORTS as readonly string[]).includes(rawSort) ? rawSort : DEFAULT_SORT) as Sort;
  const rawOrder = sp.get("order");
  const order = (rawOrder && (ORDERS as readonly string[]).includes(rawOrder) ? rawOrder : defaultOrder(sort)) as Order;
  const rawCursor = sp.get("cursor");
  const cursor = isCursor(rawCursor) ? rawCursor : null;
  const prev = cursor
    ? (sp.get("prev") ?? "")
        .split(",")
        .filter(isCursor)
    : [];
  return { q, type, sort, order, cursor, prev };
}

export const DEFAULT_STATE: LibraryState = {
  q: "",
  type: "all",
  sort: DEFAULT_SORT,
  order: defaultOrder(DEFAULT_SORT),
  cursor: null,
  prev: [],
};

/** Canonical query string: defaults are omitted, so equal views have equal URLs. */
export function toSearchParams(state: LibraryState): URLSearchParams {
  const sp = new URLSearchParams();
  if (state.q) sp.set("q", state.q);
  if (state.type !== "all") sp.set("type", state.type);
  if (state.sort !== DEFAULT_SORT) sp.set("sort", state.sort);
  if (state.order !== defaultOrder(state.sort)) sp.set("order", state.order);
  if (state.cursor) {
    sp.set("cursor", state.cursor);
    if (state.prev.length) sp.set("prev", state.prev.join(","));
  }
  return sp;
}

export function toApiParams(state: LibraryState, limit = PAGE_SIZE): ListDocumentsParams {
  const params: ListDocumentsParams = { sort: state.sort, order: state.order, limit };
  if (state.q) params.q = state.q;
  const contentType = TYPE_FILTERS.find((t) => t.value === state.type)?.contentType;
  if (contentType) params.content_type = contentType;
  if (state.cursor) params.cursor = state.cursor;
  return params;
}

/** Stable cache key for one API view. */
export function paramsKey(state: LibraryState): string {
  return toSearchParams({ ...state, prev: [] }).toString() || "default";
}

/** Changing q/type/sort/order always returns to the first page. */
export function withFilter(state: LibraryState, patch: Partial<Pick<LibraryState, "q" | "type" | "sort" | "order">>): LibraryState {
  const next = { ...state, ...patch, cursor: null, prev: [] };
  if (patch.sort && patch.sort !== state.sort && patch.order === undefined) next.order = defaultOrder(patch.sort);
  return next;
}

export function nextPage(state: LibraryState, nextCursor: string): LibraryState {
  return { ...state, cursor: nextCursor, prev: state.cursor ? [...state.prev, state.cursor] : [] };
}

export function previousPage(state: LibraryState): LibraryState {
  if (!state.cursor) return state;
  const prev = state.prev.slice(0, -1);
  const cursor = state.prev.at(-1) ?? null;
  return { ...state, cursor, prev: cursor ? prev : [] };
}

export function hasFilters(state: LibraryState): boolean {
  return state.q !== "" || state.type !== "all";
}

export function clearFilters(state: LibraryState): LibraryState {
  return withFilter(state, { q: "", type: "all" });
}

/** Header click on a sortable column: same column toggles the order, a new column starts at its default. */
export function toggleSort(state: LibraryState, sort: Sort): LibraryState {
  if (state.sort === sort) return withFilter(state, { order: state.order === "asc" ? "desc" : "asc" });
  return withFilter(state, { sort, order: defaultOrder(sort) });
}
