// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import { nav } from "@/features/documents/__tests__/helpers";
import type { Workspace } from "@/features/workspaces/api";
import { SEARCH_DEBOUNCE_MS, SearchPage } from "../search-page";

vi.mock("next/navigation", async () => (await import("@/features/documents/__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const SEARCH = "/o/acme/search";
const workspaces: Workspace[] = [
  { id: "w1", org_id: "o1", slug: "notes", name: "Acme Notes", created_at: "", role: "admin" },
  { id: "w2", org_id: "o1", slug: "papers", name: "Acme Papers", created_at: "", role: "admin" },
];

const body = (over: Record<string, unknown> = {}) => ({
  query: "alpha",
  results: [
    {
      document_id: "d1",
      title: "Quarterly report",
      workspace_id: "w1",
      workspace_slug: "notes",
      content_type: "application/pdf",
      version_id: "v2",
      version_number: 2,
      score: 0.9,
      snippets: [
        { evidence_id: "e1", chunk_id: "c-1", text: "The alpha result <b>bold</b> was good", page_start: 3, page_end: 4, highlights: [[4, 9], [4, 7], [999, 1000]] },
        { evidence_id: "e2", text: "no page here", page_start: null, page_end: null, highlights: [] },
      ],
    },
  ],
  degraded: [],
  took_ms: 5,
  scope: { workspace_count: 2 },
  ...over,
});

beforeEach(() => {
  nav.reset(SEARCH);
  window.localStorage.clear();
});
afterEach(cleanup);

type Handler = (req: Record<string, unknown>, init?: RequestInit) => Response | Promise<Response>;

function mount(handler: Handler) {
  const api = fakeFetcher({ "POST /v1/orgs/o1/search": (init) => handler(JSON.parse(String(init?.body)), init) });
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: 30_000 } });
  const utils = renderWithQuery(<SearchPage orgId="o1" orgSlug="acme" initialWorkspaces={workspaces} />, { fetcher: api.fetcher, client });
  const posts = () => api.calls.filter((c) => c.method === "POST").map((c) => JSON.parse(c.body ?? "{}"));
  return { ...utils, api, posts };
}

