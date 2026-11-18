// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import type { Document, DocumentList } from "../../types";
import { LIBRARY, makeDoc, makeVersion, nav, navigationModule, WS } from "../../__tests__/helpers";
import { Library } from "../library";
import { paramsKey, DEFAULT_STATE } from "../url-state";

vi.mock("next/navigation", async () => (await import("../../__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
// The uploader has its own suite; here it is a stand-in that reports one finished upload on click.
vi.mock("@/features/documents/upload", () => ({
  DocumentUploader: ({ onUploaded, disabled }: { onUploaded?: (r: unknown) => void; disabled?: boolean }) => (
    <button data-testid="uploader" disabled={disabled} onClick={() => onUploaded?.({ document: { id: "d-new" }, version: { id: "v-new" } })}>
      uploader
    </button>
  ),
}));

void navigationModule;

beforeEach(() => nav.reset());
afterEach(cleanup);

const list = (items: Document[], over: Partial<DocumentList> = {}): DocumentList => ({ items, next_cursor: null, can_upload: true, ...over });
const DOCS = [
  makeDoc({ id: "d-1", title: "Quarterly report" }),
  makeDoc({ id: "d-2", title: "Meeting notes", latest_version: makeVersion({ document_id: "d-2", original_filename: "notes.txt", content_type: "text/plain", size_bytes: 12 }), version_count: 3 }),
];

function mount(handler: (url: URL, init?: RequestInit) => Response | Promise<Response>, o: { initial?: DocumentList } = {}) {
  const { fetcher, calls } = fakeFetcher({
    [`GET ${WS}/documents`]: (init, url) => handler(url, init),
    [`PATCH ${WS}/documents/d-1`]: async (init) => jsonResponse(200, makeDoc({ id: "d-1", title: JSON.parse(String(init?.body)).title })),
    [`DELETE ${WS}/documents/d-1`]: () => jsonResponse(202, { status: "deletion_requested", purge_after: "2026-12-19T00:00:00Z" }),
  });
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: 30_000 }, mutations: { retry: false } });
  const utils = renderWithQuery(
    <Library
      orgId="o1"
      workspaceId="w1"
      basePath={LIBRARY}
      initial={o.initial ? { key: paramsKey(DEFAULT_STATE), data: o.initial } : undefined}
    />,
    { fetcher, client },
  );
  const listCalls = () => calls.filter((c) => c.method === "GET");
  return { ...utils, calls, listCalls };
}

