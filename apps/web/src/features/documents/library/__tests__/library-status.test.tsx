// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, screen } from "@testing-library/react";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import type { IngestionSummary } from "../../ingestion";
import { summary } from "../../ingestion/__tests__/fixtures";
import { LIBRARY, makeDoc, nav, WS } from "../../__tests__/helpers";
import { Library, LIBRARY_POLL_MS } from "../library";

vi.mock("next/navigation", async () => (await import("../../__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
vi.mock("@/features/documents/upload", () => ({ DocumentUploader: () => null }));

beforeEach(() => {
  nav.reset();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const sum = (state: IngestionSummary["state"], stage: IngestionSummary["stage"] = null): IngestionSummary => summary({ state, stage });

describe("Library status badges", () => {
  it("polls the list while a row is active, updates badges live, and stops when all are terminal", async () => {
    const script: IngestionSummary[] = [sum("queued"), sum("processing", "embed"), sum("ready")];
    let n = 0;
    const { fetcher, calls } = fakeFetcher({
      [`GET ${WS}/documents`]: () => {
        const s = script[Math.min(n++, script.length - 1)];
        return jsonResponse(200, {
          items: [makeDoc({ id: "d-1", ingestion: s }), makeDoc({ id: "d-2", title: "Done", ingestion: sum("ready") }), makeDoc({ id: "d-3", title: "Old", ingestion: null })],
          next_cursor: null,
          can_upload: false,
        });
      },
    });
    const client = makeQueryClient();
    client.setDefaultOptions({ queries: { retry: false, staleTime: 0 } });
    renderWithQuery(<Library orgId="o1" workspaceId="w1" basePath={LIBRARY} />, { fetcher, client });

    const badges = () => screen.queryAllByTestId("ingestion-badge").map((b) => b.textContent);
    await vi.waitFor(() => expect(badges()).toEqual(["Queued", "Ready"])); // row without a summary has no badge
    await act(async () => void (await vi.advanceTimersByTimeAsync(LIBRARY_POLL_MS + 50)));
    await vi.waitFor(() => expect(badges()[0]).toBe("Processing (Preparing for search)"));
    await act(async () => void (await vi.advanceTimersByTimeAsync(LIBRARY_POLL_MS + 50)));
    await vi.waitFor(() => expect(badges()[0]).toBe("Ready"));
    const total = calls.length;
    await act(async () => void (await vi.advanceTimersByTimeAsync(LIBRARY_POLL_MS * 5)));
    expect(calls.length).toBe(total); // all terminal: no more polling
    expect(calls.filter((c) => c.path.includes("/events"))).toHaveLength(0); // never one stream per row
  });

  it("does not poll when nothing is active", async () => {
    const { fetcher, calls } = fakeFetcher({
      [`GET ${WS}/documents`]: () => jsonResponse(200, { items: [makeDoc({ ingestion: sum("failed") })], next_cursor: null, can_upload: false }),
    });
    const client = makeQueryClient();
    client.setDefaultOptions({ queries: { retry: false } });
    renderWithQuery(<Library orgId="o1" workspaceId="w1" basePath={LIBRARY} />, { fetcher, client });
    await vi.waitFor(() => expect(screen.getByTestId("ingestion-badge").textContent).toBe("Failed"));
    await act(async () => void (await vi.advanceTimersByTimeAsync(LIBRARY_POLL_MS * 4)));
    expect(calls).toHaveLength(1);
  });
});
