// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import { makeDoc, makeVersion, nav } from "@/features/documents/__tests__/helpers";
import type { Workspace } from "@/features/workspaces/api";
import { CHAT_GRID_CLASS, ChatApp, PANEL_COLUMN_CLASS } from "../chat-app";
import { VerificationBanner } from "../answer-view";
import { END, cit, detail, frame, msg, scripted } from "./fixtures";

vi.mock("next/navigation", async () => (await import("@/features/documents/__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const workspaces: Workspace[] = [{ id: "w1", org_id: "o1", slug: "notes", name: "Acme Notes", created_at: "", role: "admin" }];
const T = "/v1/orgs/o1/chat/threads/t-1";
const chunkPath = (n: number) => `/v1/orgs/o1/workspaces/w1/documents/d-${n}/versions/v-${n}/chunks/c-${n}`;
const chunkBody = (n: number, over: Record<string, unknown> = {}) => ({
  chunk_id: `c-${n}`,
  text: `Passage text ${n}`,
  page_start: 3,
  page_end: 3,
  heading_path: [],
  locators: [],
  version_number: 2,
  title: `Doc ${n}`,
  neighbors: { prev_chunk_id: null, next_chunk_id: `c-${n}x` },
  ...over,
});

const thread = detail({
  messages: [
    msg({ id: "m1", role: "user", content: "What grew?" }),
    msg({
      id: "m2",
      role: "assistant",
      content: "Revenue grew [1] and costs [2].",
      citations: [cit(1), cit(2)],
      verification: { status: "grounded", withheld_count: 0, flagged_count: 0 },
      run_id: "r-0",
    }),
  ],
});

type Routes = Parameters<typeof fakeFetcher>[0];
function mount(routes: Routes = {}, t = thread, conns: Parameters<typeof scripted>[0] = []) {
  const api = fakeFetcher({
    [`GET ${T}`]: () => jsonResponse(200, t),
    "GET /v1/orgs/o1/chat/threads": () => jsonResponse(200, { items: [], next_cursor: null }),
    ...routes,
  });
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: 30_000 }, mutations: { retry: false } });
  const { stream } = scripted(conns);
  return renderWithQuery(<ChatApp orgId="o1" orgSlug="acme" initialWorkspaces={workspaces} threadId="t-1" stream={stream} />, { fetcher: api.fetcher, client });
}

beforeEach(() => nav.reset("/o/acme/chat/t-1"));
afterEach(cleanup);

