/** Query keys live under `["org", orgId, ...]` so the org-scope guard drops them on an org switch. */
export const documentKeys = {
  all: (orgId: string, workspaceId: string) => ["org", orgId, "documents", workspaceId] as const,
  list: (orgId: string, workspaceId: string, paramsKey: string) =>
    ["org", orgId, "documents", workspaceId, "list", paramsKey] as const,
};