describe("SearchPage", () => {
  it("idle: no request until there is a query", async () => {
    const m = mount(() => jsonResponse(200, body()));
    expect(screen.getByTestId("search-idle")).toBeTruthy();
    await new Promise((r) => setTimeout(r, 20));
    expect(m.posts()).toHaveLength(0);
  });

  it("debounces typing into the URL, then searches once with the right body", async () => {
    const m = mount(() => jsonResponse(200, body()));
    await userEvent.type(screen.getByLabelText("Search your documents"), "alpha");
    expect(nav.replaces).toHaveLength(0); // still debouncing
    await waitFor(() => expect(nav.current()).toBe(`${SEARCH}?q=alpha`), { timeout: SEARCH_DEBOUNCE_MS + 1500 });
    await screen.findByTestId("search-list");
    expect(m.posts()).toEqual([{ query: "alpha", limit: 20 }]);
  });

  it("renders grouped results: version badge, pages, safe highlights, links to the exact evidence", async () => {
    nav.reset(`${SEARCH}?q=alpha`);
    mount(() => jsonResponse(200, body()));
    const card = await screen.findByTestId("search-result");
    expect(within(card).getByTestId("version-badge").textContent).toBe("Version 2");
    expect(within(card).getByTestId("result-title").getAttribute("href")).toBe("/o/acme/w/notes/documents/d1?v=v2");
    const snippets = within(card).getAllByTestId("search-snippet");
    expect(snippets).toHaveLength(2);
    // highlight [4,9] overlaps [4,7]: one mark "alpha"; out-of-range range ignored; markup stays text
    expect(snippets[0].querySelectorAll("mark")).toHaveLength(1);
    expect(snippets[0].querySelector("mark")?.textContent).toBe("alpha");
    expect(snippets[0].querySelector("b")).toBeNull();
    expect(snippets[0].textContent).toContain("<b>bold</b>");
    expect(within(snippets[0]).getByTestId("snippet-page").textContent).toBe("pp. 3–4");
    const links = within(card).getAllByTestId("snippet-evidence-link");
    expect(links[0].getAttribute("href")).toBe("/o/acme/w/notes/documents/d1/evidence?v=v2&chunk=c-1&page=3");
    // a snippet without a chunk id still opens the viewer at its version (no chunk param)
    expect(links[1].getAttribute("href")).toBe("/o/acme/w/notes/documents/d1/evidence?v=v2");
    expect(screen.getByTestId("search-live").textContent).toBe("1 document, 2 passages.");
  });

  it("scope, filters and mode live in the URL and in the request", async () => {
    nav.reset(`${SEARCH}?q=alpha&ws=papers&type=pdf&mode=keyword`);
    const m = mount(() => jsonResponse(200, body()));
    await screen.findByTestId("search-list");
    expect(m.posts()).toEqual([{ query: "alpha", limit: 20, workspace_ids: ["w2"], content_types: ["application/pdf"], mode: "keyword" }]);
    expect((screen.getByLabelText("Where") as HTMLSelectElement).value).toBe("papers");
    expect(screen.getByRole("button", { name: "PDF" }).getAttribute("aria-pressed")).toBe("true");
    expect((screen.getByLabelText("Match by") as HTMLSelectElement).value).toBe("keyword");
  });

  it("changing scope/filters writes the URL, and the scope is remembered per org", async () => {
    nav.reset(`${SEARCH}?q=alpha`);
    mount(() => jsonResponse(200, body()));
    await screen.findByTestId("search-list");
    await userEvent.selectOptions(screen.getByLabelText("Where"), "notes");
    expect(nav.current()).toBe(`${SEARCH}?q=alpha&ws=notes`);
    await userEvent.click(screen.getByRole("button", { name: "Markdown" }));
    expect(nav.current()).toBe(`${SEARCH}?q=alpha&ws=notes&type=markdown`);
    expect(window.localStorage.getItem("ukos.search.scope.o1")).toBe("notes");
    await userEvent.selectOptions(screen.getByLabelText("Where"), "all");
    expect(window.localStorage.getItem("ukos.search.scope.o1")).toBe("all");
    expect(nav.current()).toBe(`${SEARCH}?q=alpha&type=markdown`);
  });

  it("restores the remembered scope when the URL has none (and never overrides one it has)", async () => {
    window.localStorage.setItem("ukos.search.scope.o1", "papers");
    nav.reset(SEARCH);
    mount(() => jsonResponse(200, body()));
    await waitFor(() => expect(nav.current()).toBe(`${SEARCH}?ws=papers`));
    cleanup();
    nav.reset(`${SEARCH}?ws=notes`);
    mount(() => jsonResponse(200, body()));
    await new Promise((r) => setTimeout(r, 30));
    expect(nav.current()).toBe(`${SEARCH}?ws=notes`);
  });

  it("works when localStorage throws", async () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    nav.reset(`${SEARCH}?q=alpha`);
    mount(() => jsonResponse(200, body()));
    await screen.findByTestId("search-list");
    spy.mockRestore();
  });

  it("an unknown workspace slug in a link searches everything and says so", async () => {
    nav.reset(`${SEARCH}?q=alpha&ws=ghost`);
    const m = mount(() => jsonResponse(200, body()));
    await screen.findByTestId("search-list");
    expect(screen.getByTestId("scope-unknown")).toBeTruthy();
    expect(m.posts()[0].workspace_ids).toBeUndefined();
  });

  it("shows the keyword-only note when embeddings are unavailable", async () => {
    nav.reset(`${SEARCH}?q=alpha`);
    mount(() => jsonResponse(200, body({ degraded: ["embedding_unavailable"] })));
    expect((await screen.findByTestId("search-degraded")).textContent).toContain("keyword matches only");
    expect(screen.getByTestId("search-list")).toBeTruthy();
  });

  it("empty results", async () => {
    nav.reset(`${SEARCH}?q=zzz`);
    mount(() => jsonResponse(200, body({ results: [] })));
    expect((await screen.findByTestId("search-empty")).textContent).toContain("No matching passages");
    expect(screen.getByTestId("search-live").textContent).toBe("No results.");
  });

  it("empty scope (no readable workspace) is a calm empty state, not an error", async () => {
    nav.reset(`${SEARCH}?q=zzz`);
    mount(() => jsonResponse(200, body({ results: [], scope: { workspace_count: 0 } })));
    expect((await screen.findByTestId("search-empty")).textContent).toContain("Nothing to search yet");
  });

  it("429 and 503 show calm errors with Try again", async () => {
    nav.reset(`${SEARCH}?q=alpha`);
    let n = 0;
    mount(() => {
      n += 1;
      return n === 1 ? jsonResponse(429, { detail: "rate_limited" }) : n === 2 ? jsonResponse(503, { detail: "search_unavailable" }) : jsonResponse(200, body());
    });
    const e1 = await screen.findByTestId("search-error");
    expect(e1.getAttribute("data-code")).toBe("rate_limited");
    expect(e1.textContent).toContain("very quickly");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByTestId("search-error").getAttribute("data-code")).toBe("unavailable"));
    expect(screen.getByTestId("search-error").textContent).toContain("temporarily unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByTestId("search-list");
  });

  it("cancels the in-flight request when the query changes", async () => {
    nav.reset(`${SEARCH}?q=first`);
    const signals: AbortSignal[] = [];
    mount((req, init) => {
      if (init?.signal) signals.push(init.signal);
      return new Promise<Response>((resolve) => setTimeout(() => resolve(jsonResponse(200, body({ query: String(req.query) }))), 150));
    });
    await waitFor(() => expect(signals).toHaveLength(1));
    act(() => nav.replace(`${SEARCH}?q=second`));
    await waitFor(() => expect(signals).toHaveLength(2));
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
    await screen.findByTestId("search-list");
  });

  it("Enter submits at once without waiting for the debounce", async () => {
    mount(() => jsonResponse(200, body()));
    await userEvent.type(screen.getByLabelText("Search your documents"), "beta{Enter}");
    expect(nav.current()).toBe(`${SEARCH}?q=beta`);
  });
});
