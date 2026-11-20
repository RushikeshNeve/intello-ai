"use client";

import { useRef, useState } from "react";
import { GitBranchIcon, LockIcon, PlugIcon, PlusIcon, RefreshCwIcon } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/badges";
import { Modal } from "@/components/ui/overlay";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { SurfaceCard } from "@/components/ui/surface";
import { useToast } from "@/components/ui/toast";
import { useApiFetcher } from "@/lib/query/provider";
import type { OrgSummary } from "@/features/organizations/api/orgs";
import type { Workspace } from "@/features/workspaces/api";
import { canAdminWorkspace, formatWhen } from "@/features/settings/permissions";
import {
  createSource,
  isValidRepo,
  listSources,
  parseGlobs,
  revokeSource,
  sourceErrorText,
  sourceKeys,
  syncSource,
  syncStateLabel,
  type Source,
  type SourceCreated,
} from "./api";

const PLANNED = ["Google Drive", "Notion", "Slack", "Confluence"] as const;

export function IntegrationsApp({ org, workspace }: { org: OrgSummary; workspace: Workspace }) {
  const canManage = canAdminWorkspace(org, workspace);
  return (
    <div className="mx-auto grid max-w-5xl gap-6" data-workspace-scope={workspace.id} data-testid="integrations-app">
      <PageHeader eyebrow={`${org.name} · ${workspace.name}`} title="Integrations" description="Connect external sources so their files are kept in sync with this workspace." />
      {canManage ? (
        <Sources orgId={org.id} workspaceId={workspace.id} />
      ) : (
        <EmptyState
          testId="integrations-restricted"
          icon={<LockIcon className="size-5" />}
          title="Only workspace admins manage integrations"
          description="Ask an admin of this workspace to connect or change sources. You can still search and open everything that was synced."
        />
      )}
      <Catalog />
    </div>
  );
}

function Catalog() {
  return (
    <section aria-labelledby="catalog-h" className="grid gap-3" data-testid="connector-catalog">
      <h2 id="catalog-h" className="text-section font-semibold tracking-tight">
        Connector catalog
      </h2>
      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <SurfaceCard as="li" className="flex items-center gap-3 p-4">
          <GitBranchIcon aria-hidden="true" className="size-5" />
          <span className="grow text-card-title font-medium">GitHub</span>
          <StatusBadge tone="success">Available</StatusBadge>
        </SurfaceCard>
        {PLANNED.map((name) => (
          <SurfaceCard as="li" key={name} className="flex items-center gap-3 p-4 text-text-secondary">
            <PlugIcon aria-hidden="true" className="size-5" />
            <span className="grow text-card-title font-medium">{name}</span>
            <StatusBadge tone="neutral">Planned</StatusBadge>
          </SurfaceCard>
        ))}
      </ul>
      <p className="text-meta text-text-secondary">Planned connectors are not available and nothing is connected for them.</p>
    </section>
  );
}

function Sources({ orgId, workspaceId }: { orgId: string; workspaceId: string }) {
  const fetcher = useApiFetcher();
  const [adding, setAdding] = useState(false);
  const [created, setCreated] = useState<SourceCreated | null>(null);
  const q = useQuery({
    queryKey: sourceKeys.list(orgId, workspaceId),
    retry: false,
    queryFn: ({ signal }) => listSources(fetcher, orgId, workspaceId, signal),
    // keep status fresh while a sync is in flight
    refetchInterval: (query) => (query.state.data?.some((s) => s.last_sync && (s.last_sync.state === "queued" || s.last_sync.state === "running")) ? 5000 : false),
  });

  return (
    <section aria-labelledby="sources-h" className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="sources-h" className="text-section font-semibold tracking-tight">
          GitHub sources
        </h2>
        <Button onClick={() => setAdding(true)} data-testid="add-source">
          <PlusIcon aria-hidden="true" /> Add GitHub source
        </Button>
      </div>
      {created && <CreatedNotice created={created} onDismiss={() => setCreated(null)} />}
      {q.isPending ? (
        <div role="status" aria-label="Loading sources" className="grid gap-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : q.isError ? (
        <ErrorState testId="sources-error" title="We couldn't load the sources" description={sourceErrorText(q.error)} onRetry={() => void q.refetch()} />
      ) : q.data.length === 0 ? (
        <EmptyState testId="sources-empty" icon={<GitBranchIcon className="size-5" />} title="No sources connected" description="Add a GitHub repository to sync its documentation and code into this workspace." />
      ) : (
        <ul className="grid gap-3">
          {q.data.map((s) => (
            <SourceCard key={s.id} source={s} orgId={orgId} workspaceId={workspaceId} />
          ))}
        </ul>
      )}
      <AddSourceModal
        open={adding}
        onClose={() => setAdding(false)}
        orgId={orgId}
        workspaceId={workspaceId}
        onCreated={(c) => {
          setAdding(false);
          setCreated(c);
        }}
      />
    </section>
  );
}

