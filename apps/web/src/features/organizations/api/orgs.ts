import { isRecord, invalid, jsonBody, listAll, oneOf, requestJson, str, type Fetcher } from "./http";

export const ORG_ROLES = ["owner", "admin", "member"] as const;
export type OrgRole = (typeof ORG_ROLES)[number];

/** `GET /v1/orgs` item (contract §4). */
export type OrgSummary = { id: string; slug: string; name: string; role: OrgRole };
/** `POST /v1/orgs` / `GET /v1/orgs/{id}` (contract §4). */
export type Org = OrgSummary & { created_at: string };

export function parseOrgSummary(v: unknown): OrgSummary {
  if (!isRecord(v)) invalid();
  return { id: str(v, "id"), slug: str(v, "slug"), name: str(v, "name"), role: oneOf(v, "role", ORG_ROLES) };
}

export function parseOrg(v: unknown): Org {
  const summary = parseOrgSummary(v);
  return { ...summary, created_at: str(v as Record<string, unknown>, "created_at") };
}

export function listOrgs(fetcher: Fetcher): Promise<OrgSummary[]> {
  return listAll(fetcher, "/v1/orgs", parseOrgSummary);
}

export type CreateOrgInput = { slug: string; name: string };

/** 201 → `Org`; 409 `slug_taken`. */
export function createOrg(fetcher: Fetcher, input: CreateOrgInput): Promise<Org> {
  return requestJson(fetcher, "/v1/orgs", parseOrg, jsonBody(input));
}

/** The API is id-based; URLs carry slugs. Resolution only ever looks at the caller's own memberships. */
export function findOrgBySlug(orgs: readonly OrgSummary[], slug: string): OrgSummary | undefined {
  return orgs.find((o) => o.slug === slug);
}

export function canManageWorkspaces(role: OrgRole): boolean {
  return role === "owner" || role === "admin";
}

export const ROLE_LABEL: Record<OrgRole, string> = { owner: "Owner", admin: "Admin", member: "Member" };
