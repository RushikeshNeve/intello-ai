import { WorkspaceHome as WorkspaceHomeView } from "@/features/documents/home/workspace-home";
import { Suspense } from "react";
import { resolveWorkspaceScope } from "@/features/organizations/server";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

const WORKSPACE_ROLE_LABEL = { admin: "Admin", editor: "Editor", viewer: "Viewer" } as const;

export default function WorkspacePage({ params }: PageProps<"/o/[orgSlug]/w/[workspaceSlug]">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <WorkspaceHome params={params} />
    </Suspense>
  );
}

async function WorkspaceHome({ params }: { params: Promise<{ orgSlug: string; workspaceSlug: string }> }) {
  const { orgSlug, workspaceSlug } = await params;
  const orgPath = `/o/${encodeURIComponent(orgSlug)}`;
  const path = `${orgPath}/w/${encodeURIComponent(workspaceSlug)}`;
  const scope = await resolveWorkspaceScope(orgSlug, workspaceSlug);
  if (!scope.ok) {
    if (scope.reason === "unauthenticated") return <SessionRedirect fallbackPath={path} />;
    if (scope.reason === "unavailable") return <ServiceUnavailable standalone={false} />;
    return <ScopeNotFound standalone={false} backHref={orgPath} backLabel="Back to the organization" />;
  }
  const { workspace, org } = scope.data;
  return (
    <WorkspaceHomeView
      orgId={org.id}
      orgSlug={orgSlug}
      orgName={org.name}
      workspaceId={workspace.id}
      workspaceSlug={workspaceSlug}
      workspaceName={workspace.name}
      roleLabel={workspace.role ? WORKSPACE_ROLE_LABEL[workspace.role] : null}
    />
  );
}