function CreatedNotice({ created, onDismiss }: { created: SourceCreated; onDismiss: () => void }) {
  return (
    <div role="status" className="grid gap-2 rounded-card border border-border-subtle bg-info-soft p-4 text-sm" data-testid="source-created">
      <p className="font-medium">Source added. The first sync is queued.</p>
      {created.webhook_secret ? (
        <>
          <p>
            Webhook secret (shown once, copy it now): <code className="break-all rounded bg-surface px-1">{created.webhook_secret}</code>
          </p>
          {created.webhook_path && <p className="text-text-secondary">Webhook path: {created.webhook_path}</p>}
        </>
      ) : (
        <p className="text-text-secondary">Webhooks are not enabled on this server; the source syncs on a schedule.</p>
      )}
      <Button variant="outline" size="sm" className="w-fit" onClick={onDismiss}>
        Dismiss
      </Button>
    </div>
  );
}

function SourceCard({ source: s, orgId, workspaceId }: { source: Source; orgId: string; workspaceId: string }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const revoked = s.state === "revoked";
  const invalidate = () => qc.invalidateQueries({ queryKey: sourceKeys.list(orgId, workspaceId) });

  const sync = useMutation({
    mutationFn: () => syncSource(fetcher, orgId, workspaceId, s.id),
    onSuccess: (r) => {
      setError(null);
      toast.push({ title: r.state === "already_queued" ? "A sync is already queued" : "Sync queued", tone: "success" });
      void invalidate();
    },
    onError: (e) => setError(sourceErrorText(e)),
  });
  const revoke = useMutation({
    mutationFn: () => revokeSource(fetcher, orgId, workspaceId, s.id),
    onSuccess: () => {
      setError(null);
      setConfirming(false);
      toast.push({ title: "Source revoked", tone: "success" });
      void invalidate();
      // The Revoke button disappears, so focus would be lost: land on the card itself.
      setTimeout(() => cardRef.current?.focus(), 0);
    },
    onError: (e) => {
      setConfirming(false);
      setError(sourceErrorText(e));
    },
  });

  const last = syncStateLabel(s.last_sync?.state);
  return (
    <SurfaceCard as="li" className="grid gap-3 p-4" data-testid="source-card">
      <div ref={cardRef} tabIndex={-1} className="flex flex-wrap items-start justify-between gap-3 outline-none" id={`source-${s.id}`}>
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 text-card-title font-medium">
            <GitBranchIcon aria-hidden="true" className="size-4" />
            <span className="break-all">{s.repo ?? s.name}</span>
            {revoked ? <StatusBadge tone="neutral">Revoked</StatusBadge> : s.state === "error" ? <StatusBadge tone="danger">Error</StatusBadge> : <StatusBadge tone="success">Active</StatusBadge>}
          </p>
          <p className="text-meta text-text-secondary">
            Branch {s.ref ?? "default"} · {s.include.length ? `Include: ${s.include.join(", ")}` : "All supported files"} · {s.has_token ? "Token stored encrypted" : "Public access (no token)"}
          </p>
        </div>
        {!revoked && (
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={sync.isPending} onClick={() => sync.mutate()} aria-label={`Sync ${s.repo ?? s.name} now`}>
              <RefreshCwIcon aria-hidden="true" /> Sync now
            </Button>
            <Button variant="outline" size="sm" onClick={() => setConfirming(true)} aria-label={`Revoke ${s.repo ?? s.name}`}>
              Revoke
            </Button>
          </div>
        )}
      </div>
      <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-meta text-text-secondary">Sync status</dt>
          <dd>
            <StatusBadge tone={last.tone}>{last.text}</StatusBadge>
            {s.last_sync?.error_code && <span className="ml-2 text-meta text-text-secondary">({s.last_sync.error_code})</span>}
          </dd>
        </div>
        <div>
          <dt className="text-meta text-text-secondary">Last sync</dt>
          <dd>{formatWhen(s.last_sync?.finished_at ?? s.last_sync?.started_at) || "Never"}</dd>
        </div>
        <div>
          <dt className="text-meta text-text-secondary">Items</dt>
          <dd>{s.items ? `${s.items.active} synced · ${s.items.deleted} removed` : "Not reported"}</dd>
        </div>
      </dl>
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <Modal
        open={confirming}
        onClose={() => setConfirming(false)}
        title="Revoke this source?"
        description="Syncing stops immediately and the stored token is discarded. Documents already imported stay in the workspace."
        footer={
          <>
            <Button variant="outline" onClick={() => setConfirming(false)} data-autofocus>
              Cancel
            </Button>
            <Button variant="destructive" disabled={revoke.isPending} onClick={() => revoke.mutate()} data-testid="confirm-revoke">
              Revoke source
            </Button>
          </>
        }
      >
        <p className="break-all text-sm">{s.repo ?? s.name}</p>
      </Modal>
    </SurfaceCard>
  );
}

