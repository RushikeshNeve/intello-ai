import { Suspense, type ReactNode } from "react";
import { redirect } from "next/navigation";
import { getSessionUser } from "@/features/auth/server";
import { loginUrl } from "@/features/organizations/api/http";
import { CommandPalette } from "@/features/palette/command-palette";
import { OrgScopeProvider, ScopedContent } from "@/features/organizations/components/org-scope";
import { OrgSwitcher } from "@/features/organizations/components/org-switcher";
import { getOrgWorkspaces, resolveOrgScope } from "@/features/organizations/server";
import { AppHeader, userLabelOf } from "@/features/shell/app-header";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";
import { WorkspaceSidebar } from "@/features/workspaces/components/workspace-sidebar";

/** Org shell: header with the org switcher, workspace sidebar, and the scoped `<main>`. */
export default function OrgLayout({ children, params }: LayoutProps<"/o/[orgSlug]">) {
  return (
    <Suspense fallback={<PageLoading />}>
      <OrgShell params={params}>{children}</OrgShell>
    </Suspense>
  );
}

async function OrgShell({ params, children }: { params: Promise<{ orgSlug: string }>; children: ReactNode }) {
  const { orgSlug } = await params;
  const path = `/o/${encodeURIComponent(orgSlug)}`;
  const user = await getSessionUser();
  if (!user) redirect(loginUrl(path));
  const userLabel = userLabelOf(user);

  const scope = await resolveOrgScope(orgSlug);
  if (!scope.ok) {
    if (scope.reason === "unauthenticated") return <SessionRedirect fallbackPath={path} />;
    return (
      <>
        <AppHeader userLabel={userLabel} />
        {scope.reason === "not_found" ? <ScopeNotFound /> : <ServiceUnavailable />}
      </>
    );
  }
  const { org, orgs } = scope.data;
  const workspaces = await getOrgWorkspaces(org.id);
  if (!workspaces.ok) {
    if (workspaces.reason === "unauthenticated") return <SessionRedirect fallbackPath={path} />;
    return (
      <>
        <AppHeader userLabel={userLabel} />
        {workspaces.reason === "not_found" ? <ScopeNotFound /> : <ServiceUnavailable />}
      </>
    );
  }

  return (
    <OrgScopeProvider org={org} orgs={orgs}>
      <AppHeader
        userLabel={userLabel}
        scope={
          <>
            <OrgSwitcher />
            <CommandPalette />
          </>
        }
      />
      <ScopedContent>
        <div className="flex flex-1 flex-col md:flex-row" data-org-scope={org.id}>
          <WorkspaceSidebar initialWorkspaces={workspaces.data} />
          <main id="main" className="min-w-0 flex-1 px-4 py-6 md:px-8">
            {children}
          </main>
        </div>
      </ScopedContent>
    </OrgScopeProvider>
  );
}
