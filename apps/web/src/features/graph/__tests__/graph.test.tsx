// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { ApiError } from "@/features/organizations/api/http";
import { parseClaimDetail, parseNeighborhood, toGraphErrorCode } from "../api";
import { GraphExplorer } from "../graph-explorer";
import { NODE_CAP, collapseNode, layout, mergeNeighborhood, moveFocus, startGraph } from "../model";
import { ProvenancePanel } from "../provenance-panel";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
// React Flow needs real layout (ResizeObserver, sizes); the canvas is a thin wrapper, the table is the tested surface.
vi.mock("../graph-canvas", () => ({ GraphCanvas: () => <div data-testid="canvas-stub" /> }));

afterEach(cleanup);

const node = (id: string, name = id) => ({ id, type: "concept", name, degree_visible: 1 });
const edge = (id: string, s: string, o: string) => ({ id, predicate: "depends_on", subject_id: s, object_id: o, claim_id: id, confidence: 0.9, evidence_count: 1 });
const nh = (nodes: ReturnType<typeof node>[], edges: ReturnType<typeof edge>[], truncated = false) => ({ nodes, edges, truncated, next_cursor: null });

describe("api parsing", () => {
  it("parses a neighborhood, dropping malformed records and defaulting flags", () => {
    const out = parseNeighborhood({ nodes: [node("a"), { id: 1 }], edges: [edge("c1", "a", "b"), "x"] });
    expect(out.nodes.map((n) => n.id)).toEqual(["a"]);
    expect(out.edges).toHaveLength(1);
    expect(out.truncated).toBe(false);
    expect(() => parseNeighborhood({ nodes: "no" })).toThrow(ApiError);
  });

  it("parses a claim with a literal object and maps errors to calm codes", () => {
    const d = parseClaimDetail({
      claim: { id: "c", predicate: "owns", subject: node("a"), object: null, object_text: "x", confidence: 0.5 },
      evidence: [{ chunk_id: "k", document_id: "d", version_id: "v", title: "T", page_start: 2, page_end: null, excerpt: "e" }],
    });
    expect(d.claim.object).toBeNull();
    expect(d.evidence[0]?.page_start).toBe(2);
    expect(toGraphErrorCode(new ApiError(404, "not_found"))).toBe("not_found");
    expect(toGraphErrorCode(new ApiError(503, "graph_unavailable"))).toBe("unavailable");
  });
});

describe("model", () => {
  it("expands, collapses and caps", () => {
    const s0 = startGraph(node("r"), nh([node("a"), node("b")], [edge("e1", "r", "a")]));
    expect(Object.keys(s0.nodes).sort()).toEqual(["a", "b", "r"].sort());
    const s1 = mergeNeighborhood(s0, "a", nh([node("c")], [edge("e2", "a", "c")]));
    expect(s1.nodes.c).toBeDefined();
    const s2 = collapseNode(s1, "a");
    expect(s2.nodes.c).toBeUndefined();
    expect(s2.edges.e2).toBeUndefined();
    expect(s2.nodes.a).toBeDefined();
    const many = Array.from({ length: NODE_CAP + 20 }, (_, i) => node(`n${i}`));
    const capped = mergeNeighborhood(s0, "r", nh(many, []));
    expect(Object.keys(capped.nodes).length).toBe(NODE_CAP);
    expect(capped.truncated).toBe(true);
  });

  it("moves focus to the nearest node in the arrow's direction", () => {
    const pos = layout(startGraph(node("r"), nh([node("a"), node("b")], [])));
    const [r, a, b] = ["r", "a", "b"].map((id) => pos.find((p) => p.id === id)!);
    expect(moveFocus(pos, r.id, "ArrowRight")).toBe("a");
    expect(moveFocus(pos, a.id, "ArrowDown")).toBe(b.id);
    expect(moveFocus(pos, a.id, "ArrowLeft")).toBe("r");
    expect(moveFocus(pos, r.id, "ArrowLeft")).toBeNull();
  });
});

