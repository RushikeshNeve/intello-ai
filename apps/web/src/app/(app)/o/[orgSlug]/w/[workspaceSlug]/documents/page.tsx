import { Suspense } from "react";
import { listDocuments } from "@/features/documents/api";
import { Library } from "@/features/documents/library/library";
import { paramsKey, parseLibraryState, toApiParams } from "@/features/documents/library/url-state";
import type { DocumentList } from "@/features/documents/types";
import { classify, resolveWorkspaceScope, serverFetcher } from "@/features/organizations/server";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

export default function DocumentsPage({ params, searchParams }: PageProps<"/o/[orgSlug]/w/[workspaceSlug]/documents">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <DocumentsLibrary params={params} searchParams={searchParams} />
    </Suspense>
  );
}

function toUrlSearchParams(raw: Record<string, string | string[] | undefined>): URLSearchParams {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(raw)) {
    const first = Array.isArray(v) ? v[0] : v;
    if (first !== undefined) sp.set(k, first);
  }
  return sp;
}

async function DocumentsLibrary({
  params,
  searchParams,
}: {
  params: Promise<{ orgSlug: string; workspaceSlug: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { orgSlug, workspaceSlug } = await params;
  const raw = await searchParams;
  const orgPath = `/o/${encodeURIComponent(orgSlug)}`;
  const wsPath = `${orgPath}/w/${encodeURIComponent(workspaceSlug)}`;
  const scope = await resolveWorkspaceScope(orgSlug, workspaceSlug);
  if (!scope.ok) {
    if (scope.reason === "unauthenticated") return <SessionRedirect fallbackPath={`${wsPath}/documents`} />;
    if (scope.reason === "unavailable") return <ServiceUnavailable standalone={false} />;
    return <ScopeNotFound standalone={false} backHref={orgPath} backLabel="Back to the organization" />;
  }
  const { org, workspace } = scope.data;

  // First view rendered on the server from the very same URL state the browser will derive.
  const state = parseLibraryState(toUrlSearchParams(raw));
  let initial: { key: string; data: DocumentList } | undefined;
  try {
    initial = { key: paramsKey(state), data: await listDocuments(serverFetcher, org.id, workspace.id, toApiParams(state)) };
  } catch (err) {
    const reason = classify(err);
    if (reason === "unauthenticated") return <SessionRedirect fallbackPath={`${wsPath}/documents`} />;
    if (reason === "not_found") return <ScopeNotFound standalone={false} backHref={orgPath} backLabel="Back to the organization" />;
    // Anything else (invalid cursor, API hiccup): the client fetches again and shows its own calm state.
  }

  return (
    <div className="mx-auto grid max-w-5xl gap-6" data-workspace-scope={workspace.id}>
      <div className="grid gap-1">
        <p className="text-xs text-muted-foreground">
          {org.name} · {workspace.name}
        </p>
        <h1 tabIndex={-1} className="text-2xl font-semibold tracking-tight outline-none" data-testid="documents-heading">
          Documents
        </h1>
      </div>
      <Library orgId={org.id} workspaceId={workspace.id} basePath={`${wsPath}/documents`} initial={initial} />
    </div>
  );
}
