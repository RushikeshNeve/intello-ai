import "server-only";
import { cache } from "react";
import { redirect } from "next/navigation";
import { apiFetch } from "@/features/auth/server";
import { listWorkspaces, type Workspace } from "@/features/workspaces/api";
import { ApiError, loginUrl, type Fetcher } from "./api/http";
import { findOrgBySlug, listOrgs, type OrgSummary } from "./api/orgs";

/** Server transport: the session's bearer token is attached by the auth DAL; the browser never sees it. */
export const serverFetcher: Fetcher = (path, init) => apiFetch(path, init);

export type LoadFailure = "unauthenticated" | "not_found" | "unavailable";
export type Loaded<T> = { ok: true; data: T } | { ok: false; reason: LoadFailure };

export function classify(err: unknown): LoadFailure {
  if (err instanceof ApiError) {
    if (err.status === 401) return "unauthenticated";
    // 403 and 404 are the same answer to the user: "not found or no access".
    if (err.status === 403 || err.status === 404) return "not_found";
  }
  return "unavailable";
}

async function load<T>(fn: () => Promise<T>): Promise<Loaded<T>> {
  try {
    return { ok: true, data: await fn() };
  } catch (err) {
    return { ok: false, reason: classify(err) };
  }
}

/** The caller's memberships, once per request (React `cache`). */
export const getMyOrgs = cache(() => load(() => listOrgs(serverFetcher)));

export const getOrgWorkspaces = cache((orgId: string) => load(() => listWorkspaces(serverFetcher, orgId)));

export type OrgScopeData = { org: OrgSummary; orgs: OrgSummary[] };

/**
 * Slug → org, resolved only against the caller's own memberships (the API is id-based). An unknown slug
 * and a slug of an org the caller is not in are indistinguishable.
 */
export async function resolveOrgScope(orgSlug: string): Promise<Loaded<OrgScopeData>> {
  const orgs = await getMyOrgs();
  if (!orgs.ok) return orgs;
  const org = findOrgBySlug(orgs.data, orgSlug);
  return org ? { ok: true, data: { org, orgs: orgs.data } } : { ok: false, reason: "not_found" };
}

export type WorkspaceScopeData = OrgScopeData & { workspace: Workspace; workspaces: Workspace[] };

export async function resolveWorkspaceScope(orgSlug: string, workspaceSlug: string): Promise<Loaded<WorkspaceScopeData>> {
  const scope = await resolveOrgScope(orgSlug);
  if (!scope.ok) return scope;
  const workspaces = await getOrgWorkspaces(scope.data.org.id);
  if (!workspaces.ok) return workspaces;
  const workspace = workspaces.data.find((w) => w.slug === workspaceSlug);
  return workspace
    ? { ok: true, data: { ...scope.data, workspace, workspaces: workspaces.data } }
    : { ok: false, reason: "not_found" };
}

/** Session gone (or the API rejected the token): sign in again and come back to `path`. */
export function redirectToLogin(path: string): never {
  redirect(loginUrl(path));
}