const IDS = { org: "o1", ws: "w1" };
const base = `/v1/orgs/${IDS.org}/workspaces/${IDS.ws}/graph`;

function routes(extra: Record<string, () => Response> = {}) {
  return {
    [`GET ${base}/entities`]: () => jsonResponse(200, { items: [{ id: "r", type: "concept", name: "Atlas" }], next_cursor: null }),
    [`GET ${base}/neighborhood`]: (_i: unknown, url: URL) =>
      url.searchParams.get("entity_id") === "r"
        ? jsonResponse(200, nh([node("a", "Garnet")], [edge("c1", "r", "a")], true))
        : jsonResponse(200, nh([node("z", "Zeta")], [edge("c2", "a", "z")])),
    [`GET ${base}/claims/c1`]: () =>
      jsonResponse(200, {
        claim: { id: "c1", predicate: "depends_on", subject: node("r", "Atlas"), object: node("a", "Garnet"), object_text: null, confidence: 0.9 },
        evidence: [{ chunk_id: "ch1", document_id: "d1", version_id: "v1", title: "Runbook", page_start: 4, page_end: 4, excerpt: "Atlas needs Garnet" }],
      }),
    ...extra,
  };
}

function setup(extra?: Record<string, () => Response>) {
  const { fetcher, calls } = fakeFetcher(routes(extra));
  renderWithQuery(<GraphExplorer orgId={IDS.org} orgSlug="acme" workspaceId={IDS.ws} workspaceRef="notes" />, { fetcher });
  return calls;
}

async function openAtlas() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Find an entity"), "atl");
  await user.click(screen.getByRole("button", { name: "Search" }));
  await user.click(await screen.findByRole("button", { name: /Atlas/ }));
  await screen.findByTestId("graph-nodes-table");
  return user;
}

describe("explorer", () => {
  it("shows the truncated message, a table alternative, and expands/collapses from the keyboard", async () => {
    const calls = setup();
    await openAtlas();
    expect(screen.getByTestId("graph-truncated").textContent).toMatch(/narrow your search/);
    const table = screen.getByTestId("graph-nodes-table");
    expect(table.textContent).toContain("Garnet");
    expect(screen.getByTestId("graph-edges-table").textContent).toContain("depends on");

    const keys = screen.getByTestId("graph-keys");
    fireEvent.keyDown(keys, { key: "ArrowRight" }); // focus moves r -> a
    fireEvent.keyDown(keys, { key: "Enter" }); // expands a
    await waitFor(() => expect(table.textContent).toContain("Zeta"));
    expect(calls.filter((c) => c.path.includes("/neighborhood"))).toHaveLength(2);
    fireEvent.keyDown(keys, { key: "Escape" }); // collapses a
    await waitFor(() => expect(table.textContent).not.toContain("Zeta"));
  });

  it("opens the provenance panel with evidence linking to the evidence viewer", async () => {
    setup();
    const user = await openAtlas();
    await user.click(screen.getByRole("button", { name: /Show source/ }));
    const link = await screen.findByRole("link", { name: /Runbook/ });
    expect(link.getAttribute("href")).toBe("/o/acme/w/notes/documents/d1/evidence?v=v1&chunk=ch1&page=4");
  });

  it("shows calm states for a graph that is unavailable", async () => {
    setup({ [`GET ${base}/neighborhood`]: () => jsonResponse(503, { detail: "graph_unavailable" }) });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Find an entity"), "a");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await user.click(await screen.findByRole("button", { name: /Atlas/ }));
    expect((await screen.findByTestId("graph-error")).textContent).toMatch(/temporarily unavailable/);
  });
});

describe("provenance panel", () => {
  it("answers a missing claim with a calm message", async () => {
    const { fetcher } = fakeFetcher({});
    renderWithQuery(<ProvenancePanel orgId={IDS.org} orgSlug="acme" workspaceId={IDS.ws} workspaceRef="notes" claimId="gone" onClose={() => {}} />, { fetcher });
    expect(await screen.findByText(/no longer available/)).toBeTruthy();
  });
});
