import { ChatApp } from "@/features/chat";
import { getOrgWorkspaces, resolveOrgScope } from "@/features/organizations/server";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { ScopeNotFound, ServiceUnavailable } from "@/features/shell/states";

/** Server side of `/o/{org}/chat[/{thread}]`: authorizes the org, then hands over to the client app. */
export async function ChatLoader({ params }: { params: Promise<{ orgSlug: string; threadId?: string }> }) {
  const { orgSlug, threadId = null } = await params;
  const path = `/o/${encodeURIComponent(orgSlug)}/chat${threadId ? `/${encodeURIComponent(threadId)}` : ""}`;
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
  return <ChatApp orgId={scope.data.org.id} orgSlug={orgSlug} initialWorkspaces={workspaces.data} threadId={threadId} />;
}
