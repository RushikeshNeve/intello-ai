import { describe, expect, it, vi } from "vitest";
import { ApiError, createBrowserFetcher, loginUrl, type Fetcher } from "../api/http";
import { createOrg, findOrgBySlug, listOrgs, parseOrg } from "../api/orgs";
import { createWorkspace, listWorkspaces } from "@/features/workspaces/api";
import { fakeFetcher, jsonResponse, ORG_A, ORG_B, ws } from "./helpers";

describe("orgs API client (contract §4)", () => {
  it("lists OrgSummary items and treats next_cursor as optional", async () => {
    const { fetcher, calls } = fakeFetcher({ "GET /v1/orgs": () => jsonResponse(200, { items: [ORG_A, ORG_B] }) });
    await expect(listOrgs(fetcher)).resolves.toEqual([ORG_A, ORG_B]);
    expect(calls).toEqual([{ method: "GET", path: "/v1/orgs?limit=200", body: undefined }]);
  });

  it("follows next_cursor across pages", async () => {
    const { fetcher, calls } = fakeFetcher({
      "GET /v1/orgs": (_i, url) =>
        url.searchParams.get("cursor") === "c2"
          ? jsonResponse(200, { items: [ORG_B], next_cursor: null })
          : jsonResponse(200, { items: [ORG_A], next_cursor: "c2" }),
    });
    await expect(listOrgs(fetcher)).resolves.toEqual([ORG_A, ORG_B]);
    expect(calls.map((c) => c.path)).toEqual(["/v1/orgs?limit=200", "/v1/orgs?limit=200&cursor=c2"]);
  });

  it("rejects responses that do not match the contract", async () => {
    const bad = [{ items: [{ ...ORG_A, role: "superuser" }] }, { items: [{ id: 1 }] }, { orgs: [] }, [ORG_A]];
    for (const body of bad) {
      const { fetcher } = fakeFetcher({ "GET /v1/orgs": () => jsonResponse(200, body) });
      await expect(listOrgs(fetcher)).rejects.toMatchObject({ code: "invalid_response" });
    }
  });

  it("creates an org (201 Org) and posts {slug,name} as JSON", async () => {
    const created = { ...ORG_A, created_at: "2026-11-16T00:00:00Z" };
    const { fetcher, calls } = fakeFetcher({ "POST /v1/orgs": () => jsonResponse(201, created) });
    await expect(createOrg(fetcher, { slug: "acme", name: "Acme Research" })).resolves.toEqual(created);
    expect(JSON.parse(calls[0].body!)).toEqual({ slug: "acme", name: "Acme Research" });
    const init = fetcher.mock.calls[0][1]!;
    expect(new Headers(init.headers).get("content-type")).toBe("application/json");
    expect(parseOrg(created)).toEqual(created);
  });

  it("maps {detail} error bodies to ApiError codes", async () => {
    const cases: [number, unknown, string][] = [
      [409, { detail: "slug_taken" }, "slug_taken"],
      [401, { detail: "unauthenticated" }, "unauthenticated"],
      [403, { detail: "csrf_rejected" }, "csrf_rejected"],
      [503, { detail: "identity_unavailable" }, "identity_unavailable"],
      [502, { detail: "upstream_unavailable" }, "upstream_unavailable"],
      [422, { detail: [{ loc: ["body", "slug"], msg: "bad" }] }, "validation_error"],
      [500, "oops", "http_500"],
    ];
    for (const [status, body, code] of cases) {
      const { fetcher } = fakeFetcher({ "POST /v1/orgs": () => jsonResponse(status, body) });
      const err = await createOrg(fetcher, { slug: "x1", name: "X" }).catch((e) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(err).toMatchObject({ status, code });
    }
  });

  it("classifies transport failures", async () => {
    const fetcher: Fetcher = async () => {
      throw new TypeError("fetch failed");
    };
    const err = await listOrgs(fetcher).catch((e) => e);
    expect(err).toMatchObject({ status: 0, code: "network_error", isUnavailable: true });
    expect(new ApiError(403, "forbidden").isNotFoundOrForbidden).toBe(true);
    expect(new ApiError(404, "not_found").isNotFoundOrForbidden).toBe(true);
    expect(new ApiError(401, "unauthenticated").isUnauthenticated).toBe(true);
  });

  it("resolves slugs only within the caller's memberships", () => {
    expect(findOrgBySlug([ORG_A, ORG_B], "globex")).toBe(ORG_B);
    expect(findOrgBySlug([ORG_A], "globex")).toBeUndefined();
  });
});

describe("workspaces API client", () => {
  it("lists an org's workspaces by org id and drops items of other orgs", async () => {
    const mine = ws(ORG_A, "notes", "Notes");
    const foreign = ws(ORG_B, "secret", "Secret");
    const { fetcher, calls } = fakeFetcher({
      [`GET /v1/orgs/${ORG_A.id}/workspaces`]: () => jsonResponse(200, { items: [mine, foreign] }),
    });
    await expect(listWorkspaces(fetcher, ORG_A.id)).resolves.toEqual([mine]);
    expect(calls[0].path).toBe(`/v1/orgs/${ORG_A.id}/workspaces?limit=200`);
  });

  it("creates a workspace; 403 and 409 surface as ApiError", async () => {
    const created = ws(ORG_A, "papers", "Papers");
    const { fetcher } = fakeFetcher({ [`POST /v1/orgs/${ORG_A.id}/workspaces`]: () => jsonResponse(201, created) });
    await expect(createWorkspace(fetcher, ORG_A.id, { slug: "papers", name: "Papers" })).resolves.toEqual(created);
    for (const [status, detail] of [
      [403, "forbidden"],
      [409, "slug_taken"],
    ] as const) {
      const f = fakeFetcher({ [`POST /v1/orgs/${ORG_A.id}/workspaces`]: () => jsonResponse(status, { detail }) });
      await expect(createWorkspace(f.fetcher, ORG_A.id, { slug: "p1", name: "P" })).rejects.toMatchObject({ status, code: detail });
    }
  });
});

describe("browser transport (BFF)", () => {
  it("calls same-origin /api/v1/* with credentials and never adds an Authorization header", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(200, { items: [] }));
    const fetcher = createBrowserFetcher({ fetchImpl, onUnauthenticated: vi.fn(), currentPath: () => "/home" });
    await listOrgs(fetcher);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/orgs?limit=200");
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).has("authorization")).toBe(false);
  });

  it("on 401 sends the user to login with returnTo = the current deep link", async () => {
    const onUnauthenticated = vi.fn();
    const fetcher = createBrowserFetcher({
      fetchImpl: async () => jsonResponse(401, { detail: "unauthenticated" }),
      onUnauthenticated,
      currentPath: () => "/o/acme/w/notes?tab=1",
    });
    await expect(listOrgs(fetcher)).rejects.toMatchObject({ status: 401, code: "unauthenticated" });
    expect(onUnauthenticated).toHaveBeenCalledWith("/api/auth/login?returnTo=%2Fo%2Facme%2Fw%2Fnotes%3Ftab%3D1");
    expect(loginUrl("/home")).toBe("/api/auth/login?returnTo=%2Fhome");
  });
});
