/**
 * Query keys. Everything that belongs to an organization lives under `["org", orgId, ...]`, so the scope
 * guard can drop every other org's data with one predicate (ADR-044/047).
 */
export const queryKeys = {
  /** The caller's memberships (not org-scoped). */
  orgs: () => ["orgs"] as const,
  org: (orgId: string) => ["org", orgId] as const,
  workspaces: (orgId: string) => ["org", orgId, "workspaces"] as const,
};

export function orgIdOfKey(key: readonly unknown[]): string | null {
  return key[0] === "org" && typeof key[1] === "string" ? key[1] : null;
}
