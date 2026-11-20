"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeftIcon, ChevronRightIcon, ExternalLinkIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/badges";
import { Skeleton } from "@/components/ui/skeleton";
import { useApiFetcher } from "@/lib/query/provider";
import { getDocument } from "@/features/documents/api";
import { getChunk, toEvidenceErrorCode } from "./api";
import { ChunkBody, ERROR_COPY, pageLabel } from "./chunk-body";
import { documentHref, evidenceHref } from "./links";

/** What the panel needs to open one passage; a chat citation maps onto this directly. */
export type EvidenceTarget = {
  workspaceId: string;
  workspaceRef: string;
  documentId: string;
  versionId: string;
  chunkId: string;
  title: string;
  versionNumber: number | null;
  page: number | null;
};

export type EvidencePanelProps = { orgId: string; orgSlug: string; target: EvidenceTarget | null };

const chunkKey = (orgId: string, chunkId: string) => ["org", orgId, "evidence", chunkId] as const;

/** The persistent evidence inspector: shows one cited passage with its source, version, location and neighbours. */
export function EvidencePanel({ orgId, orgSlug, target }: EvidencePanelProps) {
  return (
    <aside aria-label="Evidence" className="grid content-start gap-3 rounded-card border border-border bg-card p-4 shadow-card" data-testid="evidence-panel">
      <p className="text-meta font-semibold tracking-wide text-text-secondary uppercase">Evidence</p>
      {target ? (
        // Re-keyed per selected citation so previous/next navigation starts over for each pick.
        <PanelBody key={`${target.chunkId}:${target.versionId}`} orgId={orgId} orgSlug={orgSlug} target={target} />
      ) : (
        <p className="text-sm text-text-secondary" data-testid="evidence-panel-empty">
          Select a citation such as [1] in an answer to read the exact passage it came from.
        </p>
      )}
    </aside>
  );
}

function PanelBody({ orgId, orgSlug, target }: { orgId: string; orgSlug: string; target: EvidenceTarget }) {
  const fetcher = useApiFetcher();
  const [chunkId, setChunkId] = useState(target.chunkId);
  const query = useQuery({
    queryKey: chunkKey(orgId, chunkId),
    retry: false,
    staleTime: 60_000,
    queryFn: ({ signal }) =>
      getChunk(fetcher, { orgId, workspaceId: target.workspaceId, documentId: target.documentId, versionId: target.versionId, chunkId }, signal),
  });
  // Used only for the "newer version exists" note; a failure here just hides the note.
  const doc = useQuery({
    queryKey: ["org", orgId, "evidence-doc", target.documentId],
    retry: false,
    staleTime: 60_000,
    queryFn: () => getDocument(fetcher, orgId, target.workspaceId, target.documentId),
  });
  const chunk = query.data ?? null;
  const errorCode = query.isError ? toEvidenceErrorCode(query.error) : null;
  const latest = doc.data?.latest_version ?? null;
  const versionNumber = chunk?.version_number ?? target.versionNumber;
  const newer = latest && latest.id !== target.versionId && versionNumber !== null && latest.version_number > versionNumber ? latest : null;
  const title = chunk?.title || target.title || "Document";
  const pages = chunk ? pageLabel(chunk.page_start, chunk.page_end) : target.page ? `page ${target.page}` : null;

  return (
    <div className="grid min-w-0 gap-3" data-testid="evidence-panel-body" data-chunk-id={chunkId}>
      <div className="grid gap-1">
        <h2 className="text-card-title font-semibold break-words" data-testid="panel-title">
          {title}
        </h2>
        <p className="flex flex-wrap items-center gap-1.5 text-meta text-text-secondary" data-testid="panel-meta">
          {versionNumber !== null && <StatusBadge tone="info">Version {versionNumber}</StatusBadge>}
          {pages && <span>{pages}</span>}
        </p>
      </div>
      {newer && (
        <p role="note" className="rounded-control border border-border bg-warning-soft px-3 py-2 text-sm" data-testid="panel-newer-note">
          This passage is from version {versionNumber}. A newer version (version {newer.version_number}) exists.{" "}
          <Link href={documentHref(orgSlug, target.workspaceRef, target.documentId, newer.id)} className="underline underline-offset-2">
            Open the newest version
          </Link>
        </p>
      )}
      {query.isPending ? (
        <div role="status" aria-label="Loading passage" data-testid="panel-loading">
          <Skeleton className="h-32 w-full" />
        </div>
      ) : errorCode ? (
        <div role="alert" className="grid justify-items-start gap-2 rounded-control border border-dashed border-border p-4" data-testid="panel-error" data-code={errorCode}>
          <p className="font-medium">{ERROR_COPY[errorCode].title}</p>
          <p className="text-sm text-text-secondary">{ERROR_COPY[errorCode].body}</p>
          {ERROR_COPY[errorCode].retry && (
            <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
              Try again
            </Button>
          )}
        </div>
      ) : chunk ? (
        <>
          <ChunkBody chunk={chunk} highlight maxHeightClass="max-h-[45vh]" />
          <div className="flex items-center justify-between gap-2">
            <Button variant="outline" size="sm" disabled={!chunk.prev_chunk_id} onClick={() => chunk.prev_chunk_id && setChunkId(chunk.prev_chunk_id)} data-testid="panel-prev">
              <ChevronLeftIcon aria-hidden="true" />
              Previous
            </Button>
            <Button variant="outline" size="sm" disabled={!chunk.next_chunk_id} onClick={() => chunk.next_chunk_id && setChunkId(chunk.next_chunk_id)} data-testid="panel-next">
              Next
              <ChevronRightIcon aria-hidden="true" />
            </Button>
          </div>
        </>
      ) : null}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
        <Link href={documentHref(orgSlug, target.workspaceRef, target.documentId, target.versionId)} className="inline-flex items-center gap-1 underline underline-offset-2" data-testid="panel-open-document">
          <ExternalLinkIcon aria-hidden="true" className="size-3.5" />
          Open in document
        </Link>
        <Link
          href={evidenceHref({ orgSlug, workspaceRef: target.workspaceRef, documentId: target.documentId, versionId: target.versionId, chunkId, page: null })}
          className="underline underline-offset-2"
          data-testid="panel-open-evidence"
        >
          Full evidence view
        </Link>
      </div>
    </div>
  );
}
