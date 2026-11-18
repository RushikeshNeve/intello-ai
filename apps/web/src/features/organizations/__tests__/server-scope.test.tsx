// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { jsonResponse, ORG_A, ORG_B, ws } from "./helpers";

vi.mock("server-only", () => ({}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const apiFetch = vi.fn<(path: string, init?: RequestInit) => Promise<Response>>();
vi.mock("@/features/auth/server", () => ({ apiFetch: (p: string, i?: RequestInit) => apiFetch(p, i) }));

const { resolveOrgScope, resolveWorkspaceScope, classify } = await import("../server");
const { ScopeNotFound } = await import("@/features/shell/states");
const { ApiError } = await import("../api/http");

function route(map: Record<string, () => Response>) {
  apiFetch.mockImplementation(async (path) => {
    const p = new URL(path, "http://x").pathname;
    return map[p]?.() ?? jsonResponse(404, { detail: "not_found" });
  });
}

const A_WS = [ws(ORG_A, "notes", "Acme Notes"), ws(ORG_A, "papers", "Acme Papers")];

beforeEach(() => apiFetch.mockReset());
afterEach(cleanup);

describe("slug → scope resolution (server)", () => {
  it("resolves a slug from the caller's org list", async () => {
    route({ "/v1/orgs": () => jsonResponse(200, { items: [ORG_A, ORG_B] }) });
    await expect(resolveOrgScope("globex")).resolves.toEqual({ ok: true, data: { org: ORG_B, orgs: [ORG_A, ORG_B] } });
  });

  it("an unknown slug and another org's slug are both not_found", async () => {
    route({ "/v1/orgs": () => jsonResponse(200, { items: [ORG_A] }) });
    await expect(resolveOrgScope("globex")).resolves.toEqual({ ok: false, reason: "not_found" });
    await expect(resolveOrgScope("does-not-exist")).resolves.toEqual({ ok: false, reason: "not_found" });
  });

  it("maps 401 → unauthenticated, 5xx/BFF errors → unavailable", async () => {
    route({ "/v1/orgs": () => jsonResponse(401, { detail: "unauthenticated" }) });
    await expect(resolveOrgScope("acme")).resolves.toEqual({ ok: false, reason: "unauthenticated" });
    route({ "/v1/orgs": () => jsonResponse(503, { detail: "identity_unavailable" }) });
    await expect(resolveOrgScope("acme")).resolves.toEqual({ ok: false, reason: "unavailable" });
    // Transport failures (apiFetch throwing: API down, base URL missing) are "unavailable".
    expect(classify(new TypeError("fetch failed"))).toBe("unavailable");
    expect(classify(new ApiError(0, "network_error"))).toBe("unavailable");
    expect(classify(new ApiError(403, "forbidden"))).toBe("not_found");
  });

  it("resolves a workspace deep link; unknown or unreadable workspace → not_found", async () => {
    route({
      "/v1/orgs": () => jsonResponse(200, { items: [ORG_A] }),
      [`/v1/orgs/${ORG_A.id}/workspaces`]: () => jsonResponse(200, { items: A_WS }),
    });
    const ok = await resolveWorkspaceScope("acme", "papers");
    expect(ok).toMatchObject({ ok: true, data: { org: ORG_A, workspace: A_WS[1] } });
    await expect(resolveWorkspaceScope("acme", "secret")).resolves.toEqual({ ok: false, reason: "not_found" });
    await expect(resolveWorkspaceScope("globex", "notes")).resolves.toEqual({ ok: false, reason: "not_found" });

    route({
      "/v1/orgs": () => jsonResponse(200, { items: [ORG_A] }),
      [`/v1/orgs/${ORG_A.id}/workspaces`]: () => jsonResponse(403, { detail: "forbidden" }),
    });
    await expect(resolveWorkspaceScope("acme", "notes")).resolves.toEqual({ ok: false, reason: "not_found" });
  });

  it("the not-found page is calm, generic and leaks nothing", () => {
    render(<ScopeNotFound />);
    expect(screen.getByRole("heading", { level: 1, name: "Not found or no access" })).toBeTruthy();
    expect(screen.getByRole("main")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Go to your organizations" }).getAttribute("href")).toBe("/home");
    const text = document.body.textContent ?? "";
    for (const leak of [ORG_A.name, ORG_B.name, "Acme Notes", "403", "404", "forbidden"]) expect(text).not.toContain(leak);
  });
});
