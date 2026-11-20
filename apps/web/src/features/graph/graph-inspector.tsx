"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { XIcon } from "lucide-react";
import { Eyebrow } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/badges";
import { SurfaceCard } from "@/components/ui/surface";
import { useApiFetcher } from "@/lib/query/provider";
import { evidenceHref } from "@/features/evidence/links";
import { getClaim, toGraphErrorCode, type GraphEdge, type GraphNode } from "./api";
import { nodeTone, TONE_DOT_CLASS } from "./node-style";

export type GraphInspectorProps = {
  orgId: string;
  orgSlug: string;
  workspaceId: string;
  workspaceRef: string;
  /** Selected edge: shows the claim and its evidence. */
  claimId: string | null;
  /** Selected node (when no edge is selected): summary plus its visible connections. */
  node?: GraphNode | null;
  nodeEdges?: readonly GraphEdge[];
  nameOf?: (id: string) => string;
  onSelectClaim?: (claimId: string) => void;
  onClose: () => void;
};

export function pages(a: number | null, b: number | null): string {
  if (a === null) return "";
  return b !== null && b !== a ? `pp. ${a}-${b}` : `p. ${a}`;
}

/** Why a node or connection exists: the claim and each piece of evidence, linking to the evidence viewer. */
export function GraphInspector({ orgId, orgSlug, workspaceId, workspaceRef, claimId, node, nodeEdges = [], nameOf = (id) => id, onSelectClaim, onClose }: GraphInspectorProps) {
  return (
    <SurfaceCard as="div" className="grid content-start gap-3" aria-label="Selected knowledge" data-testid="graph-inspector">
      <div className="flex items-start justify-between gap-2">
        <Eyebrow>{claimId ? "Source of this connection" : "Selected knowledge"}</Eyebrow>
        <button type="button" onClick={onClose} aria-label="Close inspector" className="focus-ring -mt-1 -mr-1 grid size-7 place-items-center rounded-control text-text-secondary hover:bg-muted" data-testid="provenance-close">
          <XIcon aria-hidden="true" className="size-4" />
        </button>
      </div>
      {claimId ? (
        <ClaimDetail orgId={orgId} orgSlug={orgSlug} workspaceId={workspaceId} workspaceRef={workspaceRef} claimId={claimId} />
      ) : node ? (
        <>
          <div className="grid gap-1">
            <h2 className="text-section font-semibold text-brand-indigo break-words dark:text-foreground">{node.name}</h2>
            <p className="flex items-center gap-1.5 text-sm text-text-secondary">
              <span aria-hidden="true" className={`size-2.5 rounded-full ${TONE_DOT_CLASS[nodeTone(node.type)]}`} />
              {node.type}
            </p>
          </div>
          <h3 className="text-sm font-semibold">Evidence-backed relationships</h3>
          {nodeEdges.length === 0 ? (
            <p className="text-sm text-text-secondary">No connections in view for this entity.</p>
          ) : (
            <ul className="grid gap-2" aria-label="Relationships">
              {nodeEdges.map((e) => (
                <li key={e.id} className="rounded-control border border-border-subtle p-2.5 text-sm">
                  <button type="button" className="focus-ring w-full text-left" onClick={() => onSelectClaim?.(e.claim_id)} data-testid="inspector-edge">
                    <span className="font-medium">
                      {nameOf(e.subject_id)} {e.predicate.replaceAll("_", " ")} {nameOf(e.object_id)}
                    </span>
                    <span className="block text-meta text-text-secondary">
                      {e.evidence_count} reference{e.evidence_count === 1 ? "" : "s"} · show source
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : null}
    </SurfaceCard>
  );
}

function ClaimDetail({ orgId, orgSlug, workspaceId, workspaceRef, claimId }: { orgId: string; orgSlug: string; workspaceId: string; workspaceRef: string; claimId: string }) {
  const fetcher = useApiFetcher();
  const q = useQuery({
    queryKey: ["org", orgId, "graph", workspaceId, "claim", claimId],
    queryFn: ({ signal }) => getClaim(fetcher, { orgId, workspaceId }, claimId, signal),
  });
  const code = q.error ? toGraphErrorCode(q.error) : null;
  return (
    <div className="grid gap-3" data-testid="provenance-panel">
      {q.isPending ? <Skeleton className="h-16 w-full" /> : null}
      {code === "not_found" ? <p role="status">This connection is no longer available.</p> : null}
      {code === "unavailable" ? <p role="status">The knowledge graph is temporarily unavailable. Try again in a moment.</p> : null}
      {code && code !== "not_found" && code !== "unavailable" ? <p role="status">We couldn&apos;t load this connection.</p> : null}
      {q.data ? (
        <>
          <p className="text-sm break-words" data-testid="provenance-claim">
            <strong>{q.data.claim.subject.name}</strong> {q.data.claim.predicate.replaceAll("_", " ")}{" "}
            <strong>{q.data.claim.object?.name ?? q.data.claim.object_text ?? ""}</strong>
            <StatusBadge tone="info" className="ml-2">
              confidence {Math.round(q.data.claim.confidence * 100)}%
            </StatusBadge>
          </p>
          {q.data.evidence.length === 0 ? <p className="text-sm text-text-secondary">No readable evidence is available for this connection.</p> : null}
          <ul className="grid gap-2" aria-label="Evidence">
            {q.data.evidence.map((ev) => (
              <li key={`${ev.chunk_id}`} className="rounded-control border border-border-subtle p-3 text-sm">
                <Link
                  className="focus-ring font-medium text-brand-blue underline-offset-2 hover:underline"
                  href={evidenceHref({ orgSlug, workspaceRef, documentId: ev.document_id, versionId: ev.version_id, chunkId: ev.chunk_id, page: ev.page_start })}
                >
                  {ev.title || "Document"} {pages(ev.page_start, ev.page_end)}
                </Link>
                <p className="mt-1 text-text-secondary">{ev.excerpt}</p>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
