// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import type { Document, Version } from "../../types";
import { LIBRARY, makeDoc, makeVersion, nav, WS } from "../../__tests__/helpers";
import { DocumentViewer } from "../viewer";
import { readCapped } from "../preview";
import { previewKind } from "../preview-kind";

vi.mock("next/navigation", async () => (await import("../../__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
vi.mock("@/features/documents/upload", () => ({
  DocumentUploader: ({ documentId, onUploaded }: { documentId?: string; onUploaded?: (r: unknown) => void }) => (
    <button data-testid="uploader" data-document-id={documentId} onClick={() => onUploaded?.({ document: { id: "d-1" }, version: { id: "v-new" } })}>
      uploader
    </button>
  ),
}));

const STORE = "https://store.invalid/obj";

beforeEach(() => nav.reset(`${LIBRARY}/d-1`));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function contentUrl(v: Version, over: Record<string, unknown> = {}, expiresInMs = 300_000) {
  return {
    url: `${STORE}/${v.id}?sig=1`,
    expires_at: new Date(Date.now() + expiresInMs).toISOString(),
    filename: v.original_filename,
    content_type: v.content_type,
    disposition: "inline",
    ...over,
  };
}

type Mount = {
  doc?: Document;
  versions?: Version[];
  selected?: Version | null;
  contentUrl?: (url: URL, n: number) => Response;
};

function mount(o: Mount = {}) {
  const v1 = makeVersion();
  const versions = o.versions ?? [v1];
  const selected = o.selected === undefined ? versions[0] : o.selected;
  const doc = o.doc ?? makeDoc({ latest_version: versions[0], version_count: versions.length });
  let n = 0;
  const { fetcher, calls } = fakeFetcher({
    [`GET ${WS}/documents/d-1/versions/${selected?.id ?? "x"}/content-url`]: (_i, url) =>
      (o.contentUrl ?? ((u) => jsonResponse(200, contentUrl(selected!, { disposition: u.searchParams.get("disposition") }))))(url, ++n),
    [`PATCH ${WS}/documents/d-1`]: async (init) => jsonResponse(200, makeDoc({ title: JSON.parse(String(init?.body)).title })),
    [`DELETE ${WS}/documents/d-1`]: () => jsonResponse(202, { status: "deletion_requested", purge_after: null }),
  });
  const client = makeQueryClient();
  const utils = renderWithQuery(
    <DocumentViewer orgId="o1" workspaceId="w1" libraryPath={LIBRARY} document={doc} versions={versions} selected={selected} />,
    { fetcher, client },
  );
  return { ...utils, calls, contentCalls: () => calls.filter((c) => c.path.includes("content-url")) };
}

describe("previewKind (contract §6 allow-list)", () => {
  it.each([
    ["application/pdf", "pdf"],
    ["image/png", "image"],
    ["image/jpeg", "image"],
    ["image/gif", "image"],
    ["image/webp", "image"],
    ["text/plain", "text"],
    ["text/plain; charset=utf-8", "text"],
    ["text/markdown", "text"],
    ["text/html", "download"],
    ["image/svg+xml", "download"],
    ["application/javascript", "download"],
    ["application/zip", "download"],
    ["application/octet-stream", "download"],
  ])("%s -> %s", (type, kind) => expect(previewKind(type)).toBe(kind));
});

describe("preview kinds", () => {
  it("PDF: asks for an inline URL and shows it in a sandboxed iframe", async () => {
    const m = mount();
    const frame = (await screen.findByTestId("preview-pdf")) as HTMLIFrameElement;
    expect(frame.tagName).toBe("IFRAME");
    expect(frame.getAttribute("src")).toBe(`${STORE}/v-1?sig=1`);
    expect(frame.hasAttribute("sandbox")).toBe(true);
    expect(frame.getAttribute("sandbox")).not.toContain("allow-top-navigation");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-popups");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-forms");
    expect(m.contentCalls()[0].path).toContain("content-url?disposition=inline");
  });

  it("image: renders an <img> with the title as alt text", async () => {
    const v = makeVersion({ original_filename: "chart.png", content_type: "image/png" });
    mount({ versions: [v] });
    const img = (await screen.findByTestId("preview-image")) as HTMLImageElement;
    expect(img.getAttribute("src")).toBe(`${STORE}/v-1?sig=1`);
    expect(img.alt).toBe("Quarterly report");
  });

  it("text: fetched and shown as plain text; markup and scripts are never interpreted", async () => {
    const hostile = '# Title\n<script>window.__pwned = 1</script>\n<img src=x onerror="window.__pwned = 2">\n<b>bold</b>';
    const fetchSpy = vi.fn(async () => new Response(hostile, { status: 200 }));
    vi.stubGlobal("fetch", fetchSpy);
    const v = makeVersion({ original_filename: "notes.md", content_type: "text/markdown", size_bytes: hostile.length });
    const m = mount({ versions: [v] });
    const pre = await screen.findByTestId("preview-text");
    expect(pre.tagName).toBe("PRE");
    expect(pre.textContent).toBe(hostile);
    expect(pre.querySelector("script, img, b")).toBeNull();
    expect(m.container.querySelector("script")).toBeNull();
    expect(m.container.querySelector("[data-testid='preview-region'] img")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(fetchSpy).toHaveBeenCalledWith(`${STORE}/v-1?sig=1`, expect.objectContaining({ credentials: "omit" }));
  });

  it("text over the cap is truncated with a notice", async () => {
    const big = "x".repeat(1024 * 1024 + 500);
    vi.stubGlobal("fetch", async () => new Response(big, { status: 200 }));
    const v = makeVersion({ content_type: "text/plain", original_filename: "big.txt", size_bytes: big.length });
    mount({ versions: [v] });
    const pre = await screen.findByTestId("preview-text");
    expect(pre.textContent!.length).toBe(1024 * 1024);
    expect(screen.getByTestId("preview-truncated").textContent).toContain("first 1 MB");
  });

  it("readCapped stops reading at the cap", async () => {
    const res = new Response("abcdefghij");
    expect(await readCapped(res, 4)).toEqual({ text: "abcd", truncated: true });
    expect(await readCapped(new Response("abc"), 4)).toEqual({ text: "abc", truncated: false });
  });

  it.each([
    ["application/zip", "bundle.zip"],
    ["text/html", "page.html"],
    ["image/svg+xml", "logo.svg"],
    ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "brief.docx"],
  ])("%s is a download card, with no inline content-url request", async (type, name) => {
    const v = makeVersion({ content_type: type, original_filename: name });
    const m = mount({ versions: [v] });
    const card = await screen.findByTestId("preview-download");
    expect(card.textContent).toContain(name);
    expect(screen.queryByTestId("preview-pdf")).toBeNull();
    expect(screen.queryByTestId("preview-text")).toBeNull();
    expect(m.contentCalls()).toHaveLength(0);
  });

  it("honours an `attachment` answer from the API even for an allow-listed type", async () => {
    mount({ contentUrl: () => jsonResponse(200, contentUrl(makeVersion(), { disposition: "attachment" })) });
    const card = await screen.findByTestId("preview-download");
    expect(card.textContent).toContain("Download it to view it");
    expect(screen.queryByTestId("preview-pdf")).toBeNull();
  });

  it("download asks for a fresh attachment URL on click", async () => {
    const v = makeVersion({ content_type: "application/zip", original_filename: "bundle.zip" });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.href).toBe(`${STORE}/v-1?sig=1`);
    });
    const m = mount({ versions: [v], contentUrl: (u) => jsonResponse(200, contentUrl(v, { disposition: u.searchParams.get("disposition") })) });
    await userEvent.click(within(await screen.findByTestId("preview-download")).getByRole("button", { name: "Download" }));
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(m.contentCalls()).toHaveLength(1);
    expect(m.contentCalls()[0].path).toContain("disposition=attachment");
  });
});