describe("Library table states", () => {
  it("renders the server-provided first page without fetching again", () => {
    const m = mount(() => jsonResponse(200, list([])), { initial: list(DOCS) });
    expect(screen.getAllByTestId("document-row")).toHaveLength(2);
    expect(m.listCalls()).toHaveLength(0);
    const row = screen.getAllByTestId("document-row")[1];
    expect(within(row).getByRole("link", { name: "Meeting notes" }).getAttribute("href")).toBe(`${LIBRARY}/d-2`);
    expect(row.textContent).toContain("Text");
    expect(row.textContent).toContain("12 B");
    expect(row.textContent).toContain("2026-11-12");
    expect(within(row).getAllByRole("cell").some((c) => c.textContent === "3")).toBe(true);
  });

  it("shows loading skeletons, then the rows, and announces the count", async () => {
    mount(() => jsonResponse(200, list(DOCS)));
    expect(screen.getByTestId("documents-loading")).toBeTruthy();
    await screen.findByTestId("documents-table");
    expect(screen.getByTestId("results-live").textContent).toContain("2 documents, page 1.");
  });

  it("empty state: invites an upload only when the caller can upload", async () => {
    const a = mount(() => jsonResponse(200, list([])));
    expect((await screen.findByTestId("documents-empty")).textContent).toContain("No documents yet");
    expect(screen.getByTestId("documents-empty").textContent).toContain("upload area");
    expect(screen.getByTestId("uploader")).toBeTruthy();
    a.unmount();
    cleanup();
    mount(() => jsonResponse(200, list([], { can_upload: false })));
    await screen.findByTestId("documents-empty");
    expect(screen.getByTestId("documents-empty").textContent).not.toContain("upload area");
    expect(screen.queryByTestId("uploader")).toBeNull();
  });

  it("no-results state with a Clear button that restores the unfiltered view", async () => {
    nav.reset(`${LIBRARY}?q=zzz&type=pdf`);
    const m = mount((url) => jsonResponse(200, url.searchParams.get("q") ? list([]) : list(DOCS)));
    await screen.findByTestId("documents-no-results");
    expect(m.listCalls()[0].path).toBe(`${WS}/documents?sort=updated_at&order=desc&limit=25&q=zzz&content_type=application%2Fpdf`);
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await screen.findByTestId("documents-table");
    expect(nav.current()).toBe(LIBRARY);
    expect((screen.getByLabelText("Search") as HTMLInputElement).value).toBe("");
  });

  it("error state with Retry", async () => {
    let fail = true;
    const m = mount(() => (fail ? jsonResponse(500, { detail: "boom" }) : jsonResponse(200, list(DOCS))));
    await screen.findByTestId("documents-error");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByTestId("documents-table");
    expect(m.listCalls()).toHaveLength(2);
  });

  it("sortable headers announce aria-sort and toggle through the URL", async () => {
    mount(() => jsonResponse(200, list(DOCS)));
    await screen.findByTestId("documents-table");
    const updated = screen.getByRole("columnheader", { name: /Updated/ });
    const title = screen.getByRole("columnheader", { name: /Title/ });
    expect(updated.getAttribute("aria-sort")).toBe("descending");
    expect(title.getAttribute("aria-sort")).toBe("none");
    await userEvent.click(within(title).getByRole("button"));
    expect(nav.current()).toBe(`${LIBRARY}?sort=title`);
    await waitFor(() => expect(screen.getByRole("columnheader", { name: /Title/ }).getAttribute("aria-sort")).toBe("ascending"));
    await userEvent.click(within(screen.getByRole("columnheader", { name: /Title/ })).getByRole("button"));
    expect(nav.current()).toBe(`${LIBRARY}?sort=title&order=desc`);
    expect(screen.getByRole("columnheader", { name: /Title/ }).getAttribute("aria-sort")).toBe("descending");
  });

  it("row actions are gated by the document's permissions", async () => {
    mount(() =>
      jsonResponse(
        200,
        list([
          makeDoc({ id: "d-1", title: "Full", permissions: { write: true, manage: true } }),
          makeDoc({ id: "d-2", title: "Writer", permissions: { write: true, manage: false } }),
          makeDoc({ id: "d-3", title: "Reader", permissions: { write: false, manage: false } }),
        ]),
      ),
    );
    await screen.findByTestId("documents-table");
    for (const t of ["Full", "Writer", "Reader"]) expect(screen.getByRole("link", { name: `Open ${t}` })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Rename Full" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Delete Full" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Rename Writer" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete Writer" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Rename Reader|Delete Reader/ })).toBeNull();
  });
});

