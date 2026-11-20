"use client";

import { useCallback, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { SearchIcon } from "lucide-react";
import { SurfaceCard } from "@/components/ui/surface";
import { StatusBadge } from "@/components/ui/badges";
import { useApiFetcher } from "@/lib/query/provider";
import { getNeighborhood, searchEntities, toGraphErrorCode, type EntityHit, type GraphErrorCode } from "./api";
import { GraphCanvas } from "./graph-canvas";
import { collapseNode, layout, mergeNeighborhood, moveFocus, startGraph, type ArrowKey, type GraphState } from "./model";
import { GraphInspector } from "./graph-inspector";
import { GraphListFallback } from "./graph-list-fallback";
import { nodeTone, TONE_DOT_CLASS, type NodeTone } from "./node-style";

const ERROR_COPY: Record<GraphErrorCode, string> = {
  not_found: "Nothing was found here, or you don't have access.",
  unavailable: "The knowledge graph is temporarily unavailable. Your documents are safe; try again in a moment.",
  unauthenticated: "Your session has ended. Please sign in again.",
  invalid: "That request couldn't be run. Try different words.",
  unknown: "Something went wrong. Try again.",
};

const ARROWS = new Set<string>(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);

export type GraphExplorerProps = { orgId: string; orgSlug: string; workspaceId: string; workspaceRef: string };

export function GraphExplorer({ orgId, orgSlug, workspaceId, workspaceRef }: GraphExplorerProps) {
  const fetcher = useApiFetcher();
  const ids = useMemo(() => ({ orgId, workspaceId }), [orgId, workspaceId]);
  const [text, setText] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [state, setState] = useState<GraphState | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [claimId, setClaimId] = useState<string | null>(null);
  const [error, setError] = useState<GraphErrorCode | null>(null);
  const [busy, setBusy] = useState(false);
  const canvasRef = useRef<HTMLDivElement>(null);

  const hits = useQuery({
    queryKey: ["org", orgId, "graph", workspaceId, "entities", submitted],
    queryFn: ({ signal }) => searchEntities(fetcher, ids, { q: submitted ?? "" }, signal),
    enabled: submitted !== null,
  });

  const open = useCallback(
    async (hit: EntityHit) => {
      setBusy(true);
      setError(null);
      setClaimId(null);
      try {
        const nh = await getNeighborhood(fetcher, ids, { entityId: hit.id, depth: 1 });
        setState(startGraph(hit, nh));
        setFocusedId(hit.id);
      } catch (err) {
        setError(toGraphErrorCode(err));
      } finally {
        setBusy(false);
      }
    },
    [fetcher, ids],
  );

  const expand = useCallback(
    async (id: string) => {
      if (!state || state.expanded.includes(id)) return;
      setError(null);
      try {
        const nh = await getNeighborhood(fetcher, ids, { entityId: id, depth: 1 });
        setState((s) => (s ? mergeNeighborhood(s, id, nh) : s));
      } catch (err) {
        setError(toGraphErrorCode(err));
      }
    },
    [fetcher, ids, state],
  );

  const collapse = useCallback((id: string) => setState((s) => (s ? collapseNode(s, id) : s)), []);

  const focusNode = useCallback((id: string) => {
    setFocusedId(id);
    const all = canvasRef.current?.querySelectorAll<HTMLElement>("[data-id]") ?? [];
    Array.from(all)
      .find((el) => el.dataset.id === id)
      ?.focus();
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!state) return;
    if (ARROWS.has(e.key)) {
      const next = moveFocus(layout(state), focusedId, e.key as ArrowKey);
      if (next) {
        e.preventDefault();
        focusNode(next);
      }
    } else if (e.key === "Enter" && focusedId) {
      e.preventDefault();
      void expand(focusedId);
    } else if (e.key === "Escape" && focusedId) {
      e.preventDefault();
      collapse(focusedId);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(text.trim());
  };
  const searchError = hits.error ? toGraphErrorCode(hits.error) : null;
  const shownError = error ?? searchError;
  const name = (id: string) => state?.nodes[id]?.name ?? "?";
  const selected = focusedId && state ? (state.nodes[focusedId] ?? null) : null;
  const selectedEdges = selected && state ? Object.values(state.edges).filter((e) => e.subject_id === selected.id || e.object_id === selected.id) : [];
  const nodeCount = state ? Object.keys(state.nodes).length : 0;
  const edgeCount = state ? Object.keys(state.edges).length : 0;
  const legend: NodeTone[] = state ? Array.from(new Set(Object.values(state.nodes).map((n) => nodeTone(n.type)))) : [];
  const toneLabel: Record<NodeTone, string> = { blue: "Concepts", teal: "Documents and products", indigo: "People and projects", violet: "Synthesis" };

  return (
    <div className="grid gap-4" data-testid="graph-explorer">
      <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-2" role="search">
        <div className="grid min-w-0 flex-1 gap-1 sm:max-w-md">
          <Label htmlFor="graph-q">Find an entity</Label>
          <div className="relative">
            <SearchIcon aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-text-secondary" />
            <Input id="graph-q" className="pl-8" value={text} onChange={(e) => setText(e.target.value)} maxLength={200} placeholder="A person, product, policy..." />
          </div>
        </div>
        <Button type="submit">Search</Button>
      </form>

      {shownError ? (
        <p role="status" data-testid="graph-error" className="rounded-control bg-warning-soft px-3 py-2 text-sm text-warning">
          {ERROR_COPY[shownError]}
        </p>
      ) : null}
      {hits.isFetching ? <Skeleton className="h-10 w-full" /> : null}
      {hits.data && hits.data.items.length === 0 ? <p role="status" className="text-sm text-text-secondary">No entities match. Try fewer or different words.</p> : null}
      {hits.data && hits.data.items.length > 0 ? (
        <ul aria-label="Matching entities" className="flex flex-wrap gap-2">
          {hits.data.items.map((h) => (
            <li key={h.id}>
              <Button type="button" variant="outline" onClick={() => void open(h)} disabled={busy}>
                <span aria-hidden="true" className={`size-2 rounded-full ${TONE_DOT_CLASS[nodeTone(h.type)]}`} />
                {h.name} <span className="ml-1 text-xs text-muted-foreground">{h.type}</span>
              </Button>
            </li>
          ))}
        </ul>
      ) : null}

      {!state && !hits.data && !shownError ? (
        <SurfaceCard className="text-sm text-text-secondary" data-testid="graph-intro">
          Search for a person, product or policy to open a bounded neighborhood of connected knowledge, then expand nodes to follow relationships.
        </SurfaceCard>
      ) : null}

      {state ? (
        <>
          {state.truncated ? (
            <p role="status" data-testid="graph-truncated" className="rounded-control bg-warning-soft px-3 py-2 text-sm text-warning">
              This neighborhood is truncated, narrow your search to see the rest.
            </p>
          ) : null}
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
            <div className="grid min-w-0 content-start gap-3">
              <SurfaceCard className="grid gap-3 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge tone="info">Relationships</StatusBadge>
                  <span className="ml-auto text-meta text-text-secondary" data-testid="graph-counts">
                    {nodeCount} node{nodeCount === 1 ? "" : "s"} · {edgeCount} relationship{edgeCount === 1 ? "" : "s"}
                  </span>
                </div>
                <p className="text-meta text-text-secondary">Arrow keys move between nodes, Enter expands a node, Escape collapses it. A table version follows the graph.</p>
                <div ref={canvasRef} onKeyDown={onKeyDown} data-testid="graph-keys">
                  <GraphCanvas state={state} focusedId={focusedId} onNodeClick={(id) => { setFocusedId(id); setClaimId(null); void expand(id); }} onEdgeClick={setClaimId} />
                </div>
                <ul aria-label="Legend" className="flex flex-wrap gap-3 text-meta text-text-secondary">
                  {legend.map((t) => (
                    <li key={t} className="flex items-center gap-1.5">
                      <span aria-hidden="true" className={`size-2.5 rounded-full ${TONE_DOT_CLASS[t]}`} />
                      {toneLabel[t]}
                    </li>
                  ))}
                </ul>
              </SurfaceCard>
              <GraphListFallback
                state={state}
                selectedId={focusedId}
                onSelectNode={(id) => { setFocusedId(id); setClaimId(null); }}
                onExpand={(id) => void expand(id)}
                onCollapse={collapse}
                onShowSource={setClaimId}
              />
            </div>
            <div className="min-w-0 max-lg:order-first">
              {claimId || selected ? (
                <GraphInspector
                  orgId={orgId}
                  orgSlug={orgSlug}
                  workspaceId={workspaceId}
                  workspaceRef={workspaceRef}
                  claimId={claimId}
                  node={selected}
                  nodeEdges={selectedEdges}
                  nameOf={name}
                  onSelectClaim={setClaimId}
                  onClose={() => (claimId ? setClaimId(null) : setFocusedId(null))}
                />
              ) : null}
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