describe("unavailable and error states", () => {
  it("an unknown/other-document version shows the not-available state and requests nothing", () => {
    const m = mount({ selected: null, versions: [makeVersion()] });
    const box = screen.getByTestId("document-unavailable");
    expect(box.textContent).toContain("isn't available");
    expect(within(box).getByRole("link", { name: "View the latest version" }).getAttribute("href")).toBe(`${LIBRARY}/d-1`);
    expect(m.contentCalls()).toHaveLength(0);
    // no version details leak into the unavailable box
    expect(box.textContent).not.toContain("report.pdf");
  });

  it.each([404, 410, 403])("a %s from content-url shows not-available", async (status) => {
    mount({ contentUrl: () => jsonResponse(status, { detail: status === 403 ? "forbidden" : "not_found" }) });
    await screen.findByTestId("document-unavailable");
    expect(screen.queryByTestId("preview-pdf")).toBeNull();
    expect(screen.queryByRole("button", { name: "Download" })).toBeNull();
  });

  it("a 404 from the object store for a text file is not-available too", async () => {
    vi.stubGlobal("fetch", async () => new Response("gone", { status: 404 }));
    mount({ versions: [makeVersion({ content_type: "text/plain", original_filename: "a.txt", size_bytes: 4 })] });
    await screen.findByTestId("document-unavailable");
  });

  it("storage_unavailable shows a retry that asks again", async () => {
    let fail = true;
    const m = mount({
      contentUrl: () => (fail ? jsonResponse(503, { detail: "storage_unavailable" }) : jsonResponse(200, contentUrl(makeVersion()))),
    });
    await screen.findByTestId("preview-error");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByTestId("preview-pdf");
    expect(m.contentCalls()).toHaveLength(2);
  });

  it("a store 403 (expired signature) renews the URL once and then shows the text", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => {
      seen.push(url);
      return url.includes("sig=2") ? new Response("fresh text") : new Response("expired", { status: 403 });
    });
    const v = makeVersion({ content_type: "text/plain", original_filename: "a.txt", size_bytes: 10 });
    let n = 0;
    const m = mount({ versions: [v], contentUrl: () => jsonResponse(200, contentUrl(v, { url: `${STORE}/v-1?sig=${++n}` })) });
    expect((await screen.findByTestId("preview-text")).textContent).toBe("fresh text");
    expect(seen).toEqual([`${STORE}/v-1?sig=1`, `${STORE}/v-1?sig=2`]);
    expect(m.contentCalls()).toHaveLength(2);
  });

  it("repeated store 403s end in an error, not a loop", async () => {
    vi.stubGlobal("fetch", async () => new Response("nope", { status: 403 }));
    const v = makeVersion({ content_type: "text/plain", original_filename: "a.txt", size_bytes: 10 });
    let n = 0;
    const m = mount({ versions: [v], contentUrl: () => jsonResponse(200, contentUrl(v, { url: `${STORE}/v-1?sig=${++n}` })) });
    await screen.findByTestId("preview-error");
    expect(m.contentCalls().length).toBeLessThanOrEqual(3);
  });
});

