"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { PlusIcon, Trash2Icon } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useApiFetcher } from "@/lib/query/provider";
import { deleteThread, listThreads, type Thread } from "./api";
import { chatKeys } from "./keys";

export const threadTitle = (t: Pick<Thread, "title">): string => t.title.trim() || "New chat";

export function ThreadList({ orgId, orgSlug, activeThreadId }: { orgId: string; orgSlug: string; activeThreadId: string | null }) {
  const fetcher = useApiFetcher();
  const router = useRouter();
  const queryClient = useQueryClient();
  const base = `/o/${encodeURIComponent(orgSlug)}/chat`;
  const query = useInfiniteQuery({
    queryKey: chatKeys.threads(orgId),
    initialPageParam: null as string | null,
    retry: false,
    staleTime: 10_000,
    queryFn: ({ pageParam, signal }) => listThreads(fetcher, orgId, { after: pageParam }, signal),
    getNextPageParam: (last) => last.next_cursor,
  });
  const threads = query.data?.pages.flatMap((p) => p.items) ?? [];
  const [deleting, setDeleting] = useState<Thread | null>(null);

  async function confirmDelete(t: Thread) {
    await deleteThread(fetcher, orgId, t.id);
    queryClient.removeQueries({ queryKey: chatKeys.thread(orgId, t.id) });
    await queryClient.invalidateQueries({ queryKey: chatKeys.threads(orgId) });
    setDeleting(null);
    if (t.id === activeThreadId) router.push(base);
  }

  return (
    <nav aria-label="Conversations" className="grid content-start gap-3" data-testid="thread-list">
      <Link href={base} className={buttonVariants({ variant: "outline" })} data-testid="new-chat">
        <PlusIcon aria-hidden="true" />
        New chat
      </Link>
      {query.isPending ? (
        <div role="status" aria-label="Loading conversations" className="grid gap-2">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : query.isError ? (
        <div role="alert" className="grid justify-items-start gap-2 text-sm">
          <p className="text-muted-foreground">Conversations couldn&apos;t be loaded.</p>
          <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
            Try again
          </Button>
        </div>
      ) : threads.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="no-threads">
          No conversations yet.
        </p>
      ) : (
        <ul className="grid gap-0.5">
          {threads.map((t) => (
            <li key={t.id} className="group flex items-center gap-1">
              <Link
                href={`${base}/${encodeURIComponent(t.id)}`}
                aria-current={t.id === activeThreadId ? "page" : undefined}
                className="min-w-0 flex-1 truncate rounded-md px-2 py-1.5 text-sm outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring aria-[current=page]:bg-accent aria-[current=page]:font-medium"
                data-testid="thread-link"
              >
                {threadTitle(t)}
              </Link>
              <Button variant="ghost" size="icon-sm" aria-label={`Delete conversation ${threadTitle(t)}`} onClick={() => setDeleting(t)}>
                <Trash2Icon aria-hidden="true" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      {query.hasNextPage && (
        <Button variant="ghost" size="sm" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
          {query.isFetchingNextPage ? "Loading…" : "Show older"}
        </Button>
      )}
      {deleting && <DeleteThreadDialog thread={deleting} onClose={() => setDeleting(null)} onDelete={() => confirmDelete(deleting)} />}
    </nav>
  );
}

function DeleteThreadDialog({ thread, onClose, onDelete }: { thread: Thread; onClose: () => void; onDelete: () => Promise<void> }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const cancelRef = useRef<HTMLButtonElement>(null);
  async function confirm() {
    if (pending) return;
    setPending(true);
    setError("");
    try {
      await onDelete();
    } catch {
      setError("We couldn't delete this conversation. Try again.");
      setPending(false);
    }
  }
  return (
    <Dialog
      open
      onClose={onClose}
      title="Delete this conversation?"
      description={`“${threadTitle(thread)}” and its messages will no longer be available.`}
      initialFocusRef={cancelRef}
    >
      <p role="alert" className="min-h-5 text-sm text-destructive">
        {error}
      </p>
      <div className="flex justify-end gap-2">
        <Button ref={cancelRef} type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button type="button" variant="destructive" onClick={() => void confirm()} disabled={pending}>
          {pending ? "Deleting…" : "Delete"}
        </Button>
      </div>
    </Dialog>
  );
}
