import { Suspense, type ReactNode } from "react";
import { redirect } from "next/navigation";
import { getSessionUser } from "@/features/auth/server";
import { loginUrl } from "@/features/organizations/api/http";
import { OrgScopeProvider } from "@/features/organizations/components/org-scope";
import { getOrgWorkspaces, resolveOrgScope } from "@/features/organizations/server";
import { AppHeader, userLabelOf } from "@/features/shell/app-header";
import { AppShell } from "@/features/shell/app-shell";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

/** Org shell (F1): sidebar + top bar + scoped `<main>` via `AppShell`; error states keep the slim header. */
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
      <AppShell userLabel={userLabel} initialWorkspaces={workspaces.data}>
        {children}
      </AppShell>
    </OrgScopeProvider>
  );
}
