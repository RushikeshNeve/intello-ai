import { EvalsApp } from "@/features/evals";
import { WorkspaceScopePage } from "@/features/settings/scope-page";

export const metadata = { title: "Golden sets" };

export default function Page({ params }: PageProps<"/o/[orgSlug]/w/[workspaceSlug]/evals">) {
  return (
    <WorkspaceScopePage params={params} segment="evals">
      {({ org, workspace }) => <EvalsApp org={org} workspace={workspace} />}
    </WorkspaceScopePage>
  );
}
