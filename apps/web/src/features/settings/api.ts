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

