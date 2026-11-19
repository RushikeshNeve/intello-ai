"use client";

import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { useApiFetcher } from "@/lib/query/provider";
import { DocumentUploader } from "@/features/documents/upload";
import { deleteDocument, listDocuments, renameDocument, toDocumentErrorCode } from "../api";
import { documentKeys } from "../keys";
import { SORTS, type Document, type DocumentList, type Order, type Sort } from "../types";
import { isTerminal } from "../ingestion";
import { DeleteDialog, RenameDialog } from "./document-dialogs";
import { DocumentsTable } from "./documents-table";
import {
  SORT_LABELS,
  TYPE_FILTERS,
  clearFilters,
  hasFilters,
  nextPage,
  paramsKey,
  parseLibraryState,
  previousPage,
  toApiParams,
  toggleSort,
  toSearchParams,
  withFilter,
  type LibraryState,
  type TypeFilter,
} from "./url-state";
export const SEARCH_DEBOUNCE_MS = 300;
/**
 * Live status in the library: no per-row streams. While any row on the page is queued/processing the page's own
 * list query is refetched every LIBRARY_POLL_MS (one request per interval whatever the page size; react-query
 * pauses it in hidden tabs and stops it when every row is terminal).
 */
export const LIBRARY_POLL_MS = 3_000;

export type LibraryProps = {
  orgId: string;
  workspaceId: string;
  /** `/o/{org}/w/{ws}/documents` */
  basePath: string;
  /** Server-rendered first view; used only while the URL still produces the same API request. */
  initial?: { key: string; data: DocumentList };
};