describe("URL is the single source of truth", () => {
  it("debounces the search box into ?q= (replace, not push) and resets the cursor", async () => {
    nav.reset(`${LIBRARY}?cursor=c2&type=pdf`);
    const m = mount((url) => (url.searchParams.get("q") === "abc" ? jsonResponse(200, list([DOCS[0]])) : jsonResponse(200, list(DOCS, { next_cursor: "c3" }))));
    await screen.findByTestId("documents-table");
    const input = screen.getByLabelText("Search");
    await userEvent.type(input, "abc");
    expect(nav.replaces).toHaveLength(0); // still inside the debounce window
    await waitFor(() => expect(nav.current()).toBe(`${LIBRARY}?q=abc&type=pdf`));
    expect(nav.replaces).toHaveLength(1);
    expect(nav.pushes).toHaveLength(0);
    await waitFor(() => expect(screen.getAllByTestId("document-row")).toHaveLength(1));
    const last = m.listCalls().at(-1)!.path;
    expect(last).toContain("q=abc");
    expect(last).not.toContain("cursor");
  });

  it("type, sort and order selects write to the URL and the request", async () => {
    const m = mount(() => jsonResponse(200, list(DOCS)));
    await screen.findByTestId("documents-table");
    await userEvent.selectOptions(screen.getByLabelText("File type"), "image");
    expect(nav.current()).toBe(`${LIBRARY}?type=image`);
    await userEvent.selectOptions(screen.getByLabelText("Sort by"), "title");
    expect(nav.current()).toBe(`${LIBRARY}?type=image&sort=title`);
    await userEvent.selectOptions(screen.getByLabelText("Order"), "desc");
    expect(nav.current()).toBe(`${LIBRARY}?type=image&sort=title&order=desc`);
    await waitFor(() => expect(m.listCalls().at(-1)!.path).toBe(`${WS}/documents?sort=title&order=desc&limit=25&content_type=image%2F`));
  });

  it("the controls follow the URL, including back/forward and externally changed links", async () => {
    mount(() => jsonResponse(200, list(DOCS)));
    await screen.findByTestId("documents-table");
    await userEvent.selectOptions(screen.getByLabelText("File type"), "pdf");
    expect((screen.getByLabelText("File type") as HTMLSelectElement).value).toBe("pdf");
    act(() => nav.back());
    expect((screen.getByLabelText("File type") as HTMLSelectElement).value).toBe("all");
    act(() => nav.forward());
    expect((screen.getByLabelText("File type") as HTMLSelectElement).value).toBe("pdf");
    act(() => nav.push(`${LIBRARY}?q=zeta&sort=created_at&order=asc`));
    expect((screen.getByLabelText("Search") as HTMLInputElement).value).toBe("zeta");
    expect((screen.getByLabelText("Sort by") as HTMLSelectElement).value).toBe("created_at");
    expect((screen.getByLabelText("Order") as HTMLSelectElement).value).toBe("asc");
    expect((screen.getByLabelText("File type") as HTMLSelectElement).value).toBe("all");
  });

  it("a shared/reloaded URL reproduces the same request", async () => {
    nav.reset(`${LIBRARY}?q=notes&type=text&sort=title&cursor=c5&prev=c2,c3`);
    const m = mount(() => jsonResponse(200, list(DOCS)));
    await screen.findByTestId("documents-table");
    expect(m.listCalls()[0].path).toBe(`${WS}/documents?sort=title&order=asc&limit=25&q=notes&content_type=text%2F&cursor=c5`);
    expect(screen.getByText("Page 4")).toBeTruthy();
  });

  it("keyset pagination: Next pushes the cursor, Previous walks the stack back", async () => {
    const pages: Record<string, DocumentList> = {
      "": list([makeDoc({ id: "d-1", title: "P1" })], { next_cursor: "c2" }),
      c2: list([makeDoc({ id: "d-2", title: "P2" })], { next_cursor: "c3" }),
      c3: list([makeDoc({ id: "d-3", title: "P3" })]),
    };
    mount((url) => jsonResponse(200, pages[url.searchParams.get("cursor") ?? ""]));
    await screen.findByRole("link", { name: "P1" });
    const prev = screen.getByRole("button", { name: "Previous" });
    expect(prev.hasAttribute("disabled")).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByRole("link", { name: "P2" });
    expect(nav.current()).toBe(`${LIBRARY}?cursor=c2`);
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByRole("link", { name: "P3" });
    expect(nav.current()).toBe(`${LIBRARY}?cursor=c3&prev=c2`);
    expect(screen.getByRole("button", { name: "Next" }).hasAttribute("disabled")).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    await screen.findByRole("link", { name: "P2" });
    expect(nav.current()).toBe(`${LIBRARY}?cursor=c2`);
    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    await screen.findByRole("link", { name: "P1" });
    expect(nav.current()).toBe(LIBRARY);
  });

  it("an invalid cursor falls back to page 1 with a calm message", async () => {
    nav.reset(`${LIBRARY}?cursor=stale`);
    mount((url) =>
      url.searchParams.get("cursor") ? jsonResponse(422, { detail: "invalid_cursor" }) : jsonResponse(200, list(DOCS)),
    );
    await screen.findByTestId("documents-table");
    expect(nav.current()).toBe(LIBRARY);
    expect(nav.replaces).toEqual([LIBRARY]);
    expect(screen.getByTestId("cursor-notice").textContent).toContain("back to the first page");
    expect(screen.queryByTestId("documents-error")).toBeNull();
  });
});