describe("content URL lifetime", () => {
  it("re-requests the URL before it expires and never reuses an expired one", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-19T10:00:00Z"));
    const v = makeVersion();
    let n = 0;
    const m = mount({
      contentUrl: () => jsonResponse(200, { ...contentUrl(v, { url: `${STORE}/v-1?sig=${++n}` }, 60_000) }),
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(m.contentCalls()).toHaveLength(1);
    // The on-screen frame stays on the URL it already loaded (no reload while reading)...
    expect(screen.getByTestId("preview-pdf").getAttribute("src")).toBe(`${STORE}/v-1?sig=1`);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(40_000); // still 20s of life > 15s margin
    });
    expect(m.contentCalls()).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000); // inside the margin: renewed
    });
    expect(m.contentCalls()).toHaveLength(2);
    // ...and the renewed URL is scheduled for renewal in turn.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    expect(m.contentCalls()).toHaveLength(3);
  });

  it("an image whose URL expired falls over to the renewed URL when it fails to load", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-19T10:00:00Z"));
    const v = makeVersion({ content_type: "image/png", original_filename: "c.png" });
    let n = 0;
    mount({ versions: [v], contentUrl: () => jsonResponse(200, contentUrl(v, { url: `${STORE}/v-1?sig=${++n}` }, 60_000)) });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    const img = screen.getByTestId("preview-image") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe(`${STORE}/v-1?sig=1`);
    act(() => {
      img.dispatchEvent(new Event("error"));
    });
    expect(screen.getByTestId("preview-image").getAttribute("src")).toBe(`${STORE}/v-1?sig=2`);
  });
});

