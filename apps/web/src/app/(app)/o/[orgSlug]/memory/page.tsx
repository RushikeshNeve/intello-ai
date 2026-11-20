import { Suspense } from "react";
import { MemoryApp } from "@/features/memory";
import { getOrgWorkspaces, resolveOrgScope } from "@/features/organizations/server";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

export default function OrgMemoryPage({ params }: PageProps<"/o/[orgSlug]/memory">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <MemoryLoader params={params} />
    </Suspense>
  );
}

async function MemoryLoader({ params }: { params: Promise<{ orgSlug: string }> }) {
  const { orgSlug } = await params;
  const path = `/o/${encodeURIComponent(orgSlug)}/memory`;
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
  return <MemoryApp orgId={scope.data.org.id} orgSlug={orgSlug} workspaces={workspaces.data} />;
}
