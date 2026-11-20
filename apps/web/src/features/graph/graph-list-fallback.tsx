"use client";

import type { GraphState } from "./model";
import { nodeTone, TONE_DOT_CLASS } from "./node-style";

/**
 * Accessible table alternative to the canvas. It carries the same entities and connections (parity), is the primary
 * surface on small screens, and exposes expand/collapse and "show source" without a pointer.
 */
export function GraphListFallback({
  state,
  selectedId,
  onSelectNode,
  onExpand,
  onCollapse,
  onShowSource,
}: {
  state: GraphState;
  selectedId?: string | null;
  onSelectNode?: (id: string) => void;
  onExpand: (id: string) => void;
  onCollapse: (id: string) => void;
  onShowSource: (claimId: string) => void;
}) {
  const nodeList = Object.values(state.nodes);
  const edgeList = Object.values(state.edges);
  const name = (id: string) => state.nodes[id]?.name ?? "?";
  const th = "py-1.5 pr-3 text-left text-meta font-semibold text-text-secondary";
  const td = "py-2 pr-3 align-top";
  return (
    <section aria-labelledby="graph-table-h" className="grid gap-3 rounded-card border border-border bg-card p-4 shadow-card" data-testid="graph-list-fallback">
      <h2 id="graph-table-h" className="text-card-title font-semibold text-brand-indigo dark:text-foreground">
        Table view
      </h2>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" data-testid="graph-nodes-table">
          <caption className="sr-only">Entities in view</caption>
          <thead>
            <tr>
              <th scope="col" className={th}>Entity</th>
              <th scope="col" className={th}>Type</th>
              <th scope="col" className={th}>Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border-subtle">
            {nodeList.map((n) => {
              const isOpen = state.expanded.includes(n.id);
              return (
                <tr key={n.id} aria-selected={n.id === selectedId || undefined}>
                  <th scope="row" className={`${td} text-left font-medium`}>
                    <button type="button" className="focus-ring rounded text-left hover:underline" onClick={() => onSelectNode?.(n.id)} aria-current={n.id === selectedId || undefined}>
                      {n.name}
                    </button>
                  </th>
                  <td className={td}>
                    <span className="inline-flex items-center gap-1.5">
                      <span aria-hidden="true" className={`size-2 rounded-full ${TONE_DOT_CLASS[nodeTone(n.type)]}`} />
                      {n.type}
                    </span>
                  </td>
                  <td className={td}>
                    <button type="button" className="focus-ring rounded text-brand-blue underline-offset-2 hover:underline" onClick={() => (isOpen ? onCollapse(n.id) : onExpand(n.id))}>
                      {isOpen ? "Collapse" : "Expand"}{" "}
                      <span className="sr-only">{n.name}</span>
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" data-testid="graph-edges-table">
          <caption className="sr-only">Connections in view</caption>
          <thead>
            <tr>
              <th scope="col" className={th}>From</th>
              <th scope="col" className={th}>Relation</th>
              <th scope="col" className={th}>To</th>
              <th scope="col" className={th}>Source</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border-subtle">
            {edgeList.map((e) => (
              <tr key={e.id}>
                <td className={td}>{name(e.subject_id)}</td>
                <td className={td}>{e.predicate.replaceAll("_", " ")}</td>
                <td className={td}>{name(e.object_id)}</td>
                <td className={td}>
                  <button type="button" className="focus-ring rounded text-brand-blue underline-offset-2 hover:underline" onClick={() => onShowSource(e.claim_id)}>
                    Show source{" "}
                    <span className="sr-only">
                      of {name(e.subject_id)} {e.predicate} {name(e.object_id)}
                    </span>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
