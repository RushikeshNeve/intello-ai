import type { ReactNode } from "react";
import { makeQueryClient } from "@/lib/query/provider";
import { ToastProvider } from "@/components/ui/toast";
import { fakeFetcher, renderWithQuery, ORG_A, ws } from "@/features/organizations/__tests__/helpers";
import type { OrgSummary } from "@/features/organizations/api/orgs";

export { jsonResponse, ORG_A } from "@/features/organizations/__tests__/helpers";

export const WS_ADMIN = ws(ORG_A, "notes", "Notes", "admin");
export const WS_VIEWER = ws(ORG_A, "notes", "Notes", "viewer");
export const orgWith = (role: OrgSummary["role"]): OrgSummary => ({ ...ORG_A, role });
export const O = `/v1/orgs/${ORG_A.id}`;
export const W = `${O}/workspaces/${WS_ADMIN.id}`;

export function mount(ui: ReactNode, routes: Parameters<typeof fakeFetcher>[0]) {
  const api = fakeFetcher(routes);
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  renderWithQuery(<ToastProvider>{ui}</ToastProvider>, { fetcher: api.fetcher, client });
  return api;
}
