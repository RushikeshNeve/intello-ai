import { Suspense } from "react";
import { getOrgWorkspaces, resolveOrgScope } from "@/features/organizations/server";
import { ResearchSessionView } from "@/features/research";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

export default function OrgResearchSessionPage({ params }: PageProps<"/o/[orgSlug]/research/[id]">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <SessionLoader params={params} />
    </Suspense>
  );
}

async function SessionLoader({ params }: { params: Promise<{ orgSlug: string; id: string }> }) {
  const { orgSlug, id } = await params;
  const path = `/o/${encodeURIComponent(orgSlug)}/research/${encodeURIComponent(id)}`;
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
  return <ResearchSessionView orgId={scope.data.org.id} orgSlug={orgSlug} sessionId={id} workspaces={workspaces.data} />;
}
