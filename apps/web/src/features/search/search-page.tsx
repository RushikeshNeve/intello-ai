"use client";

import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useApiFetcher } from "@/lib/query/provider";
import { useWorkspaces } from "@/features/workspaces/hooks";
import type { Workspace } from "@/features/workspaces/api";
import { MAX_QUERY_LENGTH, SEARCH_MODES, degradedMessage, search, toSearchErrorCode, type SearchErrorCode, type SearchMode } from "./api";
import { SearchResultCard } from "./search-results";
import {
  ALL_SCOPE,
  MODE_LABELS,
  TYPE_FILTERS,
  loadScope,
  parseSearchState,
  requestKey,
  saveScope,
  toSearchParams,
  toSearchRequest,
  type SearchState,
  type TypeFilter,
} from "./url-state";

export const SEARCH_DEBOUNCE_MS = 300;

const ERROR_COPY: Record<SearchErrorCode, string> = {
  rate_limited: "You're searching very quickly. Wait a few seconds and try again.",
  unavailable: "Search is temporarily unavailable. Your documents are safe; try again in a moment.",
  invalid: "That search couldn't be run. Try different words.",
  not_found: "This organization isn't available to you.",
  network: "We couldn't reach the server. Check your connection and try again.",
  unauthenticated: "Your session has ended. Please sign in again.",
  unknown: "Something went wrong while searching. Try again.",
};

export const searchKey = (orgId: string, key: string) => ["org", orgId, "search", key] as const;

export type SearchPageProps = {
  orgId: string;
  orgSlug: string;
  initialWorkspaces: Workspace[];
};

