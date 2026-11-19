// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "./matchers";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import type { IngestionSummary } from "../api";
import { ProcessingPanel } from "../processing-panel";
import { IngestionBadge } from "../status-badge";

afterEach(cleanup);

const WS = "/v1/orgs/o1/workspaces/w1/documents/d-1";
const failed: IngestionSummary = { state: "failed", stage: "embed", updated_at: "2026-11-18T12:00:00Z", error_code: "index_unavailable" };
const status = (s: IngestionSummary) => ({ ...s, attempts: 1, max_attempts: 5, revision_id: "rev-secret-id", published_at: null });

function mount(initial: IngestionSummary | null, o: { canWrite?: boolean; retry?: () => Response; after?: IngestionSummary } = {}) {
  const { fetcher, calls } = fakeFetcher({
    [`POST ${WS}/ingestion/retry`]: () => (o.retry ? o.retry() : jsonResponse(202, { job_id: "j-1" })),
    [`GET ${WS}/ingestion`]: () => jsonResponse(200, status(o.after ?? { state: "queued", stage: null, updated_at: null, error_code: null })),
  });
  renderWithQuery(
    <ProcessingPanel orgId="o1" workspaceId="w1" documentId="d-1" versionRef="v-1" initial={initial} canWrite={o.canWrite ?? true} eventSource={null} pollMs={60_000} />,
    { fetcher },
  );
  return calls;
}

describe("IngestionBadge", () => {
  const cases: [IngestionSummary, string][] = [
    [{ state: "queued", stage: null, updated_at: null, error_code: null }, "Queued"],
    [{ state: "processing", stage: "chunk", updated_at: null, error_code: null }, "Processing (Splitting into passages)"],
    [{ state: "indexed", stage: null, updated_at: null, error_code: null }, "Ready"],
    [{ state: "failed", stage: null, updated_at: null, error_code: "x" }, "Failed"],
    [{ state: "unsupported", stage: null, updated_at: null, error_code: "unsupported_type" }, "Not supported"],
    [{ state: "failed", stage: null, updated_at: null, error_code: "no_text_extracted" }, "No text found"],
    [{ state: "cancelled", stage: null, updated_at: null, error_code: null }, "Cancelled"],
    [{ state: "none", stage: null, updated_at: null, error_code: null }, "Not processed"],
  ];
  it.each(cases)("renders %j as text %s", (summary, text) => {
    render(<IngestionBadge summary={summary} />);
    expect(screen.getByTestId("ingestion-badge")).toHaveTextContent(text);
  });
  it("renders nothing without a summary", () => {
    const { container } = render(<IngestionBadge summary={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ProcessingPanel", () => {
  it("shows a safe failure message, never the raw code or internals", () => {
    mount({ ...failed, error_code: "weird <b>Traceback</b> at /srv/app.py:12" });
    const panel = screen.getByTestId("processing-panel");
    expect(within(panel).getByTestId("processing-failure")).toHaveTextContent("Something went wrong while processing this file");
    expect(panel.textContent).not.toMatch(/Traceback|app\.py|rev-secret/);
    expect(within(panel).getByRole("button", { name: "Retry" })).toBeEnabled();
  });

  it("shows stage progress with text, not colour alone", () => {
    mount({ state: "processing", stage: "embed", updated_at: null, error_code: null });
    const steps = within(screen.getByTestId("processing-steps")).getAllByRole("listitem");
    expect(steps.map((s) => s.getAttribute("data-status"))).toEqual(["done", "done", "current", "todo", "todo"]);
    expect(steps[2]).toHaveAttribute("aria-current", "step");
    expect(steps[2]).toHaveTextContent("Embed (in progress)");
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("announces state in a polite live region", () => {
    mount({ state: "indexed", stage: null, updated_at: null, error_code: null });
    const live = screen.getByTestId("processing-live");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent("Processing finished");
  });

  it("hides Retry from users who may not write", () => {
    mount(failed, { canWrite: false });
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    expect(screen.getByTestId("processing-failure")).toBeInTheDocument();
  });

  it("retry success: disabled while in flight, then the state follows and a result is announced", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const { fetcher, calls } = fakeFetcher({
      [`POST ${WS}/ingestion/retry`]: async () => {
        await gate;
        return jsonResponse(202, { job_id: "j-1" });
      },
      [`GET ${WS}/ingestion`]: () => jsonResponse(200, status({ state: "queued", stage: null, updated_at: null, error_code: null })),
    });
    renderWithQuery(<ProcessingPanel orgId="o1" workspaceId="w1" documentId="d-1" versionRef="v-1" initial={failed} canWrite eventSource={null} pollMs={60_000} />, { fetcher });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    const busy = screen.getByRole("button", { name: /retrying/i });
    expect(busy).toBeDisabled();
    await user.click(busy); // a second click does nothing
    release();
    await waitFor(() => expect(screen.getByTestId("retry-result")).toHaveTextContent("Processing restarted"));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
    await waitFor(() => expect(screen.getByTestId("ingestion-badge")).toHaveTextContent("Queued"));
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("retry 409: calm message that it is already running", async () => {
    mount(failed, { retry: () => jsonResponse(409, { detail: "conflict" }), after: { state: "processing", stage: "extract", updated_at: null, error_code: null } });
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByTestId("retry-result")).toHaveTextContent("already being processed"));
    await waitFor(() => expect(screen.getByTestId("ingestion-badge")).toHaveTextContent("Processing"));
  });

  it("retry 403 and 404: calm message, the failed state and the button stay", async () => {
    mount(failed, { retry: () => jsonResponse(403, { detail: "forbidden" }) });
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByTestId("retry-result")).toHaveTextContent("don't have permission"));
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    cleanup();
    mount(failed, { retry: () => jsonResponse(404, { detail: "not_found" }) });
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByTestId("retry-result")).toHaveTextContent("no longer available"));
  });

  it("retry on a server error shows a generic message without the code", async () => {
    mount(failed, { retry: () => jsonResponse(500, { detail: "db_exploded_at_row_17" }) });
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByTestId("retry-result")).toHaveTextContent("couldn't restart processing"));
    expect(document.body.textContent).not.toContain("db_exploded");
  });

  it("offers 'Process now' for a document that was never processed, and is keyboard operable", async () => {
    const calls = mount({ state: "none", stage: null, updated_at: null, error_code: null });
    const user = userEvent.setup();
    await user.tab();
    expect(screen.getByRole("button", { name: "Process now" })).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
  });

  it("is quiet (no crash, no raw data) when the API sent no summary", () => {
    mount(null);
    expect(screen.getByTestId("processing-panel")).toHaveTextContent("isn't available right now");
  });
});

