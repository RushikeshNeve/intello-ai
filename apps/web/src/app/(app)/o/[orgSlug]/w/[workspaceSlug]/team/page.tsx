import { TeamApp } from "@/features/team";
import { WorkspaceScopePage } from "@/features/settings/scope-page";

export const metadata = { title: "People and access" };

export default function Page({ params }: PageProps<"/o/[orgSlug]/w/[workspaceSlug]/team">) {
  return (
    <WorkspaceScopePage params={params} segment="team">
      {({ org, workspace }) => <TeamApp org={org} workspace={workspace} />}
    </WorkspaceScopePage>
  );
}
