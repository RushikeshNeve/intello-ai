import { SettingsApp } from "@/features/settings";
import { WorkspaceScopePage } from "@/features/settings/scope-page";

export const metadata = { title: "Settings" };

export default function Page({ params }: PageProps<"/o/[orgSlug]/w/[workspaceSlug]/settings">) {
  return (
    <WorkspaceScopePage params={params} segment="settings">
      {({ org, workspace }) => <SettingsApp org={org} workspace={workspace} />}
    </WorkspaceScopePage>
  );
}
