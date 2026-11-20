/**
 * Sources API client (contract m06 §2; source of truth apps/api/app/modules/connectors/schemas.py).
 * Routes are workspace-scoped and admin-only. A response never carries a secret except `webhook_secret`,
 * shown once at creation.
 */
import { ApiError, invalid, isRecord, jsonBody, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";

export type SyncSummary = {
  id: string;
  state: string;
  trigger: string;
  error_code: string | null;
  stats: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type Source = {
  id: string;
  workspace_id: string;
  provider: string;
  name: string;
  repo: string | null;
  ref: string | null;
  include: string[];
  state: string;
  has_token: boolean;
  sync_interval_seconds: number;
  next_sync_at: string | null;
  created_at: string;
  revoked_at: string | null;
  last_sync: SyncSummary | null;
  items: { active: number; deleted: number } | null;
};

export type SourceCreated = Source & { webhook_secret: string | null; webhook_path: string };

const optStr = (v: unknown): string | null => (typeof v === "string" ? v : null);

function parseSync(v: unknown): SyncSummary | null {
  if (!isRecord(v)) return null;
  return {
    id: str(v, "id"),
    state: str(v, "state"),
    trigger: optStr(v.trigger) ?? "",
    error_code: optStr(v.error_code),
    stats: isRecord(v.stats) ? v.stats : {},
    created_at: optStr(v.created_at) ?? "",
    started_at: optStr(v.started_at),
    finished_at: optStr(v.finished_at),
  };
}

export function parseSource(v: unknown): Source {
  if (!isRecord(v)) invalid();
  const config = isRecord(v.config) ? v.config : {};
  const items = isRecord(v.items) && typeof v.items.active === "number" && typeof v.items.deleted === "number" ? { active: v.items.active, deleted: v.items.deleted } : null;
  return {
    id: str(v, "id"),
    workspace_id: str(v, "workspace_id"),
    provider: str(v, "provider"),
    name: str(v, "name"),
    repo: optStr(config.repo),
    ref: optStr(config.ref),
    include: Array.isArray(config.include) ? config.include.filter((g): g is string => typeof g === "string") : [],
    state: str(v, "state"),
    has_token: v.has_token === true,
    sync_interval_seconds: typeof v.sync_interval_seconds === "number" ? v.sync_interval_seconds : 0,
    next_sync_at: optStr(v.next_sync_at),
    created_at: optStr(v.created_at) ?? "",
    revoked_at: optStr(v.revoked_at),
    last_sync: v.last_sync ? parseSync(v.last_sync) : null,
    items,
  };
}

export function parseSourceCreated(v: unknown): SourceCreated {
  const s = parseSource(v);
  const r = v as Record<string, unknown>;
  return { ...s, webhook_secret: optStr(r.webhook_secret), webhook_path: optStr(r.webhook_path) ?? "" };
}

/** The list route returns a bare array (`list[SourceOut]`); an `{items}` envelope is tolerated. */
export function parseSourceList(body: unknown): Source[] {
  const arr = Array.isArray(body) ? body : isRecord(body) && Array.isArray(body.items) ? body.items : invalid();
  return arr.map(parseSource);
}

const base = (orgId: string, workspaceId: string) =>
  `/v1/orgs/${encodeURIComponent(orgId)}/workspaces/${encodeURIComponent(workspaceId)}/sources`;
const one = (o: string, w: string, id: string) => `${base(o, w)}/${encodeURIComponent(id)}` as ApiPath;

export const sourceKeys = {
  list: (orgId: string, workspaceId: string) => ["org", orgId, "workspace", workspaceId, "sources"] as const,
};

export function listSources(fetcher: Fetcher, orgId: string, workspaceId: string, signal?: AbortSignal): Promise<Source[]> {
  return requestJson(fetcher, base(orgId, workspaceId) as ApiPath, parseSourceList, { signal });
}

export type CreateSourceInput = { repo: string; ref?: string; include: string[]; token?: string };

export function createSource(fetcher: Fetcher, orgId: string, workspaceId: string, input: CreateSourceInput): Promise<SourceCreated> {
  const body: Record<string, unknown> = { provider: "github", repo: input.repo, include: input.include };
  if (input.ref) body.ref = input.ref;
  if (input.token) body.token = input.token;
  return requestJson(fetcher, base(orgId, workspaceId) as ApiPath, parseSourceCreated, jsonBody(body));
}

/** 202 `{sync_id, state: queued|already_queued}`. */
export function syncSource(fetcher: Fetcher, orgId: string, workspaceId: string, id: string): Promise<{ state: "queued" | "already_queued" }> {
  return requestJson(
    fetcher,
    `${one(orgId, workspaceId, id)}/sync` as ApiPath,
    (b) => {
      if (!isRecord(b)) invalid();
      return { state: b.state === "already_queued" ? "already_queued" : "queued" };
    },
    { method: "POST" },
  );
}

export function revokeSource(fetcher: Fetcher, orgId: string, workspaceId: string, id: string): Promise<Source> {
  return requestJson(fetcher, `${one(orgId, workspaceId, id)}/revoke` as ApiPath, parseSource, { method: "POST" });
}

/** Splits a comma/newline separated glob list into trimmed, non-empty patterns. */
export function parseGlobs(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((g) => g.trim())
    .filter(Boolean);
}

/** `owner/name` with the characters GitHub allows. */
export function isValidRepo(repo: string): boolean {
  return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo.trim());
}

export function syncStateLabel(state: string | undefined): { text: string; tone: "neutral" | "info" | "success" | "warning" | "danger" } {
  switch (state) {
    case "queued":
      return { text: "Queued", tone: "info" };
    case "running":
      return { text: "Syncing", tone: "info" };
    case "succeeded":
      return { text: "Last sync succeeded", tone: "success" };
    case "failed":
      return { text: "Last sync failed", tone: "danger" };
    default:
      return { text: "Not synced yet", tone: "neutral" };
  }
}

export function sourceErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return "Only workspace admins can manage integrations.";
    if (err.status === 404) return "This source no longer exists or you no longer have access.";
    if (err.status === 422) return "The server rejected these values. Check the repository, branch and patterns.";
    if (err.status === 409) return "This conflicts with an existing source.";
    if (err.isUnavailable) return "The service is temporarily unavailable. Try again.";
  }
  return "Something went wrong. Try again.";
}
