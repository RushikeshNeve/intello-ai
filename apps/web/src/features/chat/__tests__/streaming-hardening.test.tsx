// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import { nav } from "@/features/documents/__tests__/helpers";
import { Conversation } from "../conversation";
import { applyRunEvent, INITIAL_RUN_VIEW } from "../run-state";
import { verificationNote } from "../messages";
import { detail, frame, msg, scripted, type Conn } from "./fixtures";

vi.mock("next/navigation", async () => (await import("@/features/documents/__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

beforeEach(() => nav.reset("/o/acme/chat/t-1"));
afterEach(cleanup);

function mount(conns: Conn[], routes: Parameters<typeof fakeFetcher>[0] = {}) {
  const api = fakeFetcher({
    "GET /v1/orgs/o1/chat/threads/t-1": () =>
      jsonResponse(200, detail({ messages: [msg({ id: "m1", role: "user", content: "Q", run_id: "r-7" })], active_run: { run_id: "r-7", state: "running" } })),
    "GET /v1/orgs/o1/chat/threads": () => jsonResponse(200, { items: [], next_cursor: null }),
    ...routes,
  });
  const { stream } = scripted(conns);
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: 30_000 }, mutations: { retry: false } });
  renderWithQuery(<Conversation orgId="o1" orgSlug="acme" workspaces={[]} threadId="t-1" stream={stream} />, { fetcher: api.fetcher, client });
}

describe("stage display", () => {
  it("moves searching, reading, writing, verifying from the run events", () => {
    let v = applyRunEvent(INITIAL_RUN_VIEW, 1, { type: "started" });
    expect(v.stage).toBe("searching");
    v = applyRunEvent(v, 2, { type: "retrieval", evidence_count: 3, dropped: 0, degraded: [] });
    expect(v.stage).toBe("reading");
    v = applyRunEvent(v, 3, { type: "delta", text: "Hi" });
    expect(v.stage).toBe("writing");
    v = applyRunEvent(v, 4, { type: "stage", stage: "verifying" });
    expect(v.stage).toBe("verifying");
  });

  it("shows the reading-sources label before any text", async () => {
    mount([{ frames: [frame(1, "started"), frame(2, "retrieval", { evidence_count: 2, dropped: 0, degraded: [] })], then: "hang" }]);
    await waitFor(() => expect(screen.getByTestId("run-waiting").textContent).toContain("Reading sources"));
  });

  it("shows the verifying label after the text", async () => {
    mount([{ frames: [frame(1, "started"), frame(2, "delta", { text: "abc" }), frame(3, "stage", { stage: "verifying" })], then: "hang" }]);
    await waitFor(() => expect(screen.getByTestId("run-stage").textContent).toContain("Verifying"));
  });
});

describe("cancel and unverified partials", () => {
  it("the cancel button posts to the cancel endpoint", async () => {
    let posts = 0;
    mount([{ frames: [frame(1, "started"), frame(2, "delta", { text: "Herons eat fish" })], then: "hang" }], {
      "POST /v1/orgs/o1/chat/runs/r-7/cancel": () => {
        posts++;
        return jsonResponse(202, { state: "cancelled" });
      },
    });
    await screen.findByTestId("live-answer");
    await userEvent.click(screen.getByTestId("cancel-run"));
    await waitFor(() => expect(posts).toBe(1));
  });

  it("a cancelled run keeps the partial text without citation markers or citations", () => {
    let v = applyRunEvent(INITIAL_RUN_VIEW, 1, { type: "started" });
    v = applyRunEvent(v, 2, { type: "delta", text: "Herons eat fish [1]" });
    v = applyRunEvent(v, 3, { type: "cancelled" });
    expect(v.status).toBe("cancelled");
    expect(v.text).toBe("Herons eat fish");
    expect(v.citations).toEqual([]);
  });

  it("an unverified timeout partial is labelled and never presented as grounded", () => {
    const note = verificationNote({ status: "unverified", withheld_count: 0, flagged_count: 0, stopped: "timeout" });
    expect(note?.tone).toBe("unverified");
    expect(note?.text).toContain("unverified");
    expect(note?.text).toContain("no sources");
  });
});
