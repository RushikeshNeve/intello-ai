"use client";

import { useCallback, useState } from "react";
import { EvidencePanel, type PanelTarget } from "@/features/evidence";
import { workspaceRefFor } from "@/features/evidence/links";
import { useWorkspaces } from "@/features/workspaces/hooks";
import type { Workspace } from "@/features/workspaces/api";
import type { Citation } from "./api";
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

/**
 * Chat workspace: thread list | conversation | persistent evidence panel (~345px). Below 1100px the three stack,
 * panel under the conversation.
 */
export const CHAT_GRID_CLASS = "grid min-w-0 gap-4 min-[1100px]:grid-cols-[14rem_minmax(0,1fr)_345px] min-[1100px]:items-start";
export const PANEL_COLUMN_CLASS = "min-w-0 min-[1100px]:sticky min-[1100px]:top-0";

export function targetFromCitation(c: Citation, workspaces: readonly Workspace[]): PanelTarget {
  return {
    workspaceId: c.workspace_id,
    workspaceRef: workspaceRefFor(workspaces, c.workspace_id),
    documentId: c.document_id,
    versionId: c.version_id,
    chunkId: c.chunk_id,
    title: c.title,
    versionNumber: c.version_number,
    page: c.page_start,
  };
}

/** The conversation is keyed by thread, so nothing leaks across threads; the selection is scoped to its thread too. */
export function ChatApp({ orgId, orgSlug, initialWorkspaces, threadId, stream }: ChatAppProps) {
  const { data: workspaces = initialWorkspaces } = useWorkspaces(orgId, initialWorkspaces);
  const [selected, setSelected] = useState<{ thread: string; citation: Citation } | null>(null);
  const key = threadId ?? "new";
  const active = selected && selected.thread === key ? selected.citation : null;

  const onCite = useCallback(
    (c: Citation) => {
      setSelected({ thread: key, citation: c });
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("cite", c.chunk_id);
        window.history.replaceState(window.history.state, "", url);
      } catch {
        // URL state is a convenience only.
      }
    },
    [key],
  );

  return (
    <div className={CHAT_GRID_CLASS} data-testid="chat-app">
      <ThreadList orgId={orgId} orgSlug={orgSlug} activeThreadId={threadId} />
      <Conversation
        key={key}
        orgId={orgId}
        orgSlug={orgSlug}
        workspaces={workspaces}
        threadId={threadId}
        stream={stream}
        onCite={onCite}
        activeChunkId={active?.chunk_id ?? null}
      />
      <div className={PANEL_COLUMN_CLASS}>
        <EvidencePanel orgId={orgId} orgSlug={orgSlug} target={active ? targetFromCitation(active, workspaces) : null} />
      </div>
    </div>
  );
}
