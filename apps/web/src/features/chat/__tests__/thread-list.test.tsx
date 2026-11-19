// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import { nav } from "@/features/documents/__tests__/helpers";
import { ThreadList } from "../thread-list";

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

const thread = (id: string, title: string) => ({ id, workspace_id: null, title, created_at: "a", updated_at: "b" });

function mount(pages: Array<{ items: ReturnType<typeof thread>[]; next_cursor: string | null }>, extra: Parameters<typeof fakeFetcher>[0] = {}) {
  let page = 0;
  const api = fakeFetcher({
    "GET /v1/orgs/o1/chat/threads": () => jsonResponse(200, pages[Math.min(page++, pages.length - 1)]),
    ...extra,
  });
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  renderWithQuery(<ThreadList orgId="o1" orgSlug="acme" activeThreadId="t-1" />, { fetcher: api.fetcher, client });
  return api;
}

describe("ThreadList", () => {
  it("lists own threads with links, marks the active one and offers New chat", async () => {
    mount([{ items: [thread("t-1", "Budget"), thread("t-2", "")], next_cursor: null }]);
    const links = await screen.findAllByTestId("thread-link");
    expect(links.map((l) => [l.textContent, l.getAttribute("href"), l.getAttribute("aria-current")])).toEqual([
      ["Budget", "/o/acme/chat/t-1", "page"],
      ["New chat", "/o/acme/chat/t-2", null],
    ]);
    expect(screen.getByTestId("new-chat").getAttribute("href")).toBe("/o/acme/chat");
  });

  it("an empty list says so", async () => {
    mount([{ items: [], next_cursor: null }]);
    await screen.findByTestId("no-threads");
  });

  it("loads older threads with the cursor", async () => {
    const api = mount([
      { items: [thread("t-1", "One")], next_cursor: "cur1" },
      { items: [thread("t-0", "Zero")], next_cursor: null },
    ]);
    await userEvent.click(await screen.findByRole("button", { name: "Show older" }));
    await waitFor(() => expect(screen.getAllByTestId("thread-link")).toHaveLength(2));
    expect(api.calls.at(-1)?.path).toContain("after=cur1");
  });

  it("delete asks for confirmation, then deletes and leaves the deleted active thread", async () => {
    const api = mount([{ items: [thread("t-1", "Budget")], next_cursor: null }], {
      "DELETE /v1/orgs/o1/chat/threads/t-1": () => new Response(null, { status: 204 }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Delete conversation Budget" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("Delete this conversation?");
    expect(api.calls.some((c) => c.method === "DELETE")).toBe(false); // not yet
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api.calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Delete conversation Budget" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.calls.some((c) => c.method === "DELETE")).toBe(true));
    await waitFor(() => expect(nav.pushes).toEqual(["/o/acme/chat"]));
  });

  it("a failed delete keeps the dialog with a message", async () => {
    mount([{ items: [thread("t-1", "Budget")], next_cursor: null }], {
      "DELETE /v1/orgs/o1/chat/threads/t-1": () => jsonResponse(500, { detail: "x" }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Delete conversation Budget" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }));
    expect((await screen.findByRole("alert")).textContent).toContain("couldn't delete");
  });
});
