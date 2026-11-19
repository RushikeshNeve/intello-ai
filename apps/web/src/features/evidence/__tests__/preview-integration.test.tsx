// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeVersion } from "@/features/documents/__tests__/helpers";
import { Preview } from "@/features/documents/viewer/preview";
import type { Version } from "@/features/documents/types";
import { HighlightedText } from "../highlighted-text";

vi.mock("next/link", () => ({ default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a> }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const STORE = "https://store.invalid/obj";

function mount(version: Version, extra: { pdfPage?: number | null; renderText?: (t: string) => React.ReactNode }) {
  const { fetcher } = fakeFetcher({
    "GET /v1/orgs/o1/workspaces/w1/documents/d-1/versions/v-1/content-url": () =>
      jsonResponse(200, {
        url: `${STORE}/v-1?sig=1`,
        expires_at: new Date(Date.now() + 300_000).toISOString(),
        filename: version.original_filename,
        content_type: version.content_type,
        disposition: "inline",
      }),
  });
  return renderWithQuery(
    <Preview ids={{ fetcher, orgId: "o1", workspaceId: "w1", documentId: "d-1" }} version={version} title="Report" onGone={() => undefined} {...extra} />,
    { fetcher },
  );
}

describe("Preview used by the evidence viewer", () => {
  it("opens a PDF at the requested page (#page=n)", async () => {
    mount(makeVersion(), { pdfPage: 7 });
    const frame = await screen.findByTestId("preview-pdf");
    expect(frame.getAttribute("src")).toBe(`${STORE}/v-1?sig=1#page=7`);
  });

  it("a PDF without a page hint has no fragment (the plain document viewer is unchanged)", async () => {
    mount(makeVersion(), {});
    expect((await screen.findByTestId("preview-pdf")).getAttribute("src")).toBe(`${STORE}/v-1?sig=1`);
  });

  it("text: the excerpt is highlighted inside the <pre>, as text only", async () => {
    const text = "# Notes\n<script>window.__pwned=1</script>\n\nThe target sentence is here.\n\nTail.";
    vi.stubGlobal("fetch", async () => new Response(text, { status: 200 }));
    const v = makeVersion({ original_filename: "n.md", content_type: "text/markdown", size_bytes: text.length });
    mount(v, { renderText: (t) => <HighlightedText text={t} excerpt="The target sentence is here." /> });
    const pre = await screen.findByTestId("preview-text");
    const mark = await screen.findByTestId("evidence-mark");
    expect(pre.contains(mark)).toBe(true);
    expect(mark.textContent).toBe("The target sentence is here.");
    expect(pre.querySelector("script")).toBeNull();
    expect(pre.textContent).toBe(text);
  });

  it("text without renderText is the plain text, as before", async () => {
    vi.stubGlobal("fetch", async () => new Response("plain", { status: 200 }));
    mount(makeVersion({ original_filename: "n.txt", content_type: "text/plain", size_bytes: 5 }), {});
    expect((await screen.findByTestId("preview-text")).textContent).toBe("plain");
  });
});
