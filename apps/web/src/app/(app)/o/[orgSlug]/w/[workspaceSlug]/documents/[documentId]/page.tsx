import { Suspense } from "react";
import { getDocument, getVersion, listVersions } from "@/features/documents/api";
import type { Document, Version } from "@/features/documents/types";
import { NotAvailable } from "@/features/documents/viewer/not-available";
import { DocumentViewer } from "@/features/documents/viewer/viewer";
import { classify, resolveWorkspaceScope, serverFetcher } from "@/features/organizations/server";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

export default function DocumentViewerPage({
  params,
  searchParams,
}: PageProps<"/o/[orgSlug]/w/[workspaceSlug]/documents/[documentId]">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <ViewerLoader params={params} searchParams={searchParams} />
    </Suspense>
  );
}

async function ViewerLoader({
  params,
  searchParams,
}: {
  params: Promise<{ orgSlug: string; workspaceSlug: string; documentId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { orgSlug, workspaceSlug, documentId } = await params;
  const raw = await searchParams;
  const requested = Array.isArray(raw.v) ? raw.v[0] : raw.v;
  const orgPath = `/o/${encodeURIComponent(orgSlug)}`;
  const libraryPath = `${orgPath}/w/${encodeURIComponent(workspaceSlug)}/documents`;
  const here = `${libraryPath}/${encodeURIComponent(documentId)}${requested ? `?v=${encodeURIComponent(requested)}` : ""}`;

  const scope = await resolveWorkspaceScope(orgSlug, workspaceSlug);
  if (!scope.ok) {
    if (scope.reason === "unauthenticated") return <SessionRedirect fallbackPath={here} />;
    if (scope.reason === "unavailable") return <ServiceUnavailable standalone={false} />;
    return <ScopeNotFound standalone={false} backHref={orgPath} backLabel="Back to the organization" />;
  }
  const { org, workspace } = scope.data;

  // From here on the workspace is authorized; a missing/deleted/forbidden document is one calm state.
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

  // `?v=` picks the version; default = latest. A version of another document never shows.
  let selected: Version | null = null;
  const wantedId = requested ?? doc.latest_version?.id ?? versions[0]?.id;
  if (wantedId) {
    selected = versions.find((v) => v.id === wantedId) ?? null;
    if (!selected && requested) {
      try {
        const one = await getVersion(serverFetcher, org.id, workspace.id, documentId, requested);
        selected = one.document_id === doc.id ? one : null;
      } catch (err) {
        if (classify(err) === "unauthenticated") return <SessionRedirect fallbackPath={here} />;
        selected = null;
      }
    }
  }

  return (
    <DocumentViewer
      orgId={org.id}
      workspaceId={workspace.id}
      libraryPath={libraryPath}
      document={doc}
      versions={versions}
      selected={selected}
    />
  );
}