describe("Ask → Citation → Source", () => {
  it("starts with an empty panel, then a chip click loads that chunk into it without navigating", async () => {
    mount({ [`GET ${chunkPath(2)}`]: () => jsonResponse(200, chunkBody(2)) });
    const panel = await screen.findByTestId("evidence-panel");
    expect(within(panel).getByTestId("evidence-panel-empty")).toBeTruthy();
    const chips = await screen.findAllByTestId("citation-chip");
    await userEvent.click(chips[1]);
    expect((await within(panel).findByTestId("evidence-text")).textContent).toBe("Passage text 2");
    expect(within(panel).getByTestId("panel-title").textContent).toBe("Doc 2");
    expect(within(panel).getByTestId("panel-meta").textContent).toContain("Version 2");
    expect(within(panel).getByTestId("panel-meta").textContent).toContain("page 3");
    expect(within(panel).getByTestId("panel-open-document").getAttribute("href")).toBe("/o/acme/w/notes/documents/d-2?v=v-2");
    expect(chips[1].getAttribute("aria-current")).toBe("true");
    expect(chips[0].getAttribute("aria-current")).toBeNull();
    expect(nav.pushes).toEqual([]);
  });

  it("chips are keyboard reachable and Enter selects", async () => {
    mount({ [`GET ${chunkPath(1)}`]: () => jsonResponse(200, chunkBody(1)) });
    const chips = await screen.findAllByTestId("citation-chip");
    chips[0].focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByTestId("evidence-text")).toBeTruthy();
  });

  it("a source-list item selects too, and prev/next move through neighbouring passages", async () => {
    mount({
      [`GET ${chunkPath(1)}`]: () => jsonResponse(200, chunkBody(1)),
      "GET /v1/orgs/o1/workspaces/w1/documents/d-1/versions/v-1/chunks/c-1x": () => jsonResponse(200, chunkBody(1, { chunk_id: "c-1x", text: "Next passage", neighbors: { prev_chunk_id: "c-1" } })),
    });
    const links = await screen.findAllByTestId("source-link");
    await userEvent.click(links[0]);
    await screen.findByText("Passage text 1");
    expect((screen.getByTestId("panel-prev") as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(screen.getByTestId("panel-next"));
    expect(await screen.findByText("Next passage")).toBeTruthy();
  });

  it("shows the version and a newer-version note when the document has moved on", async () => {
    mount({
      [`GET ${chunkPath(1)}`]: () => jsonResponse(200, chunkBody(1)),
      "GET /v1/orgs/o1/workspaces/w1/documents/d-1": () => jsonResponse(200, makeDoc({ id: "d-1", workspace_id: "w1", latest_version: makeVersion({ id: "v-new", document_id: "d-1", version_number: 3 }) })),
    });
    await userEvent.click((await screen.findAllByTestId("citation-chip"))[0]);
    const note = await screen.findByTestId("panel-newer-note");
    expect(note.textContent).toContain("version 3");
    expect(note.querySelector("a")?.getAttribute("href")).toBe("/o/acme/w/notes/documents/d-1?v=v-new");
  });

  it.each([
    [404, "not_found", "isn't available"],
    [410, "gone", "no longer available"],
    [503, "unavailable", "Temporarily unavailable"],
  ])("renders a calm %i state", async (status, code, copy) => {
    mount({ [`GET ${chunkPath(1)}`]: () => jsonResponse(status, { detail: "x" }) });
    await userEvent.click((await screen.findAllByTestId("citation-chip"))[0]);
    const err = await screen.findByTestId("panel-error");
    expect(err.getAttribute("data-code")).toBe(code);
    expect(err.textContent).toContain(copy);
    expect(screen.getByTestId("panel-open-document")).toBeTruthy(); // the way out stays available
  });

  it("restores the panel from ?cite= once the history has loaded", async () => {
    window.history.replaceState(null, "", "/o/acme/chat/t-1?cite=c-2");
    mount({ [`GET ${chunkPath(2)}`]: () => jsonResponse(200, chunkBody(2)) });
    expect(await screen.findByText("Passage text 2")).toBeTruthy();
    window.history.replaceState(null, "", "/");
  });

  it("writes ?cite= on selection", async () => {
    mount({ [`GET ${chunkPath(1)}`]: () => jsonResponse(200, chunkBody(1)) });
    await userEvent.click((await screen.findAllByTestId("citation-chip"))[0]);
    await waitFor(() => expect(window.location.search).toContain("cite=c-1"));
    window.history.replaceState(null, "", "/");
  });
});

describe("layout and announcements", () => {
  it("is a three-column grid from 1100px up and stacks below", () => {
    expect(CHAT_GRID_CLASS).toContain("min-[1100px]:grid-cols-[14rem_minmax(0,1fr)_345px]");
    expect(CHAT_GRID_CLASS).not.toMatch(/(^| )grid-cols-/); // no column template outside the breakpoint
    expect(PANEL_COLUMN_CLASS).toContain("min-[1100px]:sticky");
  });

  it("announces state changes once, never the streamed tokens", async () => {
    const running = detail({ messages: [msg({ id: "m1", role: "user", content: "Q", run_id: "r-1" })], active_run: { run_id: "r-1", state: "running" } });
    mount({}, running, [
      {
        frames: [
          frame(1, "started"),
          frame(2, "delta", { text: "Hello " }),
          frame(3, "delta", { text: "world" }),
          frame(4, "citations", { citations: [] }),
          frame(5, "completed", { message_id: "m-2", answer_text: "Hello world" }),
          END(),
        ],
      },
    ]);
    const status = await screen.findByTestId("chat-status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    const seen: string[] = [];
    new MutationObserver(() => seen.push(status.textContent ?? "")).observe(status, { childList: true, characterData: true, subtree: true });
    await waitFor(() => expect(status.textContent).toBe("Answer ready."));
    // Every text the region ever held is a state message; the streamed words never reach it.
    expect(seen.every((t) => ["Writing an answer…", "Answer ready.", ""].includes(t))).toBe(true);
    expect(status.textContent).not.toContain("Hello");
    expect(await screen.findByText("Hello world")).toBeTruthy();
    expect(status.contains(screen.getByText("Hello world"))).toBe(false);
  });

  it("verification banners keep their calm role and tone", () => {
    const { container } = renderWithQuery(<VerificationBanner verification={{ status: "abstained", withheld_count: 0, flagged_count: 0 }} />, { fetcher: vi.fn() });
    expect(container.querySelector("[role=note]")?.getAttribute("data-tone")).toBe("abstained");
  });
});
