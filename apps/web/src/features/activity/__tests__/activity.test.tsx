// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ActivityApp, stepState } from "../activity-app";
import { deriveFromDocuments, parseJobsPage } from "../api";
import type { Document } from "@/features/documents/types";
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
const doc = (id: string, title: string, ingestion: unknown, updated = "2026-11-10T09:00:00Z") => ({
  id,
  org_id: "o",
  workspace_id: "w",
  title,
  created_by: null,
  created_at: updated,
  updated_at: updated,
  version_count: 1,
  latest_version: null,
  permissions: { write: true, manage: true },
  ingestion,
});
const docsBody = (items: unknown[]) => ({ items, next_cursor: null, can_upload: true });

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

  it("derives a list and counts from documents (skips unprocessed, newest first)", () => {
    const docs = [doc("a", "A", ing("ready"), "2026-11-01T00:00:00Z"), doc("b", "B", ing("failed", { updated_at: "2026-11-12T00:00:00Z" })), doc("c", "C", ing("none")), doc("d", "D", null)];
    const page = deriveFromDocuments(docs as unknown as Document[]);
    expect(page.items.map((i) => i.document_id)).toEqual(["b", "a"]);
    expect(page.counts).toMatchObject({ ready: 1, failed: 1, queued: 0 });
  });

  it("stepState marks the failing stage", () => {
    expect(stepState(0, { ...ing("failed", { stage: "chunk" }), attempts_label: null } as never)).toBe("done");
    expect(stepState(1, ing("failed", { stage: "chunk" }) as never)).toBe("stopped");
    expect(stepState(2, ing("failed", { stage: "chunk" }) as never)).toBe("waiting");
  });
});

describe("ActivityApp", () => {
  it("falls back to documents when the jobs route is 404, with an honest notice and counts", async () => {
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, {
      [`GET ${W}/documents`]: () => jsonResponse(200, docsBody([doc("a", "Alpha", ing("ready")), doc("b", "Beta", ing("failed", { error_code: "timeout" }))])),
    });
    expect(screen.getByRole("status", { name: /loading activity/i })).toBeTruthy();
    expect(await screen.findByTestId("activity-fallback")).toBeTruthy();
    expect(screen.getAllByTestId("job-row")).toHaveLength(2);
    expect(screen.getByTestId("activity-counts").textContent).toMatch(/Failed1/);
    await userEvent.click(screen.getByRole("button", { name: "Ready" }));
    expect(screen.getAllByTestId("job-row")).toHaveLength(1);
  });

  it("uses the jobs route when it exists (no fallback notice)", async () => {
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, {
      [`GET ${W}/ingestion/jobs`]: () =>
        jsonResponse(200, { items: [{ document_id: "d1", title: "Gamma", version_id: "v1", state: "processing", stage: "embed", error_code: null, message: null, updated_at: "2026-11-10T10:00:00Z" }], counts: { processing: 1 }, next_cursor: null }),
    });
    expect(await screen.findByText("Gamma")).toBeTruthy();
    expect(screen.queryByTestId("activity-fallback")).toBeNull();
  });

  it("empty and error states", async () => {
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${W}/documents`]: () => jsonResponse(200, docsBody([])) });
    expect(await screen.findByTestId("activity-empty")).toBeTruthy();
    cleanup();
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${W}/ingestion/jobs`]: () => jsonResponse(503, { detail: "x" }) });
    expect(await screen.findByTestId("activity-error")).toBeTruthy();
  });

  it("opens the detail drawer with checkpoints and events", async () => {
    mount(<ActivityApp org={orgWith("owner")} workspace={WS_ADMIN} />, {
      [`GET ${W}/documents`]: () => jsonResponse(200, docsBody([doc("a", "Alpha", ing("failed", { stage: "embed", error_code: "timeout" }))])),
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