export function Library({ orgId, workspaceId, basePath, initial }: LibraryProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const fetcher = useApiFetcher();
  const queryClient = useQueryClient();

  // The URL is the single source of truth: every bit of view state is derived from it, on every render.
  const urlSearch = searchParams.toString();
  const state = useMemo(() => parseLibraryState(new URLSearchParams(urlSearch)), [urlSearch]);
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  });

  const [cursorNotice, setCursorNotice] = useState(false);

  function go(next: LibraryState, mode: "push" | "replace" = "push", keepNotice = false) {
    if (!keepNotice) setCursorNotice(false);
    const qs = toSearchParams(next).toString();
    const href = qs ? `${pathname}?${qs}` : pathname;
    router[mode](href, { scroll: false });
  }

  // --- search box: local text, debounced into the URL (and re-synced when the URL changes underneath) ---
  const [qInput, setQInput] = useState(state.q);
  const [syncedQ, setSyncedQ] = useState(state.q);
  if (state.q !== syncedQ) {
    setSyncedQ(state.q);
    setQInput(state.q);
  }
  useEffect(() => {
    const wanted = qInput.trim();
    if (wanted === stateRef.current.q) return;
    const t = setTimeout(() => go(withFilter(stateRef.current, { q: wanted }), "replace"), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `go` only closes over router/pathname
  }, [qInput]);

  // --- data ---
  const key = paramsKey(state);
  const query = useQuery({
    queryKey: documentKeys.list(orgId, workspaceId, key),
    queryFn: () => listDocuments(fetcher, orgId, workspaceId, toApiParams(state)),
    initialData: initial && initial.key === key ? initial.data : undefined,
    placeholderData: keepPreviousData,
    refetchInterval: (q) =>
      q.state.data?.items.some((d) => d.ingestion !== null && !isTerminal(d.ingestion.state)) ? LIBRARY_POLL_MS : false,
  });
  const data = query.data;
  const errorCode = query.isError ? toDocumentErrorCode(query.error) : null;

  // An invalid/stale cursor (e.g. from an old shared link) falls back to the first page, with a calm note.
  const cursorInvalid = errorCode === "invalid_cursor" && state.cursor !== null;
  if (cursorInvalid && !cursorNotice) setCursorNotice(true);
  const notice = cursorNotice ? "That page is no longer available, so we've gone back to the first page." : "";
  useEffect(() => {
    if (!cursorInvalid) return;
    go({ ...stateRef.current, cursor: null, prev: [] }, "replace", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursorInvalid]);

  const status = cursorInvalid || (query.isPending && !data) ? "loading" : query.isError && !data ? "error" : "ready";
  const refreshing = query.isFetching && !!data;

  // --- focus + announcements ---
  const resultsRef = useRef<HTMLHeadingElement>(null);
  const [message, setMessage] = useState("");

  function focusLinkOrResults(documentId: string | null) {
    setTimeout(() => {
      const link = documentId ? document.getElementById(`doc-link-${documentId}`) : null;
      (link ?? resultsRef.current)?.focus();
    }, 0);
  }

  // --- mutations ---
  const [renaming, setRenaming] = useState<Document | null>(null);
  const [deleting, setDeleting] = useState<Document | null>(null);

  async function doRename(doc: Document, title: string) {
    const updated = await renameDocument(fetcher, orgId, workspaceId, doc.id, title);
    await queryClient.invalidateQueries({ queryKey: documentKeys.all(orgId, workspaceId) });
    setRenaming(null);
    setMessage(`Renamed to “${updated.title}”.`);
    focusLinkOrResults(doc.id);
  }

  async function doDelete(doc: Document) {
    await deleteDocument(fetcher, orgId, workspaceId, doc.id);
    queryClient.setQueriesData<DocumentList>({ queryKey: documentKeys.all(orgId, workspaceId) }, (old) =>
      old ? { ...old, items: old.items.filter((d) => d.id !== doc.id) } : old,
    );
    void queryClient.invalidateQueries({ queryKey: documentKeys.all(orgId, workspaceId) });
    setDeleting(null);
    setMessage(`Deleted “${doc.title}”.`);
    focusLinkOrResults(null);
  }

  function onUploaded() {
    void queryClient.invalidateQueries({ queryKey: documentKeys.all(orgId, workspaceId) });
    router.refresh();
    // Make sure the new row is where the user will look: back to page 1 without filters.
    if (stateRef.current.cursor || hasFilters(stateRef.current)) go(clearFilters(stateRef.current), "replace");
  }

  // --- derived text ---
  const filtered = hasFilters(state);
  const page = state.cursor ? state.prev.length + 2 : 1;
  const count = data?.items.length ?? 0;
  const live =
    status === "loading" || refreshing
      ? "Loading documents…"
      : status === "error"
        ? "Documents could not be loaded."
        : count === 0
          ? filtered
            ? "No documents match your search or filters."
            : "No documents yet."
          : `${count} ${count === 1 ? "document" : "documents"}, page ${page}.`;

  return (
    <div className="grid gap-5">
      {data?.can_upload && (
        <section aria-label="Upload documents">
          <DocumentUploader orgId={orgId} workspaceId={workspaceId} onUploaded={onUploaded} />
        </section>
      )}

      <form
        role="search"
        aria-label="Filter documents"
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          if (qInput.trim() !== state.q) go(withFilter(state, { q: qInput.trim() }), "replace");
        }}
      >
        <div className="grid min-w-48 flex-1 gap-1.5">
          <Label htmlFor="doc-search">Search</Label>
          <Input
            id="doc-search"
            type="search"
            value={qInput}
            placeholder="Title or file name"
            autoComplete="off"
            onChange={(e) => setQInput(e.target.value)}
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="doc-type">File type</Label>
          <Select id="doc-type" value={state.type} onChange={(e) => go(withFilter(state, { type: e.target.value as TypeFilter }))}>
            {TYPE_FILTERS.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="doc-sort">Sort by</Label>
          <Select id="doc-sort" value={state.sort} onChange={(e) => go(withFilter(state, { sort: e.target.value as Sort }))}>
            {SORTS.map((s) => (
              <option key={s} value={s}>
                {SORT_LABELS[s]}
              </option>
            ))}
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="doc-order">Order</Label>
          <Select id="doc-order" value={state.order} onChange={(e) => go(withFilter(state, { order: e.target.value as Order }))}>
            <option value="asc">Ascending</option>
            <option value="desc">Descending</option>
          </Select>
        </div>
        {filtered && (
          <Button type="button" variant="ghost" onClick={() => go(clearFilters(state))}>
            Clear
          </Button>
        )}
      </form>

      {notice && (
        <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm" data-testid="cursor-notice">
          {notice}
        </p>
      )}

      <section aria-labelledby="doc-results-heading" aria-busy={status === "loading" || refreshing} className="grid gap-3">
        <h2 id="doc-results-heading" ref={resultsRef} tabIndex={-1} className="sr-only outline-none">
          Document results
        </h2>
        <p role="status" aria-live="polite" className="text-xs text-muted-foreground" data-testid="results-live">
          {live}
          <span className="sr-only"> {message}</span>
        </p>
        <DocumentsTable
          status={status}
          items={data?.items ?? []}
          sort={state.sort}
          order={state.order}
          onSort={(s) => go(toggleSort(state, s))}
          filtered={filtered}
          canUpload={!!data?.can_upload}
          onClear={() => go(clearFilters(state))}
          onRetry={() => void query.refetch()}
          basePath={basePath}
          onRename={setRenaming}
          onDelete={setDeleting}
        />
        <nav aria-label="Pagination" className="flex items-center justify-between gap-2">
          <Button variant="outline" disabled={!state.cursor} onClick={() => go(previousPage(state))}>
            Previous
          </Button>
          <span className="text-xs text-muted-foreground">Page {page}</span>
          <Button
            variant="outline"
            disabled={!data?.next_cursor}
            onClick={() => data?.next_cursor && go(nextPage(state, data.next_cursor))}
          >
            Next
          </Button>
        </nav>
      </section>

      {renaming && (
        <RenameDialog title={renaming.title} onClose={() => setRenaming(null)} onRename={(t) => doRename(renaming, t)} />
      )}
      {deleting && <DeleteDialog title={deleting.title} onClose={() => setDeleting(null)} onDelete={() => doDelete(deleting)} />}
    </div>
  );
}