describe("header, versions and actions", () => {
  const v2 = makeVersion({ id: "v-2", version_number: 2, original_filename: "report-final.pdf", size_bytes: 5 * 1024 * 1024, sha256: "bcdef012".padEnd(64, "0"), created_at: "2026-11-15T09:00:00Z" });
  const v1 = makeVersion({ id: "v-1", version_number: 1, retention: { status: "superseded", purge_after: null } });
  const v2c = { ...v2, retention: { status: "current" as const, purge_after: null } };

  it("lists versions newest first with current/superseded, filename, size, date and sha prefix", async () => {
    mount({ versions: [v2c, v1], selected: v1 });
    const items = screen.getAllByTestId("version-item");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("Version 2");
    expect(items[0].textContent).toContain("Current");
    expect(items[0].textContent).toContain("report-final.pdf");
    expect(items[0].textContent).toContain("5 MB");
    expect(items[0].textContent).toContain("2026-11-15");
    expect(items[0].textContent).toContain("bcdef012");
    expect(items[0].textContent).not.toContain("bcdef0120");
    expect(items[1].textContent).toContain("Superseded");
    expect(items[0].getAttribute("href")).toBe(`${LIBRARY}/d-1?v=v-2`);
    expect(items[0].getAttribute("aria-current")).toBeNull();
    expect(items[1].getAttribute("aria-current")).toBe("true");
    expect(screen.getByTestId("selected-version-meta").textContent).toContain("Version 1");
    await screen.findByTestId("preview-pdf");
  });

  it("the preview follows the selected version", async () => {
    const m = mount({ versions: [v2c, v1], selected: v2c, contentUrl: () => jsonResponse(200, contentUrl(v2c)) });
    await screen.findByTestId("preview-pdf");
    expect(m.contentCalls()[0].path).toContain("/versions/v-2/content-url");
  });

  it("hides write/manage actions and the uploader without the permissions", async () => {
    const doc = makeDoc({ permissions: { write: false, manage: false } });
    mount({ doc });
    await screen.findByTestId("preview-pdf");
    expect(screen.queryByRole("button", { name: "Rename" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.queryByTestId("uploader")).toBeNull();
    expect(screen.getByRole("button", { name: "Download" })).toBeTruthy();
  });

  it("write-only callers get rename and upload but no delete", async () => {
    mount({ doc: makeDoc({ permissions: { write: true, manage: false } }) });
    await screen.findByTestId("preview-pdf");
    expect(screen.getByRole("button", { name: "Rename" })).toBeTruthy();
    expect(screen.getByTestId("uploader").getAttribute("data-document-id")).toBe("d-1");
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("uploading a new version opens it", async () => {
    mount();
    await userEvent.click(screen.getByTestId("uploader"));
    expect(nav.pushes).toEqual([`${LIBRARY}/d-1?v=v-new`]);
    expect(nav.refresh).toHaveBeenCalled();
  });

  it("rename saves, refreshes the page data and returns focus to the title", async () => {
    const m = mount();
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));
    const dialog = screen.getByRole("dialog", { name: "Rename document" });
    await userEvent.clear(within(dialog).getByLabelText("Title"));
    await userEvent.type(within(dialog).getByLabelText("Title"), "Renamed");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(JSON.parse(String(m.calls.find((c) => c.method === "PATCH")!.body))).toEqual({ title: "Renamed" });
    expect(nav.refresh).toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId("document-title")));
  });

  it("delete confirms, calls DELETE and returns to the library", async () => {
    const m = mount();
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(m.calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(nav.pushes).toEqual([LIBRARY]));
    expect(m.calls.some((c) => c.method === "DELETE")).toBe(true);
  });

  it("the Tab key stays inside an open dialog", async () => {
    mount();
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");
    for (let i = 0; i < 5; i++) {
      await userEvent.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });
});
