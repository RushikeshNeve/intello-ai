"use client";

import { useMemo } from "react";
import { Background, Controls, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { layout, type GraphState } from "./model";

export type GraphCanvasProps = {
  state: GraphState;
  focusedId: string | null;
  onNodeClick: (id: string) => void;
  onEdgeClick: (claimId: string) => void;
};

/** The visual graph (React Flow). Keyboard control and the accessible table live in the explorer. */
export function GraphCanvas({ state, focusedId, onNodeClick, onEdgeClick }: GraphCanvasProps) {
  const nodes: Node[] = useMemo(
    () =>
      layout(state).map((p) => {
        const n = state.nodes[p.id]!;
        const open = state.expanded.includes(p.id);
        return {
          id: p.id,
          position: { x: p.x, y: p.y },
          data: { label: `${n.name} (${n.type})` },
          selected: p.id === focusedId,
          ariaLabel: `${n.name}, ${n.type}, ${open ? "expanded" : "collapsed"}`,
          style: { fontWeight: open ? 600 : 400 },
        } satisfies Node;
      }),
    [state, focusedId],
  );
  const edges: Edge[] = useMemo(
    () =>
      Object.values(state.edges).map((e) => ({
        id: e.id,
        source: e.subject_id,
        target: e.object_id,
        label: e.predicate.replaceAll("_", " "),
        data: { claimId: e.claim_id },
        ariaLabel: `${state.nodes[e.subject_id]?.name ?? "?"} ${e.predicate} ${state.nodes[e.object_id]?.name ?? "?"}`,
      })),
    [state],
  );
  return (
    <div className="h-[28rem] rounded-lg border border-border" data-testid="graph-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        nodesDraggable={false}
        nodesConnectable={false}
        onNodeClick={(_, n) => onNodeClick(n.id)}
        onEdgeClick={(_, e) => onEdgeClick(String((e.data as { claimId?: string } | undefined)?.claimId ?? e.id))}
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
