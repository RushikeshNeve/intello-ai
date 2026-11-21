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
