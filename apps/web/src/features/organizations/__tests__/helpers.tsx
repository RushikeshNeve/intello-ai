import type { ReactNode } from "react";
import { vi } from "vitest";
import { render } from "@testing-library/react";
import type { QueryClient } from "@tanstack/react-query";
import { makeQueryClient, QueryProvider } from "@/lib/query/provider";
import type { Fetcher } from "../api/http";
import type { OrgSummary } from "../api/orgs";
import type { Workspace } from "@/features/workspaces/api";

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type Handler = (init: RequestInit | undefined, url: URL) => Response | Promise<Response>;

/** A fake API transport: `"GET /v1/orgs"` → handler. Unknown routes are 404 `not_found`. */
export function fakeFetcher(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; body?: string }[] = [];
  const fetcher = vi.fn<Fetcher>(async (path, init) => {
    const url = new URL(path, "http://api.invalid");
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ method, path: url.pathname + url.search, body: typeof init?.body === "string" ? init.body : undefined });
    const handler = routes[`${method} ${url.pathname}`];
    return handler ? handler(init, url) : jsonResponse(404, { detail: "not_found" });
  });
  return { fetcher, calls };
}

export const ORG_A: OrgSummary = { id: "11111111-1111-4111-8111-111111111111", slug: "acme", name: "Acme Research", role: "owner" };
export const ORG_B: OrgSummary = { id: "22222222-2222-4222-8222-222222222222", slug: "globex", name: "Globex Lab", role: "member" };

export function ws(org: OrgSummary, slug: string, name: string, role: Workspace["role"] = "admin"): Workspace {
  return { id: `${org.id.slice(0, 8)}-${slug}`, org_id: org.id, slug, name, created_at: "2026-11-16T00:00:00Z", role };
}

export function renderWithQuery(ui: ReactNode, opts: { fetcher: Fetcher; client?: QueryClient }) {
  const client = opts.client ?? makeQueryClient();
  const wrap = (node: ReactNode) => (
    <QueryProvider client={client} fetcher={opts.fetcher}>
      {node}
    </QueryProvider>
  );
  const utils = render(wrap(ui));
  return { ...utils, client, rerender: (node: ReactNode) => utils.rerender(wrap(node)) };
}
