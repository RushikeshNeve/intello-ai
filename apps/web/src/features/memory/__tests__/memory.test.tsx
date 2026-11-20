// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery, ORG_A, ws } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import { evidenceFromCitations, parseMemory } from "../api";
import { MemoryApp } from "../memory-app";
import { RememberButton } from "../remember-button";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
afterEach(cleanup);

const O = "/v1/orgs/o1/memory";
const WS = ws(ORG_A, "notes", "Notes");
const ev = (status?: string) => ({ chunk_id: "c1", document_id: "d1", version_id: "v1", workspace_id: WS.id, ...(status ? { evidence_status: status } : {}) });
const mem = (id: string, state: string, extra = {}) => ({ id, workspace_id: WS.id, visibility: "private", kind: "fact", statement: `Statement ${id}`, state, evidence: [ev()], source: "chat", created_at: "t", ...extra });

function mount(routes: Parameters<typeof fakeFetcher>[0]) {
  const api = fakeFetcher(routes);
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  renderWithQuery(<MemoryApp orgId="o1" orgSlug="acme" workspaces={[WS]} />, { fetcher: api.fetcher, client });
  return api;
}

describe("memory api", () => {
  it("parses a memory, defaults evidence status and rejects junk", () => {
    const m = parseMemory(mem("m1", "suggested"));
    expect(m.evidence[0].evidence_status).toBeNull();
    expect(() => parseMemory({ id: "x", kind: "fact", statement: "s", state: "bogus" })).toThrow();
  });
  it("dedupes citations into evidence", () => {
    expect(evidenceFromCitations([{ chunk_id: "a", document_id: "d", version_id: "v" }, { chunk_id: "a", document_id: "d", version_id: "v" }])).toHaveLength(1);
  });
});

describe("MemoryApp", () => {
  it("lists suggestions, approves one (POST) and refreshes; rejected tab queries its state", async () => {
    let approved = false;
    const api = mount({
      [`GET ${O}`]: (_i, url) => {
        const state = url.searchParams.get("state");
        if (state === "suggested") return jsonResponse(200, { items: approved ? [] : [mem("m1", "suggested")], next_cursor: null });
        return jsonResponse(200, { items: [mem("m9", "rejected")], next_cursor: null });
      },
      [`POST ${O}/m1/approve`]: () => {
        approved = true;
        return jsonResponse(200, mem("m1", "approved"));
      },
    });
    expect((await screen.findByTestId("memory-item")).textContent).toContain("Statement m1");
    await userEvent.click(screen.getByTestId("approve"));
    await screen.findByTestId("memory-empty");
    expect(api.calls.some((c) => c.method === "POST" && c.path === `${O}/m1/approve`)).toBe(true);
    await userEvent.click(screen.getByTestId("tab-rejected"));
    expect((await screen.findByText("Statement m9")).closest("[data-testid=memory-item]")?.getAttribute("data-state")).toBe("rejected");
    expect(screen.queryByTestId("approve")).toBeNull(); // only suggestions can be approved
  });

  it("reject and delete call the right routes; failures show an error", async () => {
    const api = mount({
      [`GET ${O}`]: () => jsonResponse(200, { items: [mem("m1", "suggested")], next_cursor: null }),
      [`POST ${O}/m1/reject`]: () => jsonResponse(200, mem("m1", "rejected")),
      [`DELETE ${O}/m1`]: () => jsonResponse(403, { detail: "forbidden" }),
    });
    await userEvent.click(await screen.findByTestId("reject"));
    await waitFor(() => expect(api.calls.some((c) => c.path === `${O}/m1/reject`)).toBe(true));
    await userEvent.click(screen.getByTestId("delete"));
    expect((await screen.findByTestId("memory-error")).textContent).toMatch(/permission/);
  });

  it("evidence preview links to the evidence viewer and shows the stale badge from the detail", async () => {
    mount({
      [`GET ${O}`]: () => jsonResponse(200, { items: [mem("m1", "approved")], next_cursor: null }),
      [`GET ${O}/m1`]: () => jsonResponse(200, mem("m1", "approved", { evidence: [ev("stale")] })),
    });
    await userEvent.click(await screen.findByTestId("toggle-evidence"));
    const link = await screen.findByTestId("evidence-link");
    expect(link.getAttribute("href")).toBe("/o/acme/w/notes/documents/d1/evidence?v=v1&chunk=c1");
    await screen.findByTestId("stale-badge");
    expect(screen.getByTestId("evidence-stale")).toBeTruthy();
  });
});

describe("RememberButton", () => {
  const cites = [{ chunk_id: "c1", document_id: "d1", version_id: "v1", workspace_id: "w1" }];
  it("POSTs a suggestion with the answer's citations as evidence", async () => {
    const api = fakeFetcher({ [`POST ${O}/suggestions`]: () => jsonResponse(201, mem("m1", "suggested")) });
    renderWithQuery(<RememberButton orgId="o1" orgSlug="acme" answer="The sky is blue [1]." citations={cites} />, { fetcher: api.fetcher });
    await userEvent.click(screen.getByTestId("remember-open"));
    await userEvent.click(screen.getByTestId("remember-save"));
    await screen.findByTestId("remember-saved");
    const body = JSON.parse(api.calls[0].body!);
    expect(body).toMatchObject({ kind: "fact", workspace_id: "w1", evidence: [{ chunk_id: "c1", document_id: "d1", version_id: "v1" }] });
    expect(body.statement).toBe("The sky is blue [1].");
  });
  it("is hidden for answers without citations and reports failures", async () => {
    const api = fakeFetcher({ [`POST ${O}/suggestions`]: () => jsonResponse(422, { detail: "x" }) });
    const { unmount } = renderWithQuery(<RememberButton orgId="o1" orgSlug="acme" answer="a" citations={[]} />, { fetcher: api.fetcher });
    expect(screen.queryByTestId("remember")).toBeNull();
    unmount();
    renderWithQuery(<RememberButton orgId="o1" orgSlug="acme" answer="a" citations={cites} />, { fetcher: api.fetcher });
    await userEvent.click(screen.getByTestId("remember-open"));
    await userEvent.click(screen.getByTestId("remember-save"));
    await screen.findByTestId("remember-error");
  });
});
