"use client";

import { useCallback, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeftIcon, ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useApiFetcher } from "@/lib/query/provider";
import type { Version } from "@/features/documents/types";
import { Preview } from "@/features/documents/viewer/preview";
import { getChunk, toEvidenceErrorCode } from "./api";
import { ChunkBody, ERROR_COPY, pageLabel } from "./chunk-body";
import { HighlightedText } from "./highlighted-text";
import { documentHref, evidenceHref } from "./links";

export type EvidenceViewerProps = {
  orgId: string;
  orgSlug: string;
  workspaceId: string;
  /** Slug (or id) used in links. */
  workspaceRef: string;
  document: { id: string; title: string };
  /** The version the evidence belongs to. */
  version: Version;
  /** Number and id of the document's newest version. */
  latest: { id: string; version_number: number } | null;
  chunkId: string | null;
  page: number | null;
};

export const evidenceKey = (orgId: string, chunkId: string) => ["org", orgId, "evidence", chunkId] as const;

/**
 * Split view for one piece of evidence. Left: the chunk text (from the artifact store, via the API) with
 * previous/next navigation. Right: the document at that place: a PDF at `#page=n`, or the text with the excerpt
 * marked. The URL (`v`, `chunk`, `page`) is the state, so every view is shareable.
 */
export function EvidenceViewer({ orgId, orgSlug, workspaceId, workspaceRef, document: doc, version, latest, chunkId, page }: EvidenceViewerProps) {
  const router = useRouter();
  const fetcher = useApiFetcher();
  const [excerptFound, setExcerptFound] = useState<boolean | null>(null);
  const [goneVersion, setGoneVersion] = useState<string | null>(null);
  const onGone = useCallback(() => setGoneVersion(version.id), [version.id]);

  const query = useQuery({
    queryKey: chunkId ? evidenceKey(orgId, chunkId) : ["org", orgId, "evidence", "none"],
    enabled: chunkId !== null,
    retry: false,
    staleTime: 60_000,
    queryFn: ({ signal }) => getChunk(fetcher, { orgId, workspaceId, documentId: doc.id, versionId: version.id, chunkId: chunkId! }, signal),
  });
  const chunk = query.data ?? null;
  const errorCode = query.isError ? toEvidenceErrorCode(query.error) : null;

  const here = (over: { chunkId?: string | null }) =>
    evidenceHref({ orgSlug, workspaceRef, documentId: doc.id, versionId: version.id, chunkId: over.chunkId ?? null, page: null });
  const go = (target: string | null) => {
    if (!target) return;
    setExcerptFound(null);
    router.push(here({ chunkId: target }));
  };

  const documentPath = documentHref(orgSlug, workspaceRef, doc.id, version.id);
  const newer = latest && latest.id !== version.id && latest.version_number > version.version_number ? latest : null;
  const pages = chunk ? pageLabel(chunk.page_start, chunk.page_end) : page ? `page ${page}` : null;
  const pdfPage = chunk?.page_start ?? page;
  const unavailable = goneVersion === version.id;

  const ids = { fetcher, orgId, workspaceId, documentId: doc.id };
  const renderText = useCallback(
    (text: string) => (chunk ? <HighlightedText text={text} excerpt={chunk.text} onResolved={setExcerptFound} /> : text),
    [chunk],
  );

  return (
    <div className="mx-auto grid max-w-7xl gap-4" data-testid="evidence-viewer" data-chunk-id={chunkId ?? ""}>
      <div className="grid gap-2">
        <Link href={documentPath} className="inline-flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeftIcon aria-hidden="true" className="size-3.5" />
          Open the whole document
        </Link>
        <h1 tabIndex={-1} className="text-2xl font-semibold tracking-tight break-words outline-none" data-testid="evidence-title">
          {doc.title}
        </h1>
        <p className="text-sm text-muted-foreground" data-testid="evidence-meta">
          Version {version.version_number}
          {pages ? ` · ${pages}` : ""}
        </p>
        {newer && (
          <p role="note" className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm" data-testid="newer-version-note">
            This passage is from version {version.version_number}. A newer version (version {newer.version_number}) of this document exists.{" "}
            <Link href={documentHref(orgSlug, workspaceRef, doc.id, newer.id)} className="underline underline-offset-2">
              Open the newest version
            </Link>
          </p>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <section aria-label="Source passage" className="grid min-w-0 content-start gap-3" data-testid="evidence-passage">
          {chunkId === null ? (
            <p className="rounded-lg border border-dashed border-border p-4 text-sm text-muted-foreground" data-testid="evidence-no-chunk">
              {page ? `Showing the document at page ${page}.` : "Showing the document."}
            </p>
          ) : query.isPending ? (
            <div role="status" aria-label="Loading passage" data-testid="evidence-loading">
              <Skeleton className="h-40 w-full" />
            </div>
          ) : errorCode ? (
            <div role="alert" className="grid justify-items-start gap-3 rounded-lg border border-dashed border-border p-5" data-testid="evidence-error" data-code={errorCode}>
              <h2 className="text-lg font-semibold tracking-tight">{ERROR_COPY[errorCode].title}</h2>
              <p className="max-w-md text-sm text-muted-foreground">{ERROR_COPY[errorCode].body}</p>
              {ERROR_COPY[errorCode].retry && (
                <Button variant="outline" onClick={() => void query.refetch()}>
                  Try again
                </Button>
              )}
            </div>
          ) : chunk ? (
            <>
              <div className="flex items-center justify-between gap-2">
                <Button variant="outline" size="sm" disabled={!chunk.prev_chunk_id} onClick={() => go(chunk.prev_chunk_id)} data-testid="evidence-prev">
                  <ChevronLeftIcon aria-hidden="true" />
                  Previous passage
                </Button>
                <Button variant="outline" size="sm" disabled={!chunk.next_chunk_id} onClick={() => go(chunk.next_chunk_id)} data-testid="evidence-next">
                  Next passage
                  <ChevronRightIcon aria-hidden="true" />
                </Button>
              </div>
              <ChunkBody chunk={chunk} />
            </>
          ) : null}
        </section>

        <section aria-label="Document" className="grid min-w-0 content-start gap-2" data-testid="evidence-document">
          {unavailable ? (
            <div className="grid justify-items-start gap-2 rounded-lg border border-dashed border-border p-5" data-testid="evidence-doc-unavailable">
              <h2 className="text-lg font-semibold tracking-tight">This version of the document isn&apos;t available</h2>
              <Link href={documentPath} className={buttonVariants({ variant: "outline" })}>
                Open the document
              </Link>
            </div>
          ) : (
            <>
              <Preview key={version.id} ids={ids} version={version} title={doc.title} onGone={onGone} pdfPage={pdfPage} renderText={renderText} />
              {chunk && excerptFound === false && (
                <p role="status" className="text-xs text-muted-foreground" data-testid="excerpt-status">
                  We couldn&apos;t find this passage in the file text, so it is shown only on the left.
                </p>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
