"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Skeleton } from "@/components/ui/skeleton";
import { useApiFetcher } from "@/lib/query/provider";
import { evidenceHref } from "@/features/evidence/links";
import { getClaim, toGraphErrorCode } from "./api";

export type ProvenancePanelProps = {
  orgId: string;
  orgSlug: string;
  workspaceId: string;
  workspaceRef: string;
  claimId: string;
  onClose: () => void;
};

export function pages(a: number | null, b: number | null): string {
  if (a === null) return "";
  return b !== null && b !== a ? `pp. ${a}-${b}` : `p. ${a}`;
}

/** Why an edge exists: the claim and each piece of evidence, linking to the evidence viewer. */
export function ProvenancePanel({ orgId, orgSlug, workspaceId, workspaceRef, claimId, onClose }: ProvenancePanelProps) {
  const fetcher = useApiFetcher();
  const q = useQuery({
    queryKey: ["org", orgId, "graph", workspaceId, "claim", claimId],
    queryFn: ({ signal }) => getClaim(fetcher, { orgId, workspaceId }, claimId, signal),
  });
  const code = q.error ? toGraphErrorCode(q.error) : null;
  return (
    <aside aria-label="Why this connection exists" className="grid gap-3 rounded-lg border border-border p-4" data-testid="provenance-panel">
      <div className="flex items-start justify-between gap-2">
        <h2 className="text-base font-medium">Source of this connection</h2>
        <button type="button" onClick={onClose} className="text-sm text-muted-foreground underline" data-testid="provenance-close">
          Close
        </button>
      </div>
      {q.isPending ? <Skeleton className="h-16 w-full" /> : null}
      {code === "not_found" ? <p role="status">This connection is no longer available.</p> : null}
      {code === "unavailable" ? <p role="status">The knowledge graph is temporarily unavailable. Try again in a moment.</p> : null}
      {code && code !== "not_found" && code !== "unavailable" ? <p role="status">We couldn&apos;t load this connection.</p> : null}
      {q.data ? (
        <>
          <p className="text-sm" data-testid="provenance-claim">
            <strong>{q.data.claim.subject.name}</strong> {q.data.claim.predicate.replaceAll("_", " ")}{" "}
            <strong>{q.data.claim.object?.name ?? q.data.claim.object_text ?? ""}</strong>
            <span className="ml-2 text-xs text-muted-foreground">confidence {Math.round(q.data.claim.confidence * 100)}%</span>
          </p>
          {q.data.evidence.length === 0 ? <p className="text-sm text-muted-foreground">No readable evidence is available for this connection.</p> : null}
          <ul className="grid gap-2" aria-label="Evidence">
            {q.data.evidence.map((ev) => (
              <li key={`${ev.chunk_id}`} className="rounded-md border border-border p-3 text-sm">
                <Link
                  className="font-medium underline focus-visible:outline-2 focus-visible:outline-ring"
                  href={evidenceHref({ orgSlug, workspaceRef, documentId: ev.document_id, versionId: ev.version_id, chunkId: ev.chunk_id, page: ev.page_start })}
                >
                  {ev.title || "Document"} {pages(ev.page_start, ev.page_end)}
                </Link>
                <p className="mt-1 text-muted-foreground">{ev.excerpt}</p>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </aside>
  );
}
