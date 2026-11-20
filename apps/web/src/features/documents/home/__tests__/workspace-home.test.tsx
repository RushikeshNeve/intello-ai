// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import { makeDoc, WS, nav } from "@/features/documents/__tests__/helpers";
import { WorkspaceHome } from "../workspace-home";

vi.mock("next/navigation", async () => (await import("@/features/documents/__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
afterEach(cleanup);

const OVERVIEW = {
  documents_total: 5,
  documents: { queued: 1, processing: 1, ready: 2, failed: 0, unsupported: 0, no_text: 0, cancelled: 0 },
  sources: 3,
  pending_memories: 4,
  research: { total: 2, by_state: { done: 2 } },
  recent_chat_threads: 7,
};

function mount(items: unknown[], overview: () => Response = () => jsonResponse(200, OVERVIEW)) {
  const { fetcher } = fakeFetcher({
    [`GET ${WS}/documents`]: () => jsonResponse(200, { items, next_cursor: null, can_upload: true }),
    [`GET ${WS}/overview`]: overview,
  });
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  return renderWithQuery(
    <WorkspaceHome orgId="o1" orgSlug="acme" orgName="Acme" workspaceId="w1" workspaceSlug="notes" workspaceName="Notes" roleLabel="Admin" />,
    { fetcher, client },
  );
}

describe("WorkspaceHome", () => {
  it("shows the tiles from the overview endpoint, with no invented tiles", async () => {
    mount([makeDoc({ id: "d-1", title: "One" }), makeDoc({ id: "d-2", title: "Two" })]);
    const metrics = await screen.findByTestId("home-metrics");
    await within(metrics).findByText("Documents");
    expect(metrics.textContent).toMatch(/Documents5/);
    expect(metrics.textContent).toMatch(/Ready to search2/);
    expect(metrics.textContent).toMatch(/Processing2/);
    expect(metrics.textContent).toMatch(/Sources3/);
    expect(metrics.textContent).toMatch(/Memories to review4/);
    expect(metrics.textContent).not.toMatch(/trusted|concept/i);
    expect(screen.getAllByTestId("recent-document")).toHaveLength(2);
  });

  it("hides the tiles when the overview fails (no fake numbers)", async () => {
    mount([makeDoc()], () => jsonResponse(503, { detail: "x" }));
    await screen.findAllByTestId("recent-document");
    await waitFor(() => expect(screen.queryByTestId("home-metrics")).toBeNull());
  });

  it("shows an empty state for a workspace without documents", async () => {
    mount([]);
    expect(await screen.findByText("No documents yet")).toBeTruthy();
  });

  it("quick ask navigates to chat with the typed text", async () => {
    mount([]);
    await userEvent.type(screen.getByLabelText("Ask Intello AI"), "what is new?{Enter}");
    expect(nav.current()).toBe("/o/acme/chat?q=what%20is%20new%3F");
  });
});