function AddSourceModal({
  open,
  onClose,
  orgId,
  workspaceId,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  orgId: string;
  workspaceId: string;
  onCreated: (c: SourceCreated) => void;
}) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const [repo, setRepo] = useState("");
  const [ref, setRef] = useState("");
  const [globs, setGlobs] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const valid = isValidRepo(repo);

  const create = useMutation({
    mutationFn: () => createSource(fetcher, orgId, workspaceId, { repo: repo.trim(), ref: ref.trim() || undefined, include: parseGlobs(globs), token: token.trim() || undefined }),
    onSuccess: (c) => {
      setRepo("");
      setRef("");
      setGlobs("");
      setToken("");
      setError(null);
      void qc.invalidateQueries({ queryKey: sourceKeys.list(orgId, workspaceId) });
      onCreated(c);
    },
    onError: (e) => setError(sourceErrorText(e)),
  });

  return (
    <Modal open={open} onClose={onClose} title="Add a GitHub source" description="Files from the repository are imported and kept in sync.">
      <form
        className="grid gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) create.mutate();
        }}
      >
        <div className="grid gap-1.5">
          <Label htmlFor="src-repo">Repository</Label>
          <Input id="src-repo" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="owner/name" autoComplete="off" aria-invalid={repo !== "" && !valid} />
          {repo !== "" && !valid && <p className="text-meta text-danger">Use the form owner/name.</p>}
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="src-ref">Branch or ref (optional)</Label>
          <Input id="src-ref" value={ref} onChange={(e) => setRef(e.target.value)} placeholder="main" autoComplete="off" />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="src-include">Include patterns (optional)</Label>
          <Input id="src-include" value={globs} onChange={(e) => setGlobs(e.target.value)} placeholder="docs/**, *.md" autoComplete="off" />
          <p className="text-meta text-text-secondary">Comma separated globs; empty means all supported files.</p>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="src-token">Access token (optional)</Label>
          <Input id="src-token" type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" />
          <p className="text-meta text-text-secondary">Only needed for private repositories. It is stored encrypted, used read-only and never shown again. Public repositories need no token.</p>
        </div>
        {error && (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={!valid || create.isPending} data-testid="submit-source">
            Add source
          </Button>
        </div>
      </form>
    </Modal>
  );
}
