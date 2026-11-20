// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ActivityApp, stepState } from "../activity-app";
import { parseJobsPage } from "../api";
import { jsonResponse, mount, orgWith, W, WS_ADMIN } from "@/features/settings/__tests__/helpers";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
afterEach(cleanup);

const ing = (state: string, extra = {}) => ({ state, stage: null, attempts_label: null, error_code: null, message: null, updated_at: "2026-11-10T10:00:00Z", can_retry: false, ...extra });
const job = (id: string, title: string, state: string, extra = {}) => ({
  document_id: id,
  title,
  version_id: `v-${id}`,
  state,
  stage: null,
  error_code: null,
  message: null,
  attempts: 1,
  updated_at: "2026-11-10T10:00:00Z",
  ...extra,
});
const jobsBody = (items: unknown[], counts = {}, next_cursor: string | null = null) => ({ items, counts, next_cursor });

describe("activity api", () => {
  it("parses the stub contract", () => {
    const page = parseJobsPage({
      items: [{ document_id: "d1", title: "A", version_id: "v1", state: "failed", stage: "embed", error_code: "timeout", message: "m", updated_at: "t" }],
      counts: { failed: 1, ready: 4 },
      next_cursor: null,
    });
    expect(page.items[0].state).toBe("failed");
    expect(page.counts).toMatchObject({ failed: 1, ready: 4, queued: 0 });
    expect(() => parseJobsPage({ items: [{ document_id: "d", state: "bogus" }] })).toThrow();
  });

  it("stepState marks the failing stage", () => {
    expect(stepState(0, { ...ing("failed", { stage: "chunk" }), attempts_label: null } as never)).toBe("done");
    expect(stepState(1, ing("failed", { stage: "chunk" }) as never)).toBe("stopped");
    expect(stepState(2, ing("failed", { stage: "chunk" }) as never)).toBe("waiting");
  });
});

describe("ActivityApp", () => {
  it("lists jobs and counts from the jobs route and filters server-side", async () => {
    const urls: string[] = [];
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, {
      [`GET ${W}/ingestion/jobs`]: (_init, url) => {
        urls.push(url.search);
        const failedOnly = url.search.includes("state=failed");
        return jsonResponse(200, jobsBody(failedOnly ? [job("b", "Beta", "failed", { error_code: "timeout" })] : [job("a", "Alpha", "ready"), job("b", "Beta", "failed", { error_code: "timeout" })], { ready: 1, failed: 1 }));
      },
    });
    expect(screen.getByRole("status", { name: /loading activity/i })).toBeTruthy();
    expect(await screen.findByText("Alpha")).toBeTruthy();
    expect(screen.getAllByTestId("job-row")).toHaveLength(2);
    expect(screen.getByTestId("activity-counts").textContent).toMatch(/Failed1/);
    await userEvent.click(screen.getByRole("button", { name: "Failed" }));
    await waitFor(() => expect(screen.getAllByTestId("job-row")).toHaveLength(1));
    expect(urls.some((u) => u.includes("state=failed"))).toBe(true);
  });

  it("loads the next page with the cursor", async () => {
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, {
      [`GET ${W}/ingestion/jobs`]: (_init, url) =>
        jsonResponse(200, url.search.includes("cursor=c1") ? jobsBody([job("b", "Beta", "ready")], { ready: 2 }) : jobsBody([job("a", "Alpha", "ready")], { ready: 2 }, "c1")),
    });
    await userEvent.click(await screen.findByRole("button", { name: /load more/i }));
    expect(await screen.findByText("Beta")).toBeTruthy();
    expect(screen.getAllByTestId("job-row")).toHaveLength(2);
  });

  it("empty and error states (no invented numbers on error)", async () => {
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${W}/ingestion/jobs`]: () => jsonResponse(200, jobsBody([])) });
    expect(await screen.findByTestId("activity-empty")).toBeTruthy();
    cleanup();
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${W}/ingestion/jobs`]: () => jsonResponse(503, { detail: "x" }) });
    expect(await screen.findByTestId("activity-error")).toBeTruthy();
    expect(screen.queryByTestId("activity-counts")).toBeNull();
  });

  it("opens the detail drawer with checkpoints and events", async () => {
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, {
      [`GET ${W}/ingestion/jobs`]: () => jsonResponse(200, jobsBody([job("a", "Alpha", "failed", { stage: "embed", error_code: "timeout" })], { failed: 1 })),
      [`GET ${W}/documents/a/ingestion`]: () =>
        jsonResponse(200, { ...ing("failed", { stage: "embed", error_code: "timeout" }), document_id: "a", events: [{ id: 1, type: "queued", label: "Queued", stage: null, attempt: 1, at: "2026-11-10T09:00:00Z" }] }),
    });
    await userEvent.click(await screen.findByRole("button", { name: /details for alpha/i }));
    const dialog = screen.getByRole("dialog", { name: /alpha/i });
    expect(within(dialog).getByTestId("checkpoint-timeline")).toBeTruthy();
    await waitFor(() => expect(within(dialog).getByTestId("event-list")).toBeTruthy());
    expect(within(dialog).getByRole("link", { name: /open document/i }).getAttribute("href")).toBe(`/o/acme/w/notes/documents/a`);
  });
});
