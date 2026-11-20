// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IntegrationsApp } from "../integrations-app";
import { parseSourceList, parseGlobs, isValidRepo } from "../api";
import { jsonResponse, mount, orgWith, W, WS_ADMIN, WS_VIEWER } from "@/features/settings/__tests__/helpers";

afterEach(cleanup);

const src = (extra = {}) => ({
  id: "s1",
  workspace_id: WS_ADMIN.id,
  provider: "github",
  name: "acme/docs",
  config: { repo: "acme/docs", ref: "main", include: ["docs/**"] },
  state: "active",
  has_token: true,
  sync_interval_seconds: 3600,
  next_sync_at: null,
  created_at: "2026-11-01T00:00:00Z",
  revoked_at: null,
  last_sync: { id: "y1", state: "succeeded", trigger: "manual", error_code: null, stats: {}, created_at: "2026-11-01T00:00:00Z", started_at: null, finished_at: "2026-11-01T10:00:00Z" },
  items: { active: 12, deleted: 1 },
  ...extra,
});

describe("sources api", () => {
  it("parses a bare array and rejects junk", () => {
    expect(parseSourceList([src()])[0]).toMatchObject({ repo: "acme/docs", ref: "main", include: ["docs/**"], items: { active: 12, deleted: 1 } });
    expect(() => parseSourceList("x")).toThrow();
    expect(parseGlobs(" docs/**, *.md\n")).toEqual(["docs/**", "*.md"]);
    expect(isValidRepo("a/b")).toBe(true);
    expect(isValidRepo("nope")).toBe(false);
  });
});

describe("IntegrationsApp", () => {
  it("shows loading, then sources with status, and the honest catalog", async () => {
    mount(<IntegrationsApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${W}/sources`]: () => jsonResponse(200, [src()]) });
    expect(screen.getByRole("status", { name: /loading sources/i })).toBeTruthy();
    expect(await screen.findByText("acme/docs")).toBeTruthy();
    expect(screen.getByText("Last sync succeeded")).toBeTruthy();
    expect(screen.getByText(/Token stored encrypted/)).toBeTruthy();
    expect(screen.getByText("12 synced · 1 removed")).toBeTruthy();
    expect(screen.getByTestId("connector-catalog").textContent).toMatch(/Available/);
    expect(screen.getAllByText("Planned").length).toBeGreaterThan(1);
    expect(screen.queryByText(/sync all/i)).toBeNull();
  });

  it("empty and error states", async () => {
    mount(<IntegrationsApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${W}/sources`]: () => jsonResponse(200, []) });
    expect(await screen.findByTestId("sources-empty")).toBeTruthy();
    cleanup();
    mount(<IntegrationsApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${W}/sources`]: () => jsonResponse(503, { detail: "x" }) });
    expect(await screen.findByTestId("sources-error")).toBeTruthy();
  });

  it("a plain member sees a restricted state and no request is made", () => {
    const api = mount(<IntegrationsApp org={orgWith("member")} workspace={WS_VIEWER} />, {});
    expect(screen.getByTestId("integrations-restricted")).toBeTruthy();
    expect(screen.queryByTestId("add-source")).toBeNull();
    expect(api.calls).toHaveLength(0);
  });

  it("sync now posts to the sync route", async () => {
    const sync = vi.fn(() => jsonResponse(202, { sync_id: "y2", state: "queued" }));
    mount(<IntegrationsApp org={orgWith("admin")} workspace={WS_ADMIN} />, { [`GET ${W}/sources`]: () => jsonResponse(200, [src()]), [`POST ${W}/sources/s1/sync`]: sync });
    await userEvent.click(await screen.findByRole("button", { name: /sync acme\/docs now/i }));
    await waitFor(() => expect(sync).toHaveBeenCalled());
  });

  it("revoke asks first, focuses inside the dialog, Escape returns focus, confirm revokes", async () => {
    const revoke = vi.fn(() => jsonResponse(200, src({ state: "revoked", revoked_at: "2026-11-02T00:00:00Z" })));
    mount(<IntegrationsApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${W}/sources`]: () => jsonResponse(200, [src()]), [`POST ${W}/sources/s1/revoke`]: revoke });
    const trigger = await screen.findByRole("button", { name: /revoke acme\/docs/i });
    await userEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: /revoke this source/i });
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(revoke).not.toHaveBeenCalled();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(trigger);
    await userEvent.click(trigger);
    await userEvent.click(screen.getByTestId("confirm-revoke"));
    await waitFor(() => expect(revoke).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
