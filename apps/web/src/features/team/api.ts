/**
 * Members API client (contract m01 §4). There is NO workspace member list and no email is sent:
 * workspace access is set per user id (PUT/DELETE), members come from the org member list, and a person who has
 * never signed in is added by pre-approving their email.
 */
import { ApiError, invalid, isRecord, listAll, oneOf, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";
import { ORG_ROLES, type OrgRole } from "@/features/organizations/api/orgs";
import { WORKSPACE_ROLES, type WorkspaceRole } from "@/features/workspaces/api";

export type OrgMember = { user_id: string; email: string; display_name: string; role: OrgRole };

export function parseMember(v: unknown): OrgMember {
  if (!isRecord(v)) invalid();
  return {
    user_id: str(v, "user_id"),
    email: typeof v.email === "string" ? v.email : "",
    display_name: typeof v.display_name === "string" ? v.display_name : "",
    role: oneOf(v, "role", ORG_ROLES),
  };
}

const org = (orgId: string) => `/v1/orgs/${encodeURIComponent(orgId)}`;
const ws = (orgId: string, workspaceId: string) => `${org(orgId)}/workspaces/${encodeURIComponent(workspaceId)}`;

export const teamKeys = {
  members: (orgId: string) => ["org", orgId, "members"] as const,
  preapprovals: (orgId: string) => ["org", orgId, "preapprovals"] as const,
  me: () => ["me"] as const,
};

export function listMembers(fetcher: Fetcher, orgId: string): Promise<OrgMember[]> {
  return listAll(fetcher, `${org(orgId)}/members` as ApiPath, parseMember);
}

/** Someone pre-approved by email: they get access the first time they sign in with it. */
export type Preapproval = { id: string; email: string; role: OrgRole; created_at: string };
export type PreapprovalResult = { status: "member_added" | "pending" };

export function parsePreapproval(v: unknown): Preapproval {
  if (!isRecord(v)) invalid();
  return { id: str(v, "id"), email: str(v, "email"), role: oneOf(v, "role", ORG_ROLES), created_at: str(v, "created_at") };
}

const preapprovalsPath = (orgId: string) => `${org(orgId)}/members/preapprovals` as ApiPath;

export function listPreapprovals(fetcher: Fetcher, orgId: string): Promise<Preapproval[]> {
  return requestJson(fetcher, preapprovalsPath(orgId), (b) => {
    if (!isRecord(b) || !Array.isArray(b.items)) invalid();
    return b.items.map(parsePreapproval);
  });
}

export function addPreapproval(fetcher: Fetcher, orgId: string, email: string, role: OrgRole): Promise<PreapprovalResult> {
  return requestJson(
    fetcher,
    preapprovalsPath(orgId),
    (b) => {
      if (!isRecord(b)) invalid();
      return { status: oneOf(b, "status", ["member_added", "pending"] as const) };
    },
    { method: "POST", body: JSON.stringify({ email, role }), headers: { "content-type": "application/json" } },
  );
}

export function removePreapproval(fetcher: Fetcher, orgId: string, id: string): Promise<void> {
  return del(fetcher, `${preapprovalsPath(orgId)}/${encodeURIComponent(id)}` as ApiPath);
}

/** `GET /v1/me` → id only (used to mark the caller's own row). */
export function getMyId(fetcher: Fetcher): Promise<string> {
  return requestJson(fetcher, "/v1/me", (b) => {
    if (!isRecord(b)) invalid();
    return str(b, "id");
  });
}

const putJson = (role: string): RequestInit => ({ method: "PUT", body: JSON.stringify({ role }), headers: { "content-type": "application/json" } });

async function del(fetcher: Fetcher, path: ApiPath): Promise<void> {
  let res: Response;
  try {
    res = await fetcher(path, { method: "DELETE" });
  } catch {
    throw new ApiError(0, "network_error");
  }
  if (res.ok) return;
  let code = `http_${res.status}`;
  try {
    const b: unknown = await res.json();
    if (isRecord(b) && typeof b.detail === "string") code = b.detail;
  } catch {
    // keep synthetic code
  }
  throw new ApiError(res.status, code);
}

export function setOrgRole(fetcher: Fetcher, orgId: string, userId: string, role: OrgRole): Promise<unknown> {
  return requestJson(fetcher, `${org(orgId)}/members/${encodeURIComponent(userId)}` as ApiPath, (b) => b, putJson(role));
}
export function removeOrgMember(fetcher: Fetcher, orgId: string, userId: string): Promise<void> {
  return del(fetcher, `${org(orgId)}/members/${encodeURIComponent(userId)}` as ApiPath);
}
export function setWorkspaceRole(fetcher: Fetcher, orgId: string, workspaceId: string, userId: string, role: WorkspaceRole): Promise<unknown> {
  return requestJson(fetcher, `${ws(orgId, workspaceId)}/members/${encodeURIComponent(userId)}` as ApiPath, (b) => b, putJson(role));
}
export function removeWorkspaceMember(fetcher: Fetcher, orgId: string, workspaceId: string, userId: string): Promise<void> {
  return del(fetcher, `${ws(orgId, workspaceId)}/members/${encodeURIComponent(userId)}` as ApiPath);
}

export { WORKSPACE_ROLES };

/**
 * Which org roles the caller may assign to a member, and whether the row is editable at all.
 * Only owners may grant or revoke `owner` (contract m01 §3); the server enforces it again.
 */
export function assignableOrgRoles(actor: OrgRole, target: OrgRole): OrgRole[] {
  if (actor === "member") return [];
  if (actor === "admin") return target === "owner" ? [] : ["admin", "member"];
  return ["owner", "admin", "member"];
}

export const WORKSPACE_ROLE_LABEL: Record<WorkspaceRole, string> = { admin: "Admin", editor: "Editor", viewer: "Viewer" };
