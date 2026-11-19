/** Query keys live under `["org", orgId, ...]` so the org-scope guard drops them on an org switch. */
export const chatKeys = {
  threads: (orgId: string) => ["org", orgId, "chat", "threads"] as const,
  thread: (orgId: string, threadId: string) => ["org", orgId, "chat", "thread", threadId] as const,
};

export const newIdempotencyKey = (): string =>
  typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
