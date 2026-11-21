// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsApp } from "../settings-app";
import { parseAiPolicy, policyBody } from "../api";
import { jsonResponse, mount, O, orgWith, W, WS_ADMIN, WS_VIEWER } from "./helpers";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, refresh: vi.fn() }) }));
afterEach(cleanup);

const policy = (extra = {}) => ({ allow_external: false, allowed_providers: ["local"], is_default: true, updated_at: null, updated_by: null, server_allows_external: false, ...extra });

describe("policy api", () => {
  it("parses, rejects junk, and builds a valid body", () => {
    expect(parseAiPolicy(policy()).server_allows_external).toBe(false);
    expect(() => parseAiPolicy({ allow_external: "yes" })).toThrow();
    expect(policyBody(false, ["openai"])).toEqual({ allow_external: false, allowed_providers: ["local"] });
    expect(policyBody(true, ["local", "openai"]).allowed_providers).toEqual(["local", "openai"]);
  });
});

describe("SettingsApp", () => {
  it("workspace tab: admin renames, slug is read-only, delete needs the slug typed", async () => {
    const patch = vi.fn(() => jsonResponse(200, { ...WS_ADMIN, name: "Renamed" }));
    const del = vi.fn(() => jsonResponse(204, null));
    mount(<SettingsApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`PATCH ${W}`]: patch, [`DELETE ${W}`]: del });
    expect((screen.getByLabelText(/address/i) as HTMLInputElement).readOnly).toBe(true);
    const name = screen.getByLabelText("Workspace name");
    await userEvent.clear(name);
    await userEvent.type(name, "Renamed");
    await userEvent.click(screen.getByTestId("rename-save"));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    await userEvent.click(screen.getByTestId("delete-open"));
    const confirm = screen.getByTestId("delete-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await userEvent.type(screen.getByLabelText("Workspace address"), WS_ADMIN.slug);
    await userEvent.click(confirm);
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/o/acme"));
  });

  it("workspace tab is read-only for a viewer", () => {
    mount(<SettingsApp org={orgWith("member")} workspace={WS_VIEWER} />, {});
    expect(screen.getByTestId("workspace-readonly")).toBeTruthy();
    expect(screen.queryByTestId("delete-open")).toBeNull();
    expect((screen.getByLabelText("Workspace name") as HTMLInputElement).disabled).toBe(true);
  });

  it("model policy: shows server flag, admin saves, member is read-only, error state", async () => {
    const put = vi.fn(() => jsonResponse(200, policy({ allow_external: true, allowed_providers: ["local", "openai"], is_default: false })));
    mount(<SettingsApp org={orgWith("admin")} workspace={WS_ADMIN} />, { [`GET ${O}/ai-policy`]: () => jsonResponse(200, policy()), [`PUT ${O}/ai-policy`]: put });
    await userEvent.click(screen.getByRole("tab", { name: "Model policy" }));
    expect(await screen.findByTestId("server-allows")).toBeTruthy();
    expect(screen.getByTestId("server-allows").textContent).toMatch(/switched off/);
    await userEvent.click(screen.getByRole("switch"));
    await userEvent.click(screen.getByLabelText(/OpenAI/));
    await userEvent.click(screen.getByTestId("policy-save"));
    await waitFor(() => expect(put).toHaveBeenCalled());
    cleanup();
    mount(<SettingsApp org={orgWith("member")} workspace={WS_VIEWER} />, { [`GET ${O}/ai-policy`]: () => jsonResponse(200, policy()) });
    await userEvent.click(screen.getByRole("tab", { name: "Model policy" }));
    expect(await screen.findByTestId("policy-readonly")).toBeTruthy();
    expect(screen.queryByTestId("policy-save")).toBeNull();
    cleanup();
    mount(<SettingsApp org={orgWith("admin")} workspace={WS_ADMIN} />, { [`GET ${O}/ai-policy`]: () => jsonResponse(503, { detail: "x" }) });
    await userEvent.click(screen.getByRole("tab", { name: "Model policy" }));
    expect(await screen.findByTestId("policy-error")).toBeTruthy();
  });

  it("privacy is static, usage shows real aggregates or an honest empty state", async () => {
    const row = { key: "chat", events: 2, tokens_in: 1200, tokens_out: 300, tokens_cached: 0, est_cost_micros: 0 };
    const usage = (rows: unknown[], totals: unknown) => () => jsonResponse(200, { group_by: "kind", scope: "org", totals, rows });
    mount(<SettingsApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${O}/usage`]: usage([row], { ...row, key: "total" }) });
    expect(screen.queryByRole("tab", { name: /notifications/i })).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: "Privacy" }));
    expect(screen.getByTestId("privacy-tab")).toBeTruthy();
    await userEvent.click(screen.getByRole("tab", { name: "Usage" }));
    expect((await screen.findByTestId("usage-table")).textContent).toMatch(/1,200/);
    cleanup();
    mount(<SettingsApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${O}/usage`]: usage([], { ...row, key: "total", events: 0 }) });
    await userEvent.click(screen.getByRole("tab", { name: "Usage" }));
    expect(await screen.findByText(/no model usage yet/i)).toBeTruthy();
  });

  it("usage limits: admins see consumption bars, members do not see the editor", async () => {
    const empty = () => jsonResponse(200, { group_by: "kind", scope: "org", totals: { key: "total", events: 0, tokens_in: 0, tokens_out: 0, tokens_cached: 0, est_cost_micros: 0 }, rows: [] });
    const limit = { id: "l1", scope: "org", kind: "chat", period: "day", max_tokens: 100, max_requests: null, action: "block", consumed_tokens: 100, consumed_requests: 3, state: "blocked", resets_at: "2030-01-01T00:00:00Z", created_at: "2030-01-01T00:00:00Z" };
    mount(<SettingsApp org={orgWith("admin")} workspace={WS_ADMIN} />, { [`GET ${O}/usage`]: empty, [`GET ${O}/usage/limits`]: () => jsonResponse(200, { limits: [limit] }) });
    await userEvent.click(screen.getByRole("tab", { name: "Usage" }));
    expect((await screen.findByTestId("limit-row")).textContent).toMatch(/limit reached/);
    expect(screen.getByRole("progressbar", { name: "Tokens" }).getAttribute("aria-valuenow")).toBe("100");
    cleanup();
    mount(<SettingsApp org={orgWith("member")} workspace={WS_VIEWER} />, { [`GET ${O}/usage`]: empty });
    await userEvent.click(screen.getByRole("tab", { name: "Usage" }));
    await screen.findByText(/no model usage yet/i);
    expect(screen.queryByTestId("limits-panel")).toBeNull();
  });
});
