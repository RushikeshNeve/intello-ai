"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { FileTextIcon, SparklesIcon, UploadIcon } from "lucide-react";
import { buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FileTypeBadge } from "@/components/ui/badges";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { MetricCard, SurfaceCard } from "@/components/ui/surface";
import { useApiFetcher } from "@/lib/query/provider";
import { listDocuments } from "../api";
import { formatDate } from "../format";
import { IngestionBadge } from "../ingestion";
import { documentKeys } from "../keys";
import { getOverview, overviewKey } from "@/features/workspaces/overview-api";

/** Page size of the recent-documents list (newest first). */
export const HOME_PAGE_SIZE = 25;
const RECENT = 5;

export type WorkspaceHomeProps = {
  orgId: string;
  orgSlug: string;
  workspaceId: string;
  workspaceSlug: string;
  workspaceName: string;
  orgName: string;
  roleLabel: string | null;
};

/**
 * Workspace home. Tiles come from `GET {W}/overview` (permission-filtered counts; hidden when it fails, never
 * estimated); the recent list comes from `GET {W}/documents`.
 */
export function WorkspaceHome(p: WorkspaceHomeProps) {
  const router = useRouter();
  const fetcher = useApiFetcher();
  const [ask, setAsk] = useState("");
  const base = `/o/${encodeURIComponent(p.orgSlug)}`;
  const wsBase = `${base}/w/${encodeURIComponent(p.workspaceSlug)}`;

  const query = useQuery({
    queryKey: documentKeys.list(p.orgId, p.workspaceId, "home"),
    queryFn: () => listDocuments(fetcher, p.orgId, p.workspaceId, { sort: "updated_at", order: "desc", limit: HOME_PAGE_SIZE }),
  });
  const data = query.data;
  const items = data?.items ?? [];
  const overview = useQuery({
    queryKey: overviewKey(p.orgId, p.workspaceId),
    retry: false,
    queryFn: ({ signal }) => getOverview(fetcher, p.orgId, p.workspaceId, signal),
  });
  const ov = overview.data;

  function submit(e: FormEvent) {
    e.preventDefault();
    const text = ask.trim();
    router.push(text ? `${base}/chat?q=${encodeURIComponent(text)}` : `${base}/chat`);
  }

  return (
    <div className="mx-auto grid max-w-5xl gap-6" data-workspace-scope={p.workspaceId}>
      <PageHeader
        eyebrow={p.orgName}
        title={p.workspaceName}
        titleTestId="workspace-heading"
        description={`Workspace${p.roleLabel ? ` · your role: ${p.roleLabel}` : ""}`}
        actions={
          <Link href={`${wsBase}/documents`} className={buttonVariants()} data-testid="open-documents">
            <UploadIcon aria-hidden="true" />
            Add knowledge
          </Link>
        }
      />

      <form onSubmit={submit} role="search" aria-label="Quick ask" className="flex gap-2" data-testid="quick-ask">
        <div className="relative flex-1">
          <SparklesIcon aria-hidden="true" className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-brand-violet" />
          <Input
            aria-label="Ask Intello AI"
            value={ask}
            onChange={(e) => setAsk(e.target.value)}
            placeholder="Ask a question about your knowledge…"
            autoComplete="off"
            className="h-11 rounded-card pl-9 text-base"
          />
        </div>
        <button type="submit" className={buttonVariants({ className: "h-11 px-5" })}>
          Ask
        </button>
      </form>

      {overview.isPending ? (
        <section aria-label="Workspace figures" className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6" data-testid="home-metrics">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="h-28 rounded-card" />
          ))}
        </section>
      ) : ov ? (
        <section aria-label="Workspace figures" className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6" data-testid="home-metrics">
          <MetricCard label="Documents" value={ov.documents_total} icon={<FileTextIcon className="size-4" />} />
          <MetricCard label="Ready to search" value={ov.documents.ready} />
          <MetricCard label="Processing" value={ov.documents.queued + ov.documents.processing} />
          <MetricCard label="Sources" value={ov.sources} />
          <MetricCard label="Memories to review" value={ov.pending_memories} />
          <MetricCard label="Research sessions" value={ov.research.total} />
        </section>
      ) : null}

      <section aria-labelledby="recent-docs-heading" className="grid gap-3">
        <div className="flex items-center justify-between gap-3">
          <h2 id="recent-docs-heading" className="text-section font-semibold text-brand-indigo dark:text-foreground">
            Recent documents
          </h2>
          <Link href={`${wsBase}/documents`} className="focus-ring rounded text-sm font-medium text-brand-blue">
            Open library
          </Link>
        </div>
        {query.isPending ? (
          <div role="status" aria-label="Loading documents" className="grid gap-2">
            <Skeleton className="h-14 rounded-control" />
            <Skeleton className="h-14 rounded-control" />
          </div>
        ) : query.isError ? (
          <ErrorState title="We couldn't load the documents" description="Your files are safe." onRetry={() => void query.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            title="No documents yet"
            description="Add a file to start building this workspace's knowledge."
            action={
              <Link href={`${wsBase}/documents`} className={buttonVariants({ variant: "outline" })}>
                Open the library
              </Link>
            }
          />
        ) : (
          <ul className="grid gap-2" data-testid="recent-documents">
            {items.slice(0, RECENT).map((d) => (
              <SurfaceCard as="li" key={d.id} className="flex items-center gap-3 p-3" data-testid="recent-document">
                <FileTypeBadge type={d.latest_version?.content_type ?? d.latest_version?.original_filename} className="shrink-0" />
                <div className="min-w-0 flex-1">
                  <Link href={`${wsBase}/documents/${encodeURIComponent(d.id)}`} className="focus-ring block truncate rounded font-semibold text-brand-indigo hover:underline dark:text-foreground">
                    {d.title}
                  </Link>
                  <time dateTime={d.updated_at} className="text-meta text-text-secondary">
                    Updated {formatDate(d.updated_at)}
                  </time>
                </div>
                <IngestionBadge summary={d.ingestion} />
              </SurfaceCard>
            ))}
          </ul>
        )}
      </section>

      <nav aria-label="Explore" className="flex flex-wrap gap-2">
        <Link href={`${wsBase}/graph`} className={buttonVariants({ variant: "outline" })} data-testid="open-graph">
          Explore the graph
        </Link>
        <Link href={`${base}/search`} className={buttonVariants({ variant: "outline" })}>
          Universal Search
        </Link>
      </nav>
    </div>
  );
}
