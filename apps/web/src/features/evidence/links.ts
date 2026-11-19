/**
 * Deep links to the evidence viewer. The URL carries everything needed to reopen the exact passage
 * (`/o/{org}/w/{workspace}/documents/{doc}/evidence?v={version}&chunk={chunk}&page={n}`), so links from
 * search results and chat citations are stable and shareable. Pure.
 */
import type { Workspace } from "@/features/workspaces/api";

export type EvidenceTarget = {
  orgSlug: string;
  /** Workspace slug when known, otherwise its id (the evidence page accepts both). */
  workspaceRef: string;
  documentId: string;
  versionId: string;
  chunkId?: string | null;
  /** 1-based page hint, used when there is no chunk. */
  page?: number | null;
};

const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
export const isId = (v: string | null | undefined): v is string => typeof v === "string" && ID_RE.test(v);

export function documentHref(orgSlug: string, workspaceRef: string, documentId: string, versionId?: string | null): string {
  const base = `/o/${encodeURIComponent(orgSlug)}/w/${encodeURIComponent(workspaceRef)}/documents/${encodeURIComponent(documentId)}`;
  return versionId ? `${base}?v=${encodeURIComponent(versionId)}` : base;
}

export function evidenceHref(t: EvidenceTarget): string {
  const base = `/o/${encodeURIComponent(t.orgSlug)}/w/${encodeURIComponent(t.workspaceRef)}/documents/${encodeURIComponent(t.documentId)}/evidence`;
  const sp = new URLSearchParams({ v: t.versionId });
  if (t.chunkId) sp.set("chunk", t.chunkId);
  if (t.page && t.page > 0) sp.set("page", String(Math.floor(t.page)));
  return `${base}?${sp}`;
}

export type EvidenceParams = { versionId: string | null; chunkId: string | null; page: number | null };

export function parseEvidenceParams(get: (name: string) => string | null | undefined): EvidenceParams {
  const v = get("v");
  const chunk = get("chunk");
  const rawPage = get("page");
  const page = rawPage && /^\d{1,5}$/.test(rawPage) && Number(rawPage) > 0 ? Number(rawPage) : null;
  return { versionId: isId(v) ? v : null, chunkId: isId(chunk) ? chunk : null, page };
}

/** The workspace segment for a link: its slug when the caller's workspace list knows it, else the id. */
export function workspaceRefFor(workspaces: readonly Pick<Workspace, "id" | "slug">[], workspaceId: string, slugHint?: string): string {
  return slugHint ?? workspaces.find((w) => w.id === workspaceId)?.slug ?? workspaceId;
}
