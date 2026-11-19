"use client";

import { useCallback, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useApiFetcher } from "@/lib/query/provider";
import { getNeighborhood, searchEntities, toGraphErrorCode, type EntityHit, type GraphErrorCode } from "./api";
import { GraphCanvas } from "./graph-canvas";
import { collapseNode, layout, mergeNeighborhood, moveFocus, startGraph, type ArrowKey, type GraphState } from "./model";
import { ProvenancePanel } from "./provenance-panel";

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
  const nodeList = state ? Object.values(state.nodes) : [];
  const edgeList = state ? Object.values(state.edges) : [];
  const name = (id: string) => state?.nodes[id]?.name ?? "?";

  return (
    <div className="grid gap-4" data-testid="graph-explorer">
      <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-2" role="search">
        <div className="grid gap-1">
          <Label htmlFor="graph-q">Find an entity</Label>
          <Input id="graph-q" value={text} onChange={(e) => setText(e.target.value)} maxLength={200} placeholder="A person, product, policy..." />
        </div>
        <Button type="submit">Search</Button>
      </form>

      {shownError ? (
        <p role="status" data-testid="graph-error">
          {ERROR_COPY[shownError]}
        </p>
      ) : null}
      {hits.isFetching ? <Skeleton className="h-10 w-full" /> : null}
      {hits.data && hits.data.items.length === 0 ? <p role="status">No entities match. Try fewer or different words.</p> : null}
      {hits.data && hits.data.items.length > 0 ? (
        <ul aria-label="Matching entities" className="flex flex-wrap gap-2">
          {hits.data.items.map((h) => (
            <li key={h.id}>
              <Button type="button" variant="outline" onClick={() => void open(h)} disabled={busy}>
                {h.name} <span className="ml-1 text-xs text-muted-foreground">{h.type}</span>
              </Button>
            </li>
          ))}
        </ul>
      ) : null}

      {state ? (
        <>
          {state.truncated ? (
            <p role="status" data-testid="graph-truncated">
              This neighborhood is truncated, narrow your search to see the rest.
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground">
            Arrow keys move between nodes, Enter expands a node, Escape collapses it. A table version follows the graph.
          </p>
          <div ref={canvasRef} onKeyDown={onKeyDown} data-testid="graph-keys">
            <GraphCanvas state={state} focusedId={focusedId} onNodeClick={(id) => { setFocusedId(id); void expand(id); }} onEdgeClick={setClaimId} />
          </div>
          <section aria-labelledby="graph-table-h" className="grid gap-2">
            <h2 id="graph-table-h" className="text-base font-medium">
              Table view
            </h2>
            <table className="w-full text-sm" data-testid="graph-nodes-table">
              <caption className="sr-only">Entities in view</caption>
              <thead>
                <tr>
                  <th scope="col" className="text-left">Entity</th>
                  <th scope="col" className="text-left">Type</th>
                  <th scope="col" className="text-left">Actions</th>
                </tr>
              </thead>
              <tbody>
                {nodeList.map((n) => {
                  const isOpen = state.expanded.includes(n.id);
                  return (
                    <tr key={n.id}>
                      <th scope="row" className="text-left font-normal">{n.name}</th>
                      <td>{n.type}</td>
                      <td>
                        <button type="button" className="underline" onClick={() => (isOpen ? collapse(n.id) : void expand(n.id))}>
                          {isOpen ? "Collapse" : "Expand"}<span className="sr-only"> {n.name}</span>
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <table className="w-full text-sm" data-testid="graph-edges-table">
              <caption className="sr-only">Connections in view</caption>
              <thead>
                <tr>
                  <th scope="col" className="text-left">From</th>
                  <th scope="col" className="text-left">Relation</th>
                  <th scope="col" className="text-left">To</th>
                  <th scope="col" className="text-left">Source</th>
                </tr>
              </thead>
              <tbody>
                {edgeList.map((e) => (
                  <tr key={e.id}>
                    <td>{name(e.subject_id)}</td>
                    <td>{e.predicate.replaceAll("_", " ")}</td>
                    <td>{name(e.object_id)}</td>
                    <td>
                      <button type="button" className="underline" onClick={() => setClaimId(e.claim_id)}>
                        Show source<span className="sr-only"> of {name(e.subject_id)} {e.predicate} {name(e.object_id)}</span>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      ) : null}

      {claimId ? (
        <ProvenancePanel orgId={orgId} orgSlug={orgSlug} workspaceId={workspaceId} workspaceRef={workspaceRef} claimId={claimId} onClose={() => setClaimId(null)} />
      ) : null}
    </div>
  );
}
