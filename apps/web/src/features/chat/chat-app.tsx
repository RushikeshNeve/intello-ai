"use client";

import { useWorkspaces } from "@/features/workspaces/hooks";
import type { Workspace } from "@/features/workspaces/api";
import { Conversation } from "./conversation";
import type { RunStream } from "./sse";
import { ThreadList } from "./thread-list";

export type ChatAppProps = {
  orgId: string;
  orgSlug: string;
  initialWorkspaces: Workspace[];
  /** From the route; null on `/o/{org}/chat` (a new chat). */
  threadId: string | null;
  stream?: RunStream;
};

/** Thread list on the left, the conversation on the right. The conversation is keyed by thread, so nothing leaks across threads. */
export function ChatApp({ orgId, orgSlug, initialWorkspaces, threadId, stream }: ChatAppProps) {
  const { data: workspaces = initialWorkspaces } = useWorkspaces(orgId, initialWorkspaces);
  return (
    <div className="mx-auto grid max-w-6xl gap-6 md:grid-cols-[16rem_minmax(0,1fr)]" data-testid="chat-app">
      <ThreadList orgId={orgId} orgSlug={orgSlug} activeThreadId={threadId} />
      <Conversation key={threadId ?? "new"} orgId={orgId} orgSlug={orgSlug} workspaces={workspaces} threadId={threadId} stream={stream} />
    </div>
  );
}