describe("rename, delete and upload", () => {
  it("renames through a dialog, refreshes the list and announces it", async () => {
    let title = "Quarterly report";
    const m = mount(() => jsonResponse(200, list([makeDoc({ id: "d-1", title })])));
    await screen.findByRole("link", { name: "Quarterly report" });
    await userEvent.click(screen.getByRole("button", { name: "Rename Quarterly report" }));
    const dialog = screen.getByRole("dialog", { name: "Rename document" });
    const input = within(dialog).getByLabelText("Title") as HTMLInputElement;
    expect(document.activeElement).toBe(input);
    title = "Annual report";
    await userEvent.clear(input);
    await userEvent.type(input, "Annual report");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(JSON.parse(String(m.calls.find((c) => c.method === "PATCH")!.body))).toEqual({ title: "Annual report" });
    await screen.findByRole("link", { name: "Annual report" });
    expect(screen.getByTestId("results-live").textContent).toContain("Renamed to “Annual report”.");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("link", { name: "Annual report" })));
  });

  it("rename dialog: Escape cancels and returns focus to the trigger; blank titles cannot be saved", async () => {
    mount(() => jsonResponse(200, list([DOCS[0]])));
    await screen.findByTestId("documents-table");
    const trigger = screen.getByRole("button", { name: "Rename Quarterly report" });
    trigger.focus();
    await userEvent.keyboard("{Enter}");
    const dialog = screen.getByRole("dialog");
    await userEvent.clear(within(dialog).getByLabelText("Title"));
    expect(within(dialog).getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(true);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("deletes only after confirmation, removes the row at once and moves focus to the results", async () => {
    let docs = [DOCS[0], DOCS[1]];
    const m = mount(() => jsonResponse(200, list(docs)));
    await screen.findByTestId("documents-table");
    await userEvent.click(screen.getByRole("button", { name: "Delete Quarterly report" }));
    const dialog = screen.getByRole("dialog", { name: "Delete this document?" });
    expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Cancel" })); // safe default
    expect(m.calls.some((c) => c.method === "DELETE")).toBe(false);
    docs = [DOCS[1]];
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(m.calls.some((c) => c.method === "DELETE" && c.path === `${WS}/documents/d-1`)).toBe(true);
    expect(screen.queryByRole("link", { name: "Quarterly report" })).toBeNull();
    expect(screen.getByTestId("results-live").textContent).toContain("Deleted “Quarterly report”.");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Document results" })));
  });

  it("a failed delete keeps the dialog open with an error", async () => {
    const { fetcher } = fakeFetcher({
      [`GET ${WS}/documents`]: () => jsonResponse(200, list([DOCS[0]])),
      [`DELETE ${WS}/documents/d-1`]: () => jsonResponse(403, { detail: "forbidden" }),
    });
    renderWithQuery(<Library orgId="o1" workspaceId="w1" basePath={LIBRARY} />, { fetcher });
    await userEvent.click(await screen.findByRole("button", { name: "Delete Quarterly report" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    await screen.findByText("You don't have permission to do that.");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("shows the uploader only when can_upload is true", async () => {
    const m = mount(() => jsonResponse(200, list(DOCS, { can_upload: false })));
    await screen.findByTestId("documents-table");
    expect(screen.queryByTestId("uploader")).toBeNull();
    m.unmount();
    cleanup();
    mount(() => jsonResponse(200, list(DOCS, { can_upload: true })));
    expect(await screen.findByTestId("uploader")).toBeTruthy();
  });

  it("after an upload the list is refreshed and the new row appears (filters/cursor are cleared)", async () => {
    nav.reset(`${LIBRARY}?q=old&cursor=c2`);
    let docs = [DOCS[0]];
    const m = mount((url) => jsonResponse(200, url.searchParams.get("q") ? list([DOCS[0]]) : list(docs)));
    await screen.findByTestId("documents-table");
    docs = [makeDoc({ id: "d-new", title: "Fresh upload" }), DOCS[0]];
    await userEvent.click(screen.getByTestId("uploader"));
    expect(nav.refresh).toHaveBeenCalled();
    await screen.findByRole("link", { name: "Fresh upload" });
    expect(nav.current()).toBe(LIBRARY);
    expect(m.listCalls().length).toBeGreaterThanOrEqual(2);
  });
});
