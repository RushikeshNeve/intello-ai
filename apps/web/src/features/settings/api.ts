/** Workspace PATCH/DELETE (contract m01 §4) and the org model policy (m04 §5, ai/policy_router.py). */
import { ApiError, invalid, isRecord, requestJson, type ApiPath, type Fetcher } from "@/features/organizations/api/http";
import { parseWorkspace, type Workspace } from "@/features/workspaces/api";

export const AI_PROVIDERS = ["local", "openai"] as const;
export type AiProvider = (typeof AI_PROVIDERS)[number];
export const PROVIDER_LABEL: Record<AiProvider, string> = { local: "Local model (stays on this server)", openai: "OpenAI (external)" };

export type AiPolicy = {
  allow_external: boolean;
  allowed_providers: AiProvider[];
  is_default: boolean;
  updated_at: string | null;
  server_allows_external: boolean;
};

export function parseAiPolicy(v: unknown): AiPolicy {
  if (!isRecord(v) || typeof v.allow_external !== "boolean" || !Array.isArray(v.allowed_providers)) invalid();
  const providers = v.allowed_providers.filter((p): p is AiProvider => (AI_PROVIDERS as readonly unknown[]).includes(p));
  return {
    allow_external: v.allow_external,
    allowed_providers: providers,
    is_default: v.is_default === true,
    updated_at: typeof v.updated_at === "string" ? v.updated_at : null,
    server_allows_external: v.server_allows_external === true,
  };
}

const policyPath = (orgId: string) => `/v1/orgs/${encodeURIComponent(orgId)}/ai-policy` as ApiPath;
export const policyKey = (orgId: string) => ["org", orgId, "ai-policy"] as const;

export const getAiPolicy = (f: Fetcher, orgId: string): Promise<AiPolicy> => requestJson(f, policyPath(orgId), parseAiPolicy);

/** Providers sent with a policy: external off means local only (the server rejects anything else). */
export function policyBody(allowExternal: boolean, selected: readonly AiProvider[]): { allow_external: boolean; allowed_providers: AiProvider[] } {
  return allowExternal ? { allow_external: true, allowed_providers: [...selected] } : { allow_external: false, allowed_providers: ["local"] };
}

export function putAiPolicy(f: Fetcher, orgId: string, allowExternal: boolean, selected: readonly AiProvider[]): Promise<AiPolicy> {
  return requestJson(f, policyPath(orgId), parseAiPolicy, {
    method: "PUT",
    body: JSON.stringify(policyBody(allowExternal, selected)),
    headers: { "content-type": "application/json" },
  });
}

const wsPath = (orgId: string, workspaceId: string) => `/v1/orgs/${encodeURIComponent(orgId)}/workspaces/${encodeURIComponent(workspaceId)}` as ApiPath;

export function renameWorkspace(f: Fetcher, orgId: string, workspaceId: string, name: string): Promise<Workspace> {
  return requestJson(f, wsPath(orgId, workspaceId), parseWorkspace, {
    method: "PATCH",
    body: JSON.stringify({ name }),
    headers: { "content-type": "application/json" },
  });
}

/** 204 on success (soft delete). */
export async function deleteWorkspace(f: Fetcher, orgId: string, workspaceId: string): Promise<void> {
  let res: Response;
  try {
    res = await f(wsPath(orgId, workspaceId), { method: "DELETE" });
  } catch {
    throw new ApiError(0, "network_error");
  }
  if (!res.ok) throw new ApiError(res.status, `http_${res.status}`);
}


/** Usage summary (M11.01, usage/router.py): aggregates per day / model / kind; no text content. */
export const USAGE_GROUPS = ["kind", "model", "day"] as const;
export type UsageGroup = (typeof USAGE_GROUPS)[number];
export type UsageRow = { key: string; events: number; tokens_in: number; tokens_out: number; tokens_cached: number; est_cost_micros: number };
export type UsageSummary = { group_by: UsageGroup; scope: "org" | "self"; totals: UsageRow; rows: UsageRow[] };

