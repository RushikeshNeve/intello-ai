/** Pure explorer state: merge/collapse neighborhoods under a node cap, layout, and arrow-key focus. */
import type { GraphEdge, GraphNode, Neighborhood } from "./api";

/** Hard client-side cap (the server caps too); beyond it the view says "truncated, narrow your search". */
export const NODE_CAP = 100;

export type GraphState = {
  rootId: string | null;
  nodes: Record<string, GraphNode>;
  edges: Record<string, GraphEdge>;
  /** node id -> the node whose expansion revealed it (the root has none). */
  parent: Record<string, string | null>;
  expanded: string[];
  truncated: boolean;
};

export const emptyState: GraphState = { rootId: null, nodes: {}, edges: {}, parent: {}, expanded: [], truncated: false };

export function startGraph(root: { id: string; type: string; name: string }, nh: Neighborhood): GraphState {
  const base: GraphState = { ...emptyState, rootId: root.id, nodes: { [root.id]: { ...root, degree_visible: 0 } }, parent: { [root.id]: null } };
  return mergeNeighborhood(base, root.id, nh);
}

export function mergeNeighborhood(state: GraphState, from: string, nh: Neighborhood): GraphState {
  const nodes = { ...state.nodes };
  const parent = { ...state.parent };
  let truncated = state.truncated || nh.truncated;
  for (const n of nh.nodes) {
    if (nodes[n.id]) {
      nodes[n.id] = n;
    } else if (Object.keys(nodes).length >= NODE_CAP) {
      truncated = true;
    } else {
      nodes[n.id] = n;
      parent[n.id] = from;
    }
  }
  const edges = { ...state.edges };
  for (const e of nh.edges) if (nodes[e.subject_id] && nodes[e.object_id]) edges[e.id] = e;
  const expanded = state.expanded.includes(from) ? state.expanded : [...state.expanded, from];
  return { ...state, nodes, edges, parent, expanded, truncated };
}

function descendants(state: GraphState, id: string): Set<string> {
  const out = new Set<string>();
  let grew = true;
  while (grew) {
    grew = false;
    for (const [child, p] of Object.entries(state.parent)) {
      if (!out.has(child) && p !== null && (p === id || out.has(p))) {
        out.add(child);
        grew = true;
      }
    }
  }
  return out;
}

/** Hides what `id`'s expansion revealed (its descendants and the edges touching them). */
export function collapseNode(state: GraphState, id: string): GraphState {
  if (!state.expanded.includes(id)) return state;
  const gone = descendants(state, id);
  const nodes = { ...state.nodes };
  const parent = { ...state.parent };
  for (const g of gone) {
    delete nodes[g];
    delete parent[g];
  }
  const edges = Object.fromEntries(Object.entries(state.edges).filter(([, e]) => !gone.has(e.subject_id) && !gone.has(e.object_id)));
  return { ...state, nodes, parent, edges, expanded: state.expanded.filter((x) => x !== id && !gone.has(x)) };
}

export type Positioned = { id: string; x: number; y: number };

/** Layered layout by expansion depth (deterministic; no physics). */
export function layout(state: GraphState): Positioned[] {
  const depth = (id: string): number => {
    let d = 0;
    for (let p = state.parent[id]; p; p = state.parent[p]) d++;
    return d;
  };
  const rows: Record<number, number> = {};
  return Object.keys(state.nodes)
    .sort((a, b) => depth(a) - depth(b) || (state.nodes[a]?.name ?? "").localeCompare(state.nodes[b]?.name ?? "") || a.localeCompare(b))
    .map((id) => {
      const d = depth(id);
      const row = (rows[d] = (rows[d] ?? -1) + 1);
      return { id, x: d * 260, y: row * 90 };
    });
}

export type ArrowKey = "ArrowUp" | "ArrowDown" | "ArrowLeft" | "ArrowRight";

/** The nearest node in the arrow's direction (cone of 45 degrees); `null` when there is none. */
export function moveFocus(positions: readonly Positioned[], currentId: string | null, key: ArrowKey): string | null {
  const cur = positions.find((p) => p.id === currentId);
  if (!cur) return positions[0]?.id ?? null;
  let best: { id: string; score: number } | null = null;
  for (const p of positions) {
    if (p.id === cur.id) continue;
    const dx = p.x - cur.x;
    const dy = p.y - cur.y;
    const along = key === "ArrowRight" ? dx : key === "ArrowLeft" ? -dx : key === "ArrowDown" ? dy : -dy;
    const across = key === "ArrowRight" || key === "ArrowLeft" ? Math.abs(dy) : Math.abs(dx);
    if (along <= 0 || across > along) continue;
    const score = along + across * 2;
    if (!best || score < best.score) best = { id: p.id, score };
  }
  return best?.id ?? null;
}
