import type { OrgSummary } from "@/features/organizations/api/orgs";
import type { Workspace } from "@/features/workspaces/api";

/** Org owner/admin. UI affordances only: the server enforces every one of these again. */
export const isOrgAdmin = (org: Pick<OrgSummary, "role">): boolean => org.role === "owner" || org.role === "admin";

/** Org owner/admin, or an explicit workspace admin (contract m01 §3). */
export const canAdminWorkspace = (org: Pick<OrgSummary, "role">, workspace: Pick<Workspace, "role">): boolean =>
  isOrgAdmin(org) || workspace.role === "admin";

export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 16)} UTC`;
}

/** Calm, code-based message for a failed management action; never echoes server text. */
export function actionError(err: unknown, fallback = "Something went wrong. Try again."): string {
  const e = err as { status?: number; code?: string } | null;
  if (e && typeof e.status === "number") {
    if (e.code === "last_owner") return "An organization must keep at least one owner.";
    if (e.code === "not_org_member") return "That person is not a member of this organization.";
    if (e.status === 403) return "Your role does not allow this.";
    if (e.status === 404) return "This no longer exists or you no longer have access.";
    if (e.status === 422) return "The server rejected this value. Check it and try again.";
    if (e.status === 0 || e.status === 502 || e.status === 503 || e.status === 504) return "The service is temporarily unavailable. Try again.";
  }
  return fallback;
}