export function SearchPage({ orgId, orgSlug, initialWorkspaces }: SearchPageProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const fetcher = useApiFetcher();
  const { data: workspaces = initialWorkspaces } = useWorkspaces(orgId, initialWorkspaces);

  // The URL is the single source of truth.
  const urlSearch = searchParams.toString();
  const state = useMemo(() => parseSearchState(new URLSearchParams(urlSearch)), [urlSearch]);
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  });

  const go = (next: SearchState, mode: "push" | "replace") => {
    const qs = toSearchParams(next).toString();
    router[mode](qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };

  // --- scope: the slug in the URL names a workspace; unknown slugs mean "all" (with a note) ---
  const scopeWorkspace = state.ws ? workspaces.find((w) => w.slug === state.ws) : undefined;
  const unknownScope = state.ws !== null && scopeWorkspace === undefined;
  const workspaceId = scopeWorkspace?.id;

  // Remembered scope: applied once, only when the URL itself does not say.
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    if (stateRef.current.ws !== null) return;
    const remembered = loadScope(orgId);
    if (remembered && remembered !== ALL_SCOPE && workspaces.some((w) => w.slug === remembered)) {
      go({ ...stateRef.current, ws: remembered }, "replace");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs once on mount
  }, []);

  // --- query box: local text, debounced into the URL (and re-synced when the URL changes underneath) ---
  const [qInput, setQInput] = useState(state.q);
  const [syncedQ, setSyncedQ] = useState(state.q);
  if (state.q !== syncedQ) {
    setSyncedQ(state.q);
    setQInput(state.q);
  }
  useEffect(() => {
    const wanted = qInput.trim().slice(0, MAX_QUERY_LENGTH);
    if (wanted === stateRef.current.q) return;
    const t = setTimeout(() => go({ ...stateRef.current, q: wanted }, "replace"), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `go` only closes over router/pathname
  }, [qInput]);

  // --- data: react-query aborts the in-flight request when the key changes or the page unmounts ---
  const request = toSearchRequest(state, workspaceId);
  const key = requestKey(state, workspaceId);
  const query = useQuery({
    queryKey: searchKey(orgId, key),
    enabled: request !== null,
    retry: false,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) => search(fetcher, orgId, request!, signal),
  });
  const data = request ? query.data : undefined;
  const errorCode = request && query.isError ? toSearchErrorCode(query.error) : null;
  const loading = request !== null && query.isFetching && !query.isError;
  const workspaceName = (id: string, slug: string) => workspaces.find((w) => w.id === id)?.name ?? slug;

  const resultCount = data?.results.length ?? 0;
  const snippetCount = data?.results.reduce((n, r) => n + r.snippets.length, 0) ?? 0;
  const announcement = !request
    ? ""
    : loading
      ? "Searching…"
      : errorCode
        ? "Search failed."
        : data
          ? resultCount === 0
            ? "No results."
            : `${resultCount} ${resultCount === 1 ? "document" : "documents"}, ${snippetCount} ${snippetCount === 1 ? "passage" : "passages"}.`
          : "";

  function submit(e: FormEvent) {
    e.preventDefault();
    go({ ...stateRef.current, q: qInput.trim().slice(0, MAX_QUERY_LENGTH) }, "replace");
  }

  function setScope(slug: string) {
    const ws = slug === ALL_SCOPE ? null : slug;
    saveScope(orgId, ws);
    go({ ...state, ws }, "push");
  }

  const degraded = data ? degradedMessage(data.degraded) : null;
  const searchedAnywhere = data?.workspace_count !== 0;

  return (
    <div className="mx-auto grid max-w-4xl gap-5" data-testid="search-page">
      <div className="grid gap-1">
        <h1 tabIndex={-1} className="text-2xl font-semibold tracking-tight outline-none">
          Search
        </h1>
        <p className="text-sm text-muted-foreground">Find passages across the documents you can read.</p>
      </div>

      <form role="search" onSubmit={submit} className="grid gap-3" data-testid="search-form">
        <div className="grid gap-1.5">
          <Label htmlFor="search-q">Search your documents</Label>
          <div className="flex gap-2">
            <Input
              id="search-q"
              type="search"
              value={qInput}
              maxLength={MAX_QUERY_LENGTH}
              autoComplete="off"
              autoFocus
              placeholder="Ask about a topic, a name, a phrase…"
              onChange={(e) => setQInput(e.target.value)}
              className="flex-1"
            />
            <Button type="submit">Search</Button>
          </div>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-1">
            <Label htmlFor="search-scope">Where</Label>
            <Select id="search-scope" value={state.ws ?? ALL_SCOPE} onChange={(e) => setScope(e.target.value)}>
              <option value={ALL_SCOPE}>All my workspaces</option>
              {workspaces.map((w) => (
                <option key={w.id} value={w.slug}>
                  {w.name}
                </option>
              ))}
              {unknownScope && state.ws && <option value={state.ws}>{state.ws} (not available)</option>}
            </Select>
          </div>
          <div className="grid gap-1">
            <Label htmlFor="search-type">File type</Label>
            <Select id="search-type" value={state.type} onChange={(e) => go({ ...state, type: e.target.value as TypeFilter }, "push")}>
              {TYPE_FILTERS.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="grid gap-1">
            <Label htmlFor="search-mode">Match by</Label>
            <Select id="search-mode" value={state.mode} onChange={(e) => go({ ...state, mode: e.target.value as SearchMode }, "push")}>
              {SEARCH_MODES.map((m) => (
                <option key={m} value={m}>
                  {MODE_LABELS[m]}
                </option>
              ))}
            </Select>
          </div>
        </div>
      </form>

      {unknownScope && (
        <p role="note" className="text-sm text-muted-foreground" data-testid="scope-unknown">
          That workspace isn&apos;t available to you, so every workspace you can read is searched instead.
        </p>
      )}

      <p role="status" aria-live="polite" className="min-h-5 text-sm text-muted-foreground" data-testid="search-live">
        {announcement}
      </p>

      <section aria-label="Search results" aria-busy={loading} className="grid gap-3" data-testid="search-results">
        {!request ? (
          <p className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground" data-testid="search-idle">
            Type a few words to search. Results stay with this page&apos;s link, so you can share or bookmark a search.
          </p>
        ) : errorCode ? (
          <div role="alert" className="grid justify-items-start gap-3 rounded-lg border border-border p-4" data-testid="search-error" data-code={errorCode}>
            <p className="text-sm">{ERROR_COPY[errorCode]}</p>
            {errorCode !== "invalid" && errorCode !== "not_found" && (
              <Button variant="outline" onClick={() => void query.refetch()}>
                Try again
              </Button>
            )}
          </div>
        ) : !data ? (
          <div role="status" aria-label="Searching" className="grid gap-3" data-testid="search-loading">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : (
          <>
            {degraded && (
              <p role="note" className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm" data-testid="search-degraded">
                {degraded}
              </p>
            )}
            {data.results.length === 0 ? (
              <div className="grid gap-1 rounded-lg border border-dashed border-border p-6 text-sm" data-testid="search-empty">
                <p className="font-medium">{searchedAnywhere ? "No matching passages" : "Nothing to search yet"}</p>
                <p className="text-muted-foreground">
                  {searchedAnywhere
                    ? "Try different words, a broader scope, or a different file type."
                    : "You don't have access to any workspaces with documents yet."}
                </p>
              </div>
            ) : (
              <ol className={loading ? "grid gap-3 opacity-60" : "grid gap-3"} data-testid="search-list">
                {data.results.map((r) => (
                  <SearchResultCard key={`${r.document_id}:${r.version_id}`} result={r} orgSlug={orgSlug} workspaceName={workspaceName(r.workspace_id, r.workspace_slug)} />
                ))}
              </ol>
            )}
          </>
        )}
      </section>
    </div>
  );
}
