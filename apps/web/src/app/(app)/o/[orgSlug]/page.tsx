import { Suspense } from "react";
import { PageHeader } from "@/components/ui/page-header";
import { ROLE_LABEL } from "@/features/organizations/api/orgs";
import { getOrgWorkspaces, resolveOrgScope } from "@/features/organizations/server";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";
import { WorkspaceOverview } from "@/features/workspaces/components/workspace-overview";

export default function OrgPage({ params }: PageProps<"/o/[orgSlug]">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <OrgOverview params={params} />
    </Suspense>
  );
}

async function OrgOverview({ params }: { params: Promise<{ orgSlug: string }> }) {
  const { orgSlug } = await params;
  const path = `/o/${encodeURIComponent(orgSlug)}`;
  // Deduplicated with the layout's reads (React `cache`, same request).
  const scope = await resolveOrgScope(orgSlug);
  if (!scope.ok) {
    if (scope.reason === "unauthenticated") return <SessionRedirect fallbackPath={path} />;
    return scope.reason === "not_found" ? <ScopeNotFound standalone={false} /> : <ServiceUnavailable standalone={false} />;
  }
  const { org } = scope.data;
  const workspaces = await getOrgWorkspaces(org.id);
  if (!workspaces.ok) {
    if (workspaces.reason === "unauthenticated") return <SessionRedirect fallbackPath={path} />;
    return <ServiceUnavailable standalone={false} />;
  }
  return (
    <div className="mx-auto grid max-w-4xl gap-6">
      <PageHeader eyebrow="Organization" title={org.name} titleTestId="org-heading" description={`Your role: ${ROLE_LABEL[org.role]}`} />
      <WorkspaceOverview initialWorkspaces={workspaces.data} />
    </div>
  );
}
