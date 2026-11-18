/**
 * Transport-agnostic JSON client for the UKOS REST API (contract §4).
 *
 * The same endpoint functions run in two places:
 * - Server Components pass `apiFetch` from `@/features/auth/server` (bearer token attached server-side).
 * - The browser passes `browserFetcher`, which calls the same-origin BFF at `/api/v1/*`.
 *
 * Errors are `{"detail": "<stable_code>"}` (contract §4); BFF errors use the same shape (DC-017).
 */
export type ApiPath = `/v1/${string}`;
export type Fetcher = (path: ApiPath, init?: RequestInit) => Promise<Response>;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    /** Stable code from `{"detail": ...}`, or a synthetic one (`network_error`, `invalid_response`, `http_<status>`). */
    readonly code: string,
  ) {
    super(`API ${status}: ${code}`);
    this.name = "ApiError";
  }

  get isUnauthenticated() {
    return this.status === 401;
  }

  /** 403 and 404 are deliberately indistinguishable to the user ("not found or no access"). */
  get isNotFoundOrForbidden() {
    return this.status === 404 || this.status === 403;
  }

  get isUnavailable() {
    return this.status === 0 || this.status === 502 || this.status === 503 || this.status === 504;
  }
}

async function errorFrom(res: Response): Promise<ApiError> {
  let code = `http_${res.status}`;
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string") code = detail;
      // FastAPI request-validation errors carry a list; keep a stable code for the UI.
      else if (Array.isArray(detail) && res.status === 422) code = "validation_error";
    }
  } catch {
    // non-JSON error body: keep the synthetic code
  }
  return new ApiError(res.status, code);
}

export async function requestJson<T>(
  fetcher: Fetcher,
  path: ApiPath,
  parse: (body: unknown) => T,
  init: RequestInit = {},
): Promise<T> {
  let res: Response;
  try {
    res = await fetcher(path, init);
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError(0, "network_error");
  }
  if (!res.ok) throw await errorFrom(res);
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    throw new ApiError(res.status, "invalid_response");
  }
  return parse(body);
}

export function jsonBody(value: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(value), headers: { "content-type": "application/json" } };
}

// --- shape guards (schema-first: reject responses that do not match the contract) ---

export function invalid(): never {
  throw new ApiError(200, "invalid_response");
}

export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function str(o: Record<string, unknown>, key: string): string {
  const v = o[key];
  if (typeof v !== "string") invalid();
  return v;
}

export function oneOf<const T extends readonly string[]>(o: Record<string, unknown>, key: string, values: T): T[number] {
  const v = o[key];
  if (typeof v !== "string" || !values.includes(v)) invalid();
  return v as T[number];
}

export type Page<T> = { items: T[]; next_cursor?: string | null };

export function parsePage<T>(body: unknown, item: (v: unknown) => T): Page<T> {
  if (!isRecord(body) || !Array.isArray(body.items)) invalid();
  const cursor = body.next_cursor;
  if (cursor !== undefined && cursor !== null && typeof cursor !== "string") invalid();
  return { items: body.items.map(item), next_cursor: cursor ?? null };
}

export const PAGE_LIMIT = 200;
const MAX_PAGES = 25;

/**
 * Reads every page of a list route (`?limit=&cursor=`). `next_cursor` is optional in practice: a response
 * without it is the last page.
 */
export async function listAll<T>(fetcher: Fetcher, path: ApiPath, item: (v: unknown) => T): Promise<T[]> {
  const out: T[] = [];
  let cursor: string | null | undefined;
  for (let i = 0; i < MAX_PAGES; i++) {
    const qs = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (cursor) qs.set("cursor", cursor);
    const page = await requestJson(fetcher, `${path}?${qs}` as ApiPath, (b) => parsePage(b, item));
    out.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) return out;
  }
  return out;
}

// --- browser transport ---

/** Login URL that returns the user to `returnTo` after sign-in (validated again server-side). */
export function loginUrl(returnTo: string): string {
  return `/api/auth/login?returnTo=${encodeURIComponent(returnTo)}`;
}

export type BrowserFetcherOptions = {
  fetchImpl?: typeof fetch;
  /** Called on a 401 (signed out / session ended). Defaults to a full-page navigation to the login route. */
  onUnauthenticated?: (loginHref: string) => void;
  currentPath?: () => string;
};

/** Same-origin BFF transport: `/v1/x` → `/api/v1/x`. The browser never holds a bearer token. */
export function createBrowserFetcher(opts: BrowserFetcherOptions = {}): Fetcher {
  const fetchImpl = opts.fetchImpl ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  const currentPath = opts.currentPath ?? (() => window.location.pathname + window.location.search);
  const onUnauthenticated = opts.onUnauthenticated ?? ((href: string) => window.location.assign(href));
  return async (path, init = {}) => {
    const headers = new Headers(init.headers);
    if (!headers.has("accept")) headers.set("accept", "application/json");
    const res = await fetchImpl(`/api${path}`, { ...init, headers, credentials: "same-origin", cache: "no-store" });
    if (res.status === 401) onUnauthenticated(loginUrl(currentPath()));
    return res;
  };
}
