"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { ScopeBadge, StatusBadge } from "@/components/ui/badges";
import { SurfaceCard } from "@/components/ui/surface";
import { useApiFetcher } from "@/lib/query/provider";
import { evidenceHref, workspaceRefFor } from "@/features/evidence/links";
import type { Workspace } from "@/features/workspaces/api";
import { approveMemory, deleteMemory, errorText, getMemory, rejectMemory, type Memory } from "./api";

export const memoryKeys = {
  list: (orgId: string, state: Memory["state"]) => ["org", orgId, "memory", "list", state] as const,
  one: (orgId: string, id: string) => ["org", orgId, "memory", "one", id] as const,
};

type Act = "approve" | "reject" | "delete";

/** Runs one memory action (approve / reject / delete) and refreshes every memory list. Errors are surfaced as calm copy. */
function useMemoryAction(orgId: string, id: string) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const act = useMutation({
    mutationFn: async (a: Act): Promise<unknown> => (a === "approve" ? approveMemory(fetcher, orgId, id) : a === "reject" ? rejectMemory(fetcher, orgId, id) : deleteMemory(fetcher, orgId, id)),
    onSuccess: () => {
      setError(null);
      void qc.invalidateQueries({ queryKey: ["org", orgId, "memory"] });
    },
    onError: (e) => setError(errorText(e)),
  });
  return { run: (a: Act) => act.mutate(a), pending: act.isPending, error };
}

/**
 * The approval gate. Approve is the only way a suggestion becomes usable in answers; Reject keeps it out; Delete removes it.
 * Rendered only for suggested memories (approved/rejected ones get delete/revoke only).
 */
export function MemoryApprovalActions({ onAct, pending, showApproval = true, deleteLabel = "Delete" }: { onAct: (a: Act) => void; pending: boolean; showApproval?: boolean; deleteLabel?: string }) {
  return (
    <div className="flex flex-wrap gap-2">
      {showApproval && (
        <>
          <Button size="sm" disabled={pending} onClick={() => onAct("approve")} data-testid="approve">
            Approve
          </Button>
          <Button size="sm" variant="outline" disabled={pending} onClick={() => onAct("reject")} data-testid="reject">
            Reject
          </Button>
        </>
      )}
      <Button size="sm" variant="ghost" disabled={pending} onClick={() => onAct("delete")} data-testid="delete">
        {deleteLabel}
      </Button>
    </div>
  );
}

const SOURCE_LABEL: Record<string, string> = { chat: "From a chat answer", research: "From research", manual: "Added by hand" };

function MemoryCardBody({ memory: m, orgId, orgSlug, workspaces, actions, error, children }: { memory: Memory; orgId: string; orgSlug: string; workspaces: readonly Workspace[]; actions: ReactNode; error: string | null; children?: ReactNode }) {
  const fetcher = useApiFetcher();
  const [showEvidence, setShowEvidence] = useState(false);
  const detail = useQuery({ queryKey: memoryKeys.one(orgId, m.id), enabled: showEvidence, retry: false, queryFn: ({ signal }) => getMemory(fetcher, orgId, m.id, signal) });
  const evidence = detail.data?.evidence ?? m.evidence;
  const anyStale = evidence.some((e) => e.evidence_status === "stale");
  return (
    <SurfaceCard as="article" className="grid gap-3 p-4" data-testid="memory-item" data-state={m.state}>
      <p className="text-sm break-words whitespace-pre-wrap">{m.statement}</p>
      <div className="flex flex-wrap items-center gap-1.5">
        <ScopeBadge scope={m.visibility === "private" ? "personal" : "workspace"} />
        <StatusBadge>{m.kind}</StatusBadge>
        {m.state === "invalidated" && <StatusBadge tone="warning">Invalidated{m.invalidated_reason ? ` (${m.invalidated_reason.replace(/_/g, " ")})` : ""}</StatusBadge>}
        {m.state === "expired" && <StatusBadge tone="warning">Expired</StatusBadge>}
        {anyStale && (
          <span role="note" className="inline-flex" data-testid="stale-badge">
            <StatusBadge tone="warning">Stale evidence: a source changed or was removed; this memory is not used.</StatusBadge>
          </span>
        )}
      </div>
      <p className="text-meta text-text-secondary">
        {SOURCE_LABEL[m.source] ?? `Source: ${m.source}`}
        {m.created_at ? ` · ${m.created_at.slice(0, 10)}` : ""}
        {m.expires_at ? ` · expires ${m.expires_at.slice(0, 10)}` : ""}
      </p>
      {children}
      {m.evidence.length > 0 && (
        <div className="grid gap-1">
          <Button variant="ghost" size="xs" className="w-fit" aria-expanded={showEvidence} onClick={() => setShowEvidence((v) => !v)} data-testid="toggle-evidence">
            Evidence ({m.evidence.length})
          </Button>
          {showEvidence && (
            <ul className="grid gap-1 text-xs" data-testid="evidence-list">
              {evidence.map((e) => (
                <li key={e.chunk_id} className="flex flex-wrap items-center gap-2">
                  <Link
                    className="focus-ring text-brand-blue underline underline-offset-2"
                    data-testid="evidence-link"
                    href={evidenceHref({
                      orgSlug,
                      workspaceRef: workspaceRefFor(workspaces, e.workspace_id ?? m.workspace_id ?? ""),
                      documentId: e.document_id,
                      versionId: e.version_id,
                      chunkId: e.chunk_id,
                    })}
                  >
                    Open source passage
                  </Link>
                  {e.evidence_status === "stale" && <span data-testid="evidence-stale">stale</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {actions}
      {error && (
        <p role="alert" className="text-xs text-danger" data-testid="memory-error">
          {error}
        </p>
      )}
    </SurfaceCard>
  );
}

type CardProps = { memory: Memory; orgId: string; orgSlug: string; workspaces: readonly Workspace[] };

/** A proposed memory awaiting the user's decision. Not used in answers until approved. */
export function MemorySuggestionCard(props: CardProps) {
  const a = useMemoryAction(props.orgId, props.memory.id);
  return (
    <MemoryCardBody {...props} error={a.error} actions={<MemoryApprovalActions onAct={a.run} pending={a.pending} />}>
      <p className="text-meta text-text-secondary">Suggested. Not used in answers until you approve it.</p>
    </MemoryCardBody>
  );
}

/** An approved (or rejected / expired / invalidated) memory: shows creator source, scope and stale state; can be revoked or deleted. */
export function ApprovedMemoryCard(props: CardProps) {
  const a = useMemoryAction(props.orgId, props.memory.id);
  const approved = props.memory.state === "approved";
  return <MemoryCardBody {...props} error={a.error} actions={<MemoryApprovalActions showApproval={false} deleteLabel={approved ? "Revoke" : "Delete"} onAct={a.run} pending={a.pending} />} />;
}
