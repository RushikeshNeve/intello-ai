import {
  isRecord,
  invalid,
  jsonBody,
  listAll,
  oneOf,
  requestJson,
  str,
  type ApiPath,
  type Fetcher,
} from "@/features/organizations/api/http";

export const WORKSPACE_ROLES = ["admin", "editor", "viewer"] as const;
export type WorkspaceRole = (typeof WORKSPACE_ROLES)[number];

/**
 * `Workspace = {id, org_id, slug, name, created_at, role}` (contract §4). For an org owner/admin without a
 * workspace membership the effective role is the implicit workspace admin (contract §3); a null role is
 * tolerated and shown as no role.
 */
export type Workspace = {
  id: string;
  org_id: string;
  slug: string;
  name: string;
  created_at: string;
  role: WorkspaceRole | null;
};

export function parseWorkspace(v: unknown): Workspace {
  if (!isRecord(v)) invalid();
  return {
    id: str(v, "id"),
    org_id: str(v, "org_id"),
    slug: str(v, "slug"),
    name: str(v, "name"),
    created_at: str(v, "created_at"),
    role: v.role === null || v.role === undefined ? null : oneOf(v, "role", WORKSPACE_ROLES),
  };
}

function orgPath(orgId: string): ApiPath {
  return `/v1/orgs/${encodeURIComponent(orgId)}/workspaces`;
}

export async function listWorkspaces(fetcher: Fetcher, orgId: string): Promise<Workspace[]> {
  const items = await listAll(fetcher, orgPath(orgId), parseWorkspace);
  // Defence in depth: never show a workspace that claims to belong to another org.
  return items.filter((w) => w.org_id === orgId);
}

export type CreateWorkspaceInput = { slug: string; name: string };

/** 201 → `Workspace`; 403 for members; 409 `slug_taken`. */
export function createWorkspace(fetcher: Fetcher, orgId: string, input: CreateWorkspaceInput): Promise<Workspace> {
  return requestJson(fetcher, orgPath(orgId), parseWorkspace, jsonBody(input));
}

export function findWorkspaceBySlug(list: readonly Workspace[], slug: string): Workspace | undefined {
  return list.find((w) => w.slug === slug);
}
