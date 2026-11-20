// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { SearchResultRow } from "../search-results";
import type { SearchResult } from "../api";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
afterEach(cleanup);

const result = {
  document_id: "d1",
  title: "Report",
  workspace_id: "w1",
  workspace_slug: "notes",
  content_type: "application/pdf",
  version_id: "v1",
  version_number: 3,
  score: 1,
  snippets: [{ evidence_id: "e1", chunk_id: "c1", text: "a <b>x</b> hit", page_start: 2, page_end: 2, highlights: [[2, 5]] }],
} as unknown as SearchResult;

describe("SearchResultRow", () => {
  it("shows type, workspace scope and version, and renders marks as text", () => {
    const { container } = render(
      <ul>
        <SearchResultRow result={result} orgSlug="acme" workspaceName="Notes" />
      </ul>,
    );
    expect(screen.getByTestId("file-type-badge").textContent).toBe("PDF");
    expect(screen.getByText("Workspace")).toBeTruthy();
    expect(screen.getByTestId("version-badge").textContent).toBe("Version 3");
    expect(container.querySelectorAll("mark")).toHaveLength(1);
    expect(container.querySelector("b")).toBeNull();
  });
});
