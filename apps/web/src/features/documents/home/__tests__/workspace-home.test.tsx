// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, within } from "@testing-library/react";
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

function mount(items: unknown[], next_cursor: string | null = null) {
  const { fetcher } = fakeFetcher({ [`GET ${WS}/documents`]: () => jsonResponse(200, { items, next_cursor, can_upload: true }) });
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  return renderWithQuery(
    <WorkspaceHome orgId="o1" orgSlug="acme" orgName="Acme" workspaceId="w1" workspaceSlug="notes" workspaceName="Notes" roleLabel="Admin" />,
    { fetcher, client },
  );
}

describe("WorkspaceHome", () => {
  it("shows only figures derived from the document list, with no invented tiles", async () => {
    mount([makeDoc({ id: "d-1", title: "One" }), makeDoc({ id: "d-2", title: "Two" })]);
    const metrics = await screen.findByTestId("home-metrics");
    await within(metrics).findByText("Documents");
    expect(within(metrics).getByText("2")).toBeTruthy();
    expect(metrics.textContent).not.toMatch(/trusted|concept/i);
    expect(screen.getAllByTestId("recent-document")).toHaveLength(2);
  });

  it("marks the count as a lower bound when more pages exist", async () => {
    mount([makeDoc()], "cursor-1");
    expect(await screen.findByText("1+")).toBeTruthy();
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
