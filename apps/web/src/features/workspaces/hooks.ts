"use client";

import { useQuery } from "@tanstack/react-query";
import { queryKeys } from "@/lib/query/keys";
import { useApiFetcher } from "@/lib/query/provider";
import { listWorkspaces, type Workspace } from "./api";

/** Workspaces of one org, keyed by org id (so an org switch can never serve another org's list). */
export function useWorkspaces(orgId: string, initialData?: Workspace[]) {
  const fetcher = useApiFetcher();
  return useQuery({
    queryKey: queryKeys.workspaces(orgId),
    queryFn: () => listWorkspaces(fetcher, orgId),
    initialData,
  });
}
