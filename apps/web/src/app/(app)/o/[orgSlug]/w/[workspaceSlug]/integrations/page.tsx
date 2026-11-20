import { IntegrationsApp } from "@/features/integrations";
import { WorkspaceScopePage } from "@/features/settings/scope-page";

export const metadata = { title: "Integrations" };

export default function Page({ params }: PageProps<"/o/[orgSlug]/w/[workspaceSlug]/integrations">) {
  return (
    <WorkspaceScopePage params={params} segment="integrations">
      {({ org, workspace }) => <IntegrationsApp org={org} workspace={workspace} />}
    </WorkspaceScopePage>
  );
}
