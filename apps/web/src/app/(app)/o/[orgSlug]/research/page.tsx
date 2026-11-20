import { Suspense } from "react";
import { getOrgWorkspaces, resolveOrgScope } from "@/features/organizations/server";
import { ResearchHome } from "@/features/research";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

export default function OrgResearchPage({ params }: PageProps<"/o/[orgSlug]/research">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <ResearchLoader params={params} />
    </Suspense>
  );
}

async function ResearchLoader({ params }: { params: Promise<{ orgSlug: string }> }) {
  const { orgSlug } = await params;
  const path = `/o/${encodeURIComponent(orgSlug)}/research`;
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
  return <ResearchHome orgId={scope.data.org.id} orgSlug={orgSlug} workspaces={workspaces.data} />;
}
