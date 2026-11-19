// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { LIBRARY, makeDoc, makeVersion, nav, WS } from "../../__tests__/helpers";
import { DocumentViewer } from "../viewer";

vi.mock("next/navigation", async () => (await import("../../__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
vi.mock("@/features/documents/upload", () => ({ DocumentUploader: () => null }));

beforeEach(() => nav.reset(`${LIBRARY}/d-1`));
afterEach(cleanup);

function mount(write: boolean) {
  const v = makeVersion();
  const doc = makeDoc({
    permissions: { write, manage: false },
    ingestion: { state: "failed", stage: "extract", updated_at: "2026-11-18T12:00:00Z", error_code: "pdf_unreadable" },
  });
  const { fetcher } = fakeFetcher({
    [`GET ${WS}/documents/d-1/versions/v-1/content-url`]: () =>
      jsonResponse(200, { url: "https://store.invalid/x", expires_at: new Date(Date.now() + 300_000).toISOString(), filename: "report.pdf", content_type: "application/pdf", disposition: "inline" }),
  });
  renderWithQuery(<DocumentViewer orgId="o1" workspaceId="w1" libraryPath={LIBRARY} document={doc} versions={[v]} selected={v} />, { fetcher });
}

describe("viewer processing panel", () => {
  it("shows the failure in plain words, and Retry to a writer", () => {
    mount(true);
    const panel = screen.getByTestId("processing-panel");
    expect(panel.textContent).toContain("This PDF couldn't be read");
    expect(panel.textContent).not.toContain("pdf_unreadable");
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
  it("hides Retry from a read-only user", () => {
    mount(false);
    expect(screen.getByTestId("processing-failure")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});
