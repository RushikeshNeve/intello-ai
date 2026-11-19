import { Suspense } from "react";
import { getOrgWorkspaces, resolveOrgScope } from "@/features/organizations/server";
import { SearchPage } from "@/features/search";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

export default function OrgSearchPage({ params }: PageProps<"/o/[orgSlug]/search">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <SearchLoader params={params} />
    </Suspense>
  );
}

async function SearchLoader({ params }: { params: Promise<{ orgSlug: string }> }) {
  const { orgSlug } = await params;
  const path = `/o/${encodeURIComponent(orgSlug)}/search`;
  // Deduplicated with the layout's reads (React `cache`, same request).
  const scope = await resolveOrgScope(orgSlug);
  if (!scope.ok) {
    if (scope.reason === "unauthenticated") return <SessionRedirect fallbackPath={path} />;
    return scope.reason === "not_found" ? <ScopeNotFound standalone={false} /> : <ServiceUnavailable standalone={false} />;
  }
  const workspaces = await getOrgWorkspaces(scope.data.org.id);
  if (!workspaces.ok) {
    if (workspaces.reason === "unauthenticated") return <SessionRedirect fallbackPath={path} />;
    return <ServiceUnavailable standalone={false} />;
  }
  return <SearchPage orgId={scope.data.org.id} orgSlug={orgSlug} initialWorkspaces={workspaces.data} />;
}
