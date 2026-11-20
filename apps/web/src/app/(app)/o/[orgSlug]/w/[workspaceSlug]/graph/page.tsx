import Link from "next/link";
import { Suspense } from "react";
import { PageHeader } from "@/components/ui/page-header";
import { GraphExplorer } from "@/features/graph";
import { resolveWorkspaceScope } from "@/features/organizations/server";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

export default function GraphPage({ params }: PageProps<"/o/[orgSlug]/w/[workspaceSlug]/graph">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <GraphLoader params={params} />
    </Suspense>
  );
}

async function GraphLoader({ params }: { params: Promise<{ orgSlug: string; workspaceSlug: string }> }) {
  const { orgSlug, workspaceSlug } = await params;
  const orgPath = `/o/${encodeURIComponent(orgSlug)}`;
  const path = `${orgPath}/w/${encodeURIComponent(workspaceSlug)}`;
  const scope = await resolveWorkspaceScope(orgSlug, workspaceSlug);
  if (!scope.ok) {
    if (scope.reason === "unauthenticated") return <SessionRedirect fallbackPath={`${path}/graph`} />;
    if (scope.reason === "unavailable") return <ServiceUnavailable standalone={false} />;
    return <ScopeNotFound standalone={false} backHref={orgPath} backLabel="Back to the organization" />;
  }
  const { workspace, org } = scope.data;
  return (
    <div className="grid gap-5">
      <PageHeader
        eyebrow="Relationships, made visible"
        title="Knowledge graph"
        titleTestId="graph-heading"
        description="Explore how people, products and policies in this workspace connect, and where each connection comes from."
        actions={
          <Link href={path} className="focus-ring rounded-control border border-border bg-card px-3 py-1.5 text-sm font-medium hover:bg-muted">
            {workspace.name}
          </Link>
        }
      />
      <GraphExplorer orgId={org.id} orgSlug={orgSlug} workspaceId={workspace.id} workspaceRef={workspace.slug} />
    </div>
  );
}