describe("timeline", () => {
  it("lists events newest first with timestamps (from the status body)", async () => {
    const { fetcher } = fakeFetcher({
      [`GET ${WS}/ingestion`]: () =>
        jsonResponse(200, {
          ...status({ state: "processing", stage: "chunk", updated_at: "2026-11-18T12:00:05Z", error_code: null }),
          events: [
            { seq: 1, state: "queued", stage: null, error_code: null, at: "2026-11-18T12:00:00Z" },
            { seq: 2, state: "processing", stage: "extract", error_code: null, at: "2026-11-18T12:00:02Z" },
            { seq: 3, state: "processing", stage: "chunk", error_code: null, at: "2026-11-18T12:00:05Z" },
          ],
        }),
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderWithQuery(
      <ProcessingPanel orgId="o1" workspaceId="w1" documentId="d-1" versionRef="v-1" initial={{ state: "queued", stage: null, updated_at: null, error_code: null }} canWrite eventSource={null} pollMs={1000} />,
      { fetcher },
    );
    await vi.advanceTimersByTimeAsync(1100);
    const items = await screen.findAllByText(/Processing \(|Queued/, { selector: "[data-testid=processing-timeline] span" });
    expect(items.map((i) => i.textContent)).toEqual(["Processing (Splitting into passages)", "Processing (Extracting text)", "Queued"]);
    expect(screen.getByText("2026-11-18 12:00:05 UTC")).toBeInTheDocument();
    vi.useRealTimers();
  });
});
