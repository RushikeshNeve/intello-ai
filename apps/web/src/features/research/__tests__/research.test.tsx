// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery, ORG_A, ws } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import { DEFAULT_LIMITS, exportUrl, parseSession, pollInterval, POLL_MS } from "../api";
import { loweredLimits, ResearchHome } from "../research-home";
import { ResearchBriefCard } from "../research-brief-card";
import { ResearchSessionView } from "../session-view";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
afterEach(cleanup);

const R = "/v1/orgs/o1/research";
const WS = ws(ORG_A, "notes", "Notes");
const cite = { marker: "[1]", chunk_id: "c1", document_id: "d1", version_id: "v1", workspace_id: WS.id, title: "Doc", version_number: 1, page_start: 2, page_end: 2, excerpt: "x" };
const session = (state: string, extra = {}) => ({
  id: "r1",
  question: "What changed?",
  state,
  plan: { subqueries: ["a", "b"] },
  steps: [
    { seq: 0, kind: "plan", query: null, state: "done" },
    { seq: 1, kind: "search", query: "a", state: "running" },
  ],
  usage: {},
  ...extra,
});

function mountSession(routes: Parameters<typeof fakeFetcher>[0]) {
  const api = fakeFetcher(routes);
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  renderWithQuery(<ResearchSessionView orgId="o1" orgSlug="acme" sessionId="r1" workspaces={[WS]} />, { fetcher: api.fetcher, client });
  return api;
}

describe("research api", () => {
  it("polls every 2 s until a terminal state, and stops on failure", () => {
    const p = (state: string) => pollInterval(parseSession(session(state)));
    expect(pollInterval(undefined)).toBe(POLL_MS);
    expect([p("planned"), p("running")]).toEqual([POLL_MS, POLL_MS]);
    expect([p("completed"), p("failed"), p("cancelled")]).toEqual([false, false, false]);
    expect(pollInterval(undefined, true)).toBe(false);
  });
  it("parses report citations and tolerates a missing report", () => {
    expect(parseSession(session("running")).report).toBeNull();
    const s = parseSession(session("completed", { report: { sections: [{ heading: "H", statements: [{ text: "T", citations: [cite, { junk: 1 }] }] }], verification: { withheld_count: 2 } } }));
    expect(s.report?.sections[0].statements[0].citations).toHaveLength(1);
    expect(s.report?.withheld).toBe(2);
    expect(exportUrl("o1", "r1")).toBe("/api/v1/orgs/o1/research/r1/export?format=md");
  });
  it("only lowered limits are sent", () => {
    expect(loweredLimits({ ...DEFAULT_LIMITS })).toEqual({});
    expect(loweredLimits({ ...DEFAULT_LIMITS, max_subqueries: 3, max_evidence: 999 })).toEqual({ max_subqueries: 3 });
  });
});

describe("ResearchSessionView", () => {
  it("shows steps for a running session, cancels it (POST) and offers no export yet", async () => {
    let cancelled = false;
    const api = mountSession({
      [`GET ${R}/r1`]: () => jsonResponse(200, session(cancelled ? "cancelled" : "running")),
      [`POST ${R}/r1/cancel`]: () => {
        cancelled = true;
        return jsonResponse(202, {});
      },
    });
    expect((await screen.findByTestId("research-steps")).textContent).toContain("search: a");
    expect(screen.queryByTestId("research-export")).toBeNull();
    await userEvent.click(screen.getByTestId("research-cancel"));
    await waitFor(() => expect(screen.getByTestId("research-state").textContent).toBe("Cancelled."));
    expect(api.calls.some((c) => c.method === "POST" && c.path === `${R}/r1/cancel`)).toBe(true);
    expect(screen.queryByTestId("research-cancel")).toBeNull();
  });

  it("a completed session (e.g. after reload) shows the report, citation chips to the evidence viewer, and the export link", async () => {
    mountSession({
      [`GET ${R}/r1`]: () => jsonResponse(200, session("completed", { report: { sections: [{ heading: "Findings", statements: [{ text: "It changed.", citations: [cite] }] }], verification: { withheld_count: 1 } } })),
    });
    const chip = await screen.findByTestId("citation-chip");
    expect(chip.getAttribute("href")).toBe("/o/acme/w/notes/documents/d1/evidence?v=v1&chunk=c1&page=2");
    expect(screen.getByTestId("research-export").getAttribute("href")).toBe("/api/v1/orgs/o1/research/r1/export?format=md");
    expect(screen.getByTestId("report-withheld")).toBeTruthy();
    expect(screen.queryByTestId("research-cancel")).toBeNull();
  });

  it("a failed session shows a safe message; a missing one says so", async () => {
    mountSession({ [`GET ${R}/r1`]: () => jsonResponse(200, session("failed", { error_code: "timeout" })) });
    expect((await screen.findByTestId("research-state")).textContent).toMatch(/ran out of time/);
    cleanup();
    mountSession({});
    expect((await screen.findByTestId("research-error")).textContent).toMatch(/isn't available/);
  });
});

describe("ResearchHome", () => {
  it("starts research with scope and lowered limits, then opens the session", async () => {
    const api = fakeFetcher({
      [`GET ${R}`]: () => jsonResponse(200, { items: [{ id: "r0", question: "Old", state: "completed", created_at: "t" }], next_cursor: null }),
      [`POST ${R}`]: () => jsonResponse(202, { id: "r9", state: "planned" }),
    });
    const client = makeQueryClient();
    client.setDefaultOptions({ queries: { retry: false } });
    renderWithQuery(<ResearchHome orgId="o1" orgSlug="acme" workspaces={[WS]} />, { fetcher: api.fetcher, client });
    expect((await screen.findByTestId("research-link")).getAttribute("href")).toBe("/o/acme/research/r0");
    await userEvent.click(screen.getByTestId("research-new"));
    await userEvent.type(screen.getByTestId("research-question"), "Why?");
    await userEvent.selectOptions(screen.getByTestId("research-scope"), WS.id);
    const lim = screen.getByTestId("limit-max_subqueries");
    await userEvent.clear(lim);
    await userEvent.type(lim, "3");
    await userEvent.click(screen.getByTestId("research-start"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/o/acme/research/r9"));
    expect(JSON.parse(api.calls.find((c) => c.method === "POST")!.body!)).toEqual({ question: "Why?", workspace_ids: [WS.id], limits: { max_subqueries: 3 } });
  });
});

describe("ResearchBriefCard", () => {
  const item = (state: string) => ({ id: "r1", question: "What changed?", state: state as never, created_at: "2026-11-02T10:00:00Z" });
  it("labels each state (terminal and in flight) and links to the session", () => {
    const { unmount } = render(<ResearchBriefCard item={item("running")} href="/o/acme/research/r1" />);
    expect(screen.getByText("Researching")).toBeTruthy();
    expect(screen.getByTestId("research-card").textContent).toMatch(/Still running/);
    expect(screen.getByTestId("research-link").getAttribute("href")).toBe("/o/acme/research/r1");
    unmount();
    for (const [state, label] of [["completed", "Completed"], ["failed", "Failed"], ["cancelled", "Cancelled"]] as const) {
      const r = render(<ResearchBriefCard item={item(state)} href="/x" />);
      expect(screen.getByText(label)).toBeTruthy();
      expect(screen.getByTestId("research-card").textContent).toMatch(/Open the report/);
      expect(screen.getByText("2026-11-02")).toBeTruthy();
      r.unmount();
    }
  });
});
