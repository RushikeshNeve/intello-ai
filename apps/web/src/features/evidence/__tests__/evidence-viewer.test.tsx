// @vitest-environment jsdom
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import { makeVersion, nav } from "@/features/documents/__tests__/helpers";
import { EvidenceViewer } from "../evidence-viewer";
import { HighlightedText } from "../highlighted-text";

vi.mock("next/navigation", async () => (await import("@/features/documents/__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

// The real preview has its own suite; here it stands in and shows what the evidence viewer hands it.
const previewProps = vi.hoisted(() => ({ last: null as null | { pdfPage?: number | null; renderText?: (t: string) => ReactNode; versionId: string } }));
vi.mock("@/features/documents/viewer/preview", () => ({
  Preview: (p: { version: { id: string }; pdfPage?: number | null; renderText?: (t: string) => ReactNode }) => {
    previewProps.last = { pdfPage: p.pdfPage, renderText: p.renderText, versionId: p.version.id };
    return (
      <div data-testid="preview-stub" data-page={p.pdfPage ?? ""}>
        {p.renderText ? p.renderText("Intro.\n\nRevenue grew by ten percent.\n\nOutro.") : null}
      </div>
    );
  },
}));

const CHUNK = "/v1/orgs/o1/workspaces/w1/documents/d-1/versions/v-1/chunks/c-2";
const EV = "/o/acme/w/notes/documents/d-1/evidence";

beforeEach(() => {
  nav.reset(`${EV}?v=v-1&chunk=c-2`);
  previewProps.last = null;
});
afterEach(cleanup);

const chunkBody = (over: Record<string, unknown> = {}) => ({
  chunk_id: "c-2",
  text: "Revenue grew by ten percent.",
  page_start: 5,
  page_end: 6,
  heading_path: ["Results", "Revenue"],
  locators: [],
  version_number: 1,
  title: "Quarterly report",
  neighbors: { prev_chunk_id: "c-1", next_chunk_id: "c-3" },
  ...over,
});

function mount(handler: () => Response, o: { chunkId?: string | null; page?: number | null; latest?: { id: string; version_number: number } | null; versionNumber?: number } = {}) {
  const { fetcher, calls } = fakeFetcher({ [`GET ${CHUNK}`]: handler });
  const client = makeQueryClient();
  const version = makeVersion({ id: "v-1", document_id: "d-1", version_number: o.versionNumber ?? 1 });
  const utils = renderWithQuery(
    <EvidenceViewer
      orgId="o1"
      orgSlug="acme"
      workspaceId="w1"
      workspaceRef="notes"
      document={{ id: "d-1", title: "Quarterly report" }}
      version={version}
      latest={o.latest === undefined ? { id: "v-1", version_number: 1 } : o.latest}
      chunkId={o.chunkId === undefined ? "c-2" : o.chunkId}
      page={o.page ?? null}
    />,
    { fetcher, client },
  );
  return { ...utils, calls };
}

describe("EvidenceViewer", () => {
  it("shows the exact passage, title, version and page range, and opens the PDF at the chunk's page", async () => {
    mount(() => jsonResponse(200, chunkBody()));
    expect((await screen.findByTestId("evidence-text")).textContent).toBe("Revenue grew by ten percent.");
    expect(screen.getByTestId("evidence-title").textContent).toBe("Quarterly report");
    expect(screen.getByTestId("evidence-meta").textContent).toBe("Version 1 · pages 5–6");
    expect(screen.getByTestId("evidence-headings").textContent).toBe("Results › Revenue");
    await waitFor(() => expect(screen.getByTestId("preview-stub").getAttribute("data-page")).toBe("5"));
  });

  it("highlights the excerpt inside the document text and scrolls it into view safely", async () => {
    mount(() => jsonResponse(200, chunkBody()));
    const mark = await screen.findByTestId("evidence-mark");
    expect(mark.textContent).toBe("Revenue grew by ten percent.");
    expect(screen.queryByTestId("excerpt-status")).toBeNull();
  });

  it("says so when the excerpt is not found in the file text", async () => {
    mount(() => jsonResponse(200, chunkBody({ text: "A passage that the preview text does not contain, at all, anywhere." })));
    expect((await screen.findByTestId("excerpt-status")).textContent).toContain("couldn't find this passage");
    expect(screen.queryByTestId("evidence-mark")).toBeNull();
  });

  it("previous/next navigate by chunk id and keep the version", async () => {
    mount(() => jsonResponse(200, chunkBody()));
    await userEvent.click(await screen.findByTestId("evidence-next"));
    expect(nav.pushes).toEqual([`${EV}?v=v-1&chunk=c-3`]);
    await userEvent.click(screen.getByTestId("evidence-prev"));
    expect(nav.pushes[1]).toBe(`${EV}?v=v-1&chunk=c-1`);
  });

  it("disables previous/next at the ends", async () => {
    mount(() => jsonResponse(200, chunkBody({ neighbors: { prev_chunk_id: null, next_chunk_id: null } })));
    await screen.findByTestId("evidence-text");
    expect(screen.getByTestId("evidence-prev").hasAttribute("disabled")).toBe(true);
    expect(screen.getByTestId("evidence-next").hasAttribute("disabled")).toBe(true);
  });

  it("404 is a calm not-available state without detail", async () => {
    mount(() => jsonResponse(404, { detail: "not_found" }));
    const err = await screen.findByTestId("evidence-error");
    expect(err.getAttribute("data-code")).toBe("not_found");
    expect(err.textContent).toContain("isn't available");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("410 explains that the source text is gone", async () => {
    mount(() => jsonResponse(410, { detail: "evidence_unavailable" }));
    const err = await screen.findByTestId("evidence-error");
    expect(err.getAttribute("data-code")).toBe("gone");
    expect(err.textContent).toContain("no longer available");
  });

  it("503 offers a retry that refetches", async () => {
    let n = 0;
    mount(() => (++n === 1 ? jsonResponse(503, { detail: "storage_unavailable" }) : jsonResponse(200, chunkBody())));
    await screen.findByTestId("evidence-error");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByTestId("evidence-text");
    expect(n).toBe(2);
  });

  it("notes when a newer version exists, linking to it, while still showing the cited version", async () => {
    mount(() => jsonResponse(200, chunkBody()), { latest: { id: "v-3", version_number: 3 }, versionNumber: 1 });
    const note = await screen.findByTestId("newer-version-note");
    expect(note.textContent).toContain("version 1");
    expect(note.textContent).toContain("version 3");
    expect(note.querySelector("a")?.getAttribute("href")).toBe("/o/acme/w/notes/documents/d-1?v=v-3");
    expect(previewProps.last?.versionId).toBe("v-1");
  });

  it("no note when this is the newest version", async () => {
    mount(() => jsonResponse(200, chunkBody()));
    await screen.findByTestId("evidence-text");
    expect(screen.queryByTestId("newer-version-note")).toBeNull();
  });

  it("without a chunk (a search hit with only a page) it opens the document at that page and makes no chunk request", async () => {
    const m = mount(() => jsonResponse(200, chunkBody()), { chunkId: null, page: 7 });
    expect((await screen.findByTestId("evidence-no-chunk")).textContent).toContain("page 7");
    expect(screen.getByTestId("preview-stub").getAttribute("data-page")).toBe("7");
    expect(m.calls).toHaveLength(0);
  });

  it("keyboard: passage text is focusable and the controls are real buttons", async () => {
    mount(() => jsonResponse(200, chunkBody()));
    const text = await screen.findByTestId("evidence-text");
    expect(text.getAttribute("tabindex")).toBe("0");
    expect(screen.getByTestId("evidence-next").tagName).toBe("BUTTON");
  });
});

describe("HighlightedText", () => {
  it("marks the located excerpt as a text node (no HTML)", () => {
    const { container } = render(<HighlightedText text={"a <b>x</b> hit here"} excerpt="hit here" />);
    expect(container.querySelector("mark")?.textContent).toBe("hit here");
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toBe("a <b>x</b> hit here");
  });
  it("renders the text unmarked and reports false when not found", () => {
    const onResolved = vi.fn();
    const { container } = render(<HighlightedText text="plain text" excerpt="something else entirely different" onResolved={onResolved} />);
    expect(container.querySelector("mark")).toBeNull();
    expect(onResolved).toHaveBeenCalledWith(false);
  });
});
