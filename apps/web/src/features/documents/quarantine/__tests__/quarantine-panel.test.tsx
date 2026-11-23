// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { QuarantinePanel } from "../quarantine-panel";

afterEach(cleanup);
const LIST = "GET /v1/orgs/o1/workspaces/w1/quarantine";
const item = { document_id: "d1", version_id: "v1", title: "Evil.txt", findings: 2, max_severity: "high", quarantined_at: "2026-11-23T10:00:00Z" };

describe("QuarantinePanel", () => {
  it("lists and releases", async () => {
    const { fetcher, calls } = fakeFetcher({
      [LIST]: () => jsonResponse(200, [item]),
      "POST /v1/orgs/o1/workspaces/w1/quarantine/d1/release": () => jsonResponse(200, { requeued: true }),
    });
    renderWithQuery(<QuarantinePanel orgId="o1" workspaceId="w1" />, { fetcher });
    await userEvent.click(await screen.findByRole("button", { name: "Release Evil.txt" }));
    await waitFor(() => expect(JSON.stringify(calls)).toContain("release"));
  });

  it("shows a calm message when not allowed", async () => {
    const { fetcher } = fakeFetcher({ [LIST]: () => jsonResponse(404, { error: { code: "not_found" } }) });
    renderWithQuery(<QuarantinePanel orgId="o1" workspaceId="w1" />, { fetcher });
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});
