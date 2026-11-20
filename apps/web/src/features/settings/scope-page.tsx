import type { ReactNode } from "react";
import { Suspense } from "react";
import { resolveWorkspaceScope, type WorkspaceScopeData } from "@/features/organizations/server";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

type Params = Promise<{ orgSlug: string; workspaceSlug: string }>;
type Render = (scope: WorkspaceScopeData) => ReactNode;

/**
 * Server helper shared by the four management pages: resolves org + workspace from the URL (own memberships only)
 * and renders `children(scope)`; every failure is the same calm state the other pages use.
 */
export function WorkspaceScopePage({ params, segment, children }: { params: Params; segment: string; children: Render }) {
  return (
    <Suspense fallback={<PageLoading />}>
      <Loader params={params} segment={segment}>
        {children}
      </Loader>
    </Suspense>
  );
}

async function Loader({ params, segment, children }: { params: Params; segment: string; children: Render }) {
  const { orgSlug, workspaceSlug } = await params;
  const orgPath = `/o/${encodeURIComponent(orgSlug)}`;
  const path = `${orgPath}/w/${encodeURIComponent(workspaceSlug)}/${segment}`;
  const scope = await resolveWorkspaceScope(orgSlug, workspaceSlug);
  if (!scope.ok) {
    if (scope.reason === "unauthenticated") return <SessionRedirect fallbackPath={path} />;
    if (scope.reason === "unavailable") return <ServiceUnavailable standalone={false} />;
    return <ScopeNotFound standalone={false} backHref={orgPath} backLabel="Back to the organization" />;
  }
  return <>{children(scope.data)}</>;
}
