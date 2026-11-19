import { Suspense } from "react";
import { getDocument, getVersion, listVersions } from "@/features/documents/api";
import type { Document, Version } from "@/features/documents/types";
import { NotAvailable } from "@/features/documents/viewer/not-available";
import { EvidenceViewer } from "@/features/evidence";
import { parseEvidenceParams } from "@/features/evidence/links";
import { classify, getOrgWorkspaces, resolveOrgScope, serverFetcher } from "@/features/organizations/server";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

export default function EvidencePage({
  params,
  searchParams,
}: PageProps<"/o/[orgSlug]/w/[workspaceSlug]/documents/[documentId]/evidence">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <EvidenceLoader params={params} searchParams={searchParams} />
    </Suspense>
  );
}

const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

async function EvidenceLoader({
  params,
  searchParams,
}: {
  params: Promise<{ orgSlug: string; workspaceSlug: string; documentId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { orgSlug, workspaceSlug, documentId } = await params;
  const raw = await searchParams;
  const evidence = parseEvidenceParams((name) => first(raw[name]));
  const orgPath = `/o/${encodeURIComponent(orgSlug)}`;
  const here = `${orgPath}/w/${encodeURIComponent(workspaceSlug)}/documents/${encodeURIComponent(documentId)}/evidence${
    evidence.versionId ? `?v=${encodeURIComponent(evidence.versionId)}${evidence.chunkId ? `&chunk=${encodeURIComponent(evidence.chunkId)}` : ""}` : ""
  }`;

  const scope = await resolveOrgScope(orgSlug);
  if (!scope.ok) {
    if (scope.reason === "unauthenticated") return <SessionRedirect fallbackPath={here} />;
    if (scope.reason === "unavailable") return <ServiceUnavailable standalone={false} />;
    return <ScopeNotFound standalone={false} backHref="/home" />;
  }
  const { org } = scope.data;
  const workspaces = await getOrgWorkspaces(org.id);
  if (!workspaces.ok) {
    if (workspaces.reason === "unauthenticated") return <SessionRedirect fallbackPath={here} />;
    if (workspaces.reason === "unavailable") return <ServiceUnavailable standalone={false} />;
    return <ScopeNotFound standalone={false} backHref={orgPath} backLabel="Back to the organization" />;
  }
  // Chat citations carry a workspace id, search results a slug: both resolve, but only to the caller's own workspaces.
  const workspace = workspaces.data.find((w) => w.slug === workspaceSlug || w.id === workspaceSlug);
  if (!workspace) return <ScopeNotFound standalone={false} backHref={orgPath} backLabel="Back to the organization" />;

  const libraryPath = `${orgPath}/w/${encodeURIComponent(workspace.slug)}/documents`;
  let doc: Document;
  let versions: Version[];
  try {
    doc = await getDocument(serverFetcher, org.id, workspace.id, documentId);
    if (doc.workspace_id !== workspace.id) throw new Error("document of another workspace");
    versions = (await listVersions(serverFetcher, org.id, workspace.id, documentId, { limit: 200 })).items;
  } catch (err) {
    const reason = classify(err);
    if (reason === "unauthenticated") return <SessionRedirect fallbackPath={here} />;
    if (reason === "unavailable") return <ServiceUnavailable standalone={false} />;
    return <NotAvailable backHref={libraryPath} />;
  }

  // The cited version is shown even when a newer one is now published; a version of another document never is.
  let version: Version | null = evidence.versionId ? (versions.find((v) => v.id === evidence.versionId) ?? null) : (versions[0] ?? null);
  if (!version && evidence.versionId) {
    try {
      const one = await getVersion(serverFetcher, org.id, workspace.id, documentId, evidence.versionId);
      version = one.document_id === doc.id ? one : null;
    } catch (err) {
      if (classify(err) === "unauthenticated") return <SessionRedirect fallbackPath={here} />;
    }
  }
  if (!version) return <NotAvailable backHref={libraryPath} />;

  const latest = versions[0] ?? doc.latest_version;
  return (
    <EvidenceViewer
      orgId={org.id}
      orgSlug={orgSlug}
      workspaceId={workspace.id}
      workspaceRef={workspace.slug}
      document={{ id: doc.id, title: doc.title }}
      version={version}
      latest={latest ? { id: latest.id, version_number: latest.version_number } : null}
      chunkId={evidence.chunkId}
      page={evidence.page}
    />
  );
}
