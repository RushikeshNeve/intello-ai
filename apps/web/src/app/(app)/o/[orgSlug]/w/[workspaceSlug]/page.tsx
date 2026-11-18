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
    <div className="mx-auto grid max-w-4xl gap-6" data-workspace-scope={workspace.id}>
      <div className="grid gap-1">
        <p className="text-xs text-muted-foreground">{org.name}</p>
        <h1 tabIndex={-1} className="text-2xl font-semibold tracking-tight outline-none" data-testid="workspace-heading">
          {workspace.name}
        </h1>
        <p className="text-sm text-muted-foreground">
          Workspace{workspace.role ? ` · your role: ${WORKSPACE_ROLE_LABEL[workspace.role]}` : ""}
        </p>
      </div>
      <section aria-labelledby="ws-content-heading" className="rounded-lg border border-dashed border-border p-6">
        <h2 id="ws-content-heading" className="text-base font-medium">
          Documents
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Nothing here yet. Sources and documents arrive in a later milestone.
        </p>
      </section>
    </div>
  );
}