function parseUsageRow(v: unknown): UsageRow {
  if (!isRecord(v) || typeof v.key !== "string") invalid();
  const n = (x: unknown) => (typeof x === "number" && Number.isFinite(x) ? x : invalid());
  return { key: v.key, events: n(v.events), tokens_in: n(v.tokens_in), tokens_out: n(v.tokens_out), tokens_cached: n(v.tokens_cached), est_cost_micros: n(v.est_cost_micros) };
}

export function parseUsageSummary(v: unknown): UsageSummary {
  if (!isRecord(v) || !Array.isArray(v.rows) || !(USAGE_GROUPS as readonly unknown[]).includes(v.group_by)) invalid();
  return { group_by: v.group_by as UsageGroup, scope: v.scope === "self" ? "self" : "org", totals: parseUsageRow(v.totals), rows: v.rows.map(parseUsageRow) };
}

export const usageKey = (orgId: string, group: UsageGroup) => ["org", orgId, "usage", group] as const;
export const getUsage = (f: Fetcher, orgId: string, group: UsageGroup): Promise<UsageSummary> =>
  requestJson(f, `/v1/orgs/${encodeURIComponent(orgId)}/usage?group_by=${group}` as ApiPath, parseUsageSummary);

/** Usage limits (M11.08, usage/limits_router.py): admin-managed budgets with current consumption. */
export const LIMIT_KINDS = ["all", "chat", "embed", "research", "graph"] as const;
export type LimitKind = (typeof LIMIT_KINDS)[number];
export type UsageLimit = {
  id: string;
  scope: "org" | "user";
  kind: LimitKind;
  period: "day" | "month";
  max_tokens: number | null;
  max_requests: number | null;
  action: "warn" | "block";
  consumed_tokens: number;
  consumed_requests: number;
  state: "ok" | "warn" | "blocked";
  resets_at: string | null;
};
export type UsageLimitInput = Pick<UsageLimit, "scope" | "kind" | "period" | "max_tokens" | "max_requests" | "action">;

export function parseUsageLimit(v: unknown): UsageLimit {
  if (!isRecord(v) || typeof v.id !== "string" || !(LIMIT_KINDS as readonly unknown[]).includes(v.kind)) invalid();
  const num = (x: unknown): number | null => (typeof x === "number" ? x : null);
  return {
    id: v.id,
    scope: v.scope === "user" ? "user" : "org",
    kind: v.kind as LimitKind,
    period: v.period === "day" ? "day" : "month",
    max_tokens: num(v.max_tokens),
    max_requests: num(v.max_requests),
    action: v.action === "warn" ? "warn" : "block",
    consumed_tokens: num(v.consumed_tokens) ?? 0,
    consumed_requests: num(v.consumed_requests) ?? 0,
    state: v.state === "blocked" || v.state === "warn" ? v.state : "ok",
    resets_at: typeof v.resets_at === "string" ? v.resets_at : null,
  };
}

const limitsPath = (orgId: string, id?: string) => `/v1/orgs/${encodeURIComponent(orgId)}/usage/limits${id ? `/${encodeURIComponent(id)}` : ""}` as ApiPath;
export const limitsKey = (orgId: string) => ["org", orgId, "usage-limits"] as const;
export const listLimits = (f: Fetcher, orgId: string): Promise<UsageLimit[]> =>
  requestJson(f, limitsPath(orgId), (v) => {
    if (!isRecord(v) || !Array.isArray(v.limits)) invalid();
    return v.limits.map(parseUsageLimit);
  });
export const createLimit = (f: Fetcher, orgId: string, body: UsageLimitInput): Promise<UsageLimit> =>
  requestJson(f, limitsPath(orgId), parseUsageLimit, { method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" } });
export async function deleteLimit(f: Fetcher, orgId: string, id: string): Promise<void> {
  let res: Response;
  try {
    res = await f(limitsPath(orgId, id), { method: "DELETE" });
  } catch {
    throw new ApiError(0, "network_error");
  }
  if (!res.ok) throw new ApiError(res.status, `http_${res.status}`);
}
