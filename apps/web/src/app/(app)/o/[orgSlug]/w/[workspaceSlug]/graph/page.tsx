import Link from "next/link";
import { Suspense } from "react";
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
    <div className="mx-auto grid max-w-5xl gap-4">
      <div className="grid gap-1">
        <Link href={path} className="text-xs text-muted-foreground underline">
          {workspace.name}
        </Link>
        <h1 tabIndex={-1} className="text-2xl font-semibold tracking-tight outline-none" data-testid="graph-heading">
          Knowledge graph
        </h1>
        <p className="text-sm text-muted-foreground">Explore how people, products and policies in this workspace connect, and where each connection comes from.</p>
      </div>
      <GraphExplorer orgId={org.id} orgSlug={orgSlug} workspaceId={workspace.id} workspaceRef={workspace.slug} />
    </div>
  );
}
