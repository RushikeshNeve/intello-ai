import { ActivityApp } from "@/features/activity";
import { WorkspaceScopePage } from "@/features/settings/scope-page";

export const metadata = { title: "Activity" };

export default function Page({ params }: PageProps<"/o/[orgSlug]/w/[workspaceSlug]/activity">) {
  return (
    <WorkspaceScopePage params={params} segment="activity">
      {({ org, workspace }) => <ActivityApp org={org} workspace={workspace} />}
    </WorkspaceScopePage>
  );
}
