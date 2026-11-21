"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { GaugeIcon, LockIcon } from "lucide-react";
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
import { Tabs, tabId, tabPanelId } from "@/components/ui/tabs";
import { useToast } from "@/components/ui/toast";
import { useApiFetcher } from "@/lib/query/provider";
import type { OrgSummary } from "@/features/organizations/api/orgs";
import type { Workspace } from "@/features/workspaces/api";
import { StylePanel } from "./style-panel";
import { actionError, canAdminWorkspace, formatWhen, isOrgAdmin } from "./permissions";
import { AI_PROVIDERS, deleteWorkspace, getAiPolicy, getUsage, usageKey, USAGE_GROUPS, type UsageGroup, PROVIDER_LABEL, policyKey, putAiPolicy, renameWorkspace, type AiPolicy, type AiProvider } from "./api";

const TABS = [
  { value: "workspace", label: "Workspace" },
  { value: "model", label: "Model policy" },
  { value: "privacy", label: "Privacy" },
  { value: "usage", label: "Usage" },
];
const ID_BASE = "settings";

export function SettingsApp({ org, workspace }: { org: OrgSummary; workspace: Workspace }) {
  const [tab, setTab] = useState("workspace");
  return (
    <div className="mx-auto grid max-w-4xl gap-6" data-workspace-scope={workspace.id} data-testid="settings-app">
      <PageHeader eyebrow={`${org.name} · ${workspace.name}`} title="Settings" description="Workspace details, how models may be used, and what happens to your data." />
      <Tabs items={TABS} value={tab} onChange={setTab} idBase={ID_BASE} label="Settings sections" />
      <div role="tabpanel" id={tabPanelId(ID_BASE, tab)} aria-labelledby={tabId(ID_BASE, tab)} tabIndex={0} className="grid gap-4 outline-none">
        {tab === "workspace" && <WorkspaceTab org={org} workspace={workspace} />}
        {tab === "model" && <ModelTab org={org} />}
        {tab === "privacy" && <PrivacyTab />}
        {tab === "usage" && <UsageTab org={org} />}
      </div>
    </div>
  );
}

function WorkspaceTab({ org, workspace }: { org: OrgSummary; workspace: Workspace }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const router = useRouter();
  const toast = useToast();
  const canEdit = canAdminWorkspace(org, workspace);
  const [name, setName] = useState(workspace.name);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [confirmSlug, setConfirmSlug] = useState("");
  const trimmed = name.trim();

  const rename = useMutation({
    mutationFn: () => renameWorkspace(fetcher, org.id, workspace.id, trimmed),
    onSuccess: () => {
      setError(null);
      toast.push({ title: "Workspace renamed", tone: "success" });
      void qc.invalidateQueries({ queryKey: ["org", org.id, "workspaces"] });
      router.refresh();
    },
    onError: (e) => setError(actionError(e)),
  });
  const del = useMutation({
    mutationFn: () => deleteWorkspace(fetcher, org.id, workspace.id),
    onSuccess: () => {
      setDeleting(false);
      void qc.invalidateQueries({ queryKey: ["org", org.id, "workspaces"] });
      router.push(`/o/${encodeURIComponent(org.slug)}`);
      router.refresh();
    },
    onError: (e) => {
      setDeleting(false);
      setError(actionError(e));
    },
  });

  return (
    <>
      <SurfaceCard className="grid gap-4 p-5" data-testid="workspace-tab">
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (canEdit && trimmed && trimmed !== workspace.name) rename.mutate();
          }}
        >
          <div className="grid gap-1.5">
            <Label htmlFor="ws-name">Workspace name</Label>
            <Input id="ws-name" value={name} onChange={(e) => setName(e.target.value)} disabled={!canEdit} maxLength={120} />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="ws-slug">Address (slug)</Label>
            <Input id="ws-slug" value={workspace.slug} readOnly aria-describedby="ws-slug-note" />
            <p id="ws-slug-note" className="text-meta text-text-secondary">
              The address cannot be changed.
            </p>
          </div>
          {canEdit ? (
            <Button type="submit" className="w-fit" disabled={!trimmed || trimmed === workspace.name || rename.isPending} data-testid="rename-save">
              Save name
            </Button>
          ) : (
            <p className="flex items-center gap-2 text-meta text-text-secondary" data-testid="workspace-readonly">
              <LockIcon aria-hidden="true" className="size-3.5" /> Only workspace admins can change these settings.
            </p>
          )}
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
        </form>
      </SurfaceCard>
      <StylePanel orgId={org.id} workspaceId={workspace.id} canEdit={canEdit} />
      {canEdit && (
        <SurfaceCard className="grid gap-3 border-danger/40 p-5" data-testid="danger-zone">
          <h2 className="text-card-title font-semibold text-danger">Delete workspace</h2>
          <p className="text-sm text-text-secondary">Deleting hides this workspace and its documents for everyone. This cannot be undone from here.</p>
          <Button variant="destructive" className="w-fit" onClick={() => setDeleting(true)} data-testid="delete-open">
            Delete workspace
          </Button>
        </SurfaceCard>
      )}
      <Modal
        open={deleting}
        onClose={() => {
          setDeleting(false);
          setConfirmSlug("");
        }}
        title="Delete this workspace?"
        description={`Type "${workspace.slug}" to confirm.`}
        footer={
          <>
            <Button variant="outline" onClick={() => setDeleting(false)}>
              Cancel
            </Button>
            <Button variant="destructive" disabled={confirmSlug !== workspace.slug || del.isPending} onClick={() => del.mutate()} data-testid="delete-confirm">
              Delete workspace
            </Button>
          </>
        }
      >
        <Label htmlFor="del-confirm" className="sr-only">
          Workspace address
        </Label>
        <Input id="del-confirm" value={confirmSlug} onChange={(e) => setConfirmSlug(e.target.value)} autoComplete="off" />
      </Modal>
    </>
  );
}

function ModelTab({ org }: { org: OrgSummary }) {
  const fetcher = useApiFetcher();
  const q = useQuery({ queryKey: policyKey(org.id), retry: false, queryFn: () => getAiPolicy(fetcher, org.id) });
  if (q.isPending) return <Skeleton className="h-40 w-full" role="status" aria-label="Loading model policy" />;
  if (q.isError) return <ErrorState testId="policy-error" title="We couldn't load the model policy" description={actionError(q.error)} onRetry={() => void q.refetch()} />;
  return <PolicyForm orgId={org.id} policy={q.data} canEdit={isOrgAdmin(org)} />;
}

function PolicyForm({ orgId, policy, canEdit }: { orgId: string; policy: AiPolicy; canEdit: boolean }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const toast = useToast();
  const [allowExternal, setAllowExternal] = useState(policy.allow_external);
  const [selected, setSelected] = useState<AiProvider[]>(policy.allowed_providers.length ? policy.allowed_providers : ["local"]);
  const [error, setError] = useState<string | null>(null);
  const dirty = allowExternal !== policy.allow_external || (allowExternal && selected.slice().sort().join() !== policy.allowed_providers.slice().sort().join());
  const valid = !allowExternal || selected.length > 0;

  const save = useMutation({
    mutationFn: () => putAiPolicy(fetcher, orgId, allowExternal, selected),
    onSuccess: (p) => {
      setError(null);
      qc.setQueryData(policyKey(orgId), p);
      toast.push({ title: "Model policy saved", tone: "success" });
    },
    onError: (e) => setError(actionError(e)),
  });

  return (
    <SurfaceCard className="grid gap-4 p-5" data-testid="model-tab">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-card-title font-semibold">External model providers</h2>
        <StatusBadge tone={policy.allow_external ? "warning" : "success"}>{policy.allow_external ? "Allowed by this organization" : "Not allowed"}</StatusBadge>
        {policy.is_default && <StatusBadge tone="neutral">Default policy</StatusBadge>}
      </div>
      <p className="text-sm" data-testid="server-allows">
        {policy.server_allows_external
          ? "This server permits external providers when an organization allows them."
          : "This server has external providers switched off, so this setting has no effect until the server allows them."}
      </p>
      <div className="grid gap-2">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" role="switch" checked={allowExternal} disabled={!canEdit} onChange={(e) => setAllowExternal(e.target.checked)} />
          Allow sending content to external model providers
        </label>
        <fieldset className="grid gap-1 pl-6" disabled={!canEdit || !allowExternal}>
          <legend className="sr-only">Allowed providers</legend>
          {AI_PROVIDERS.map((p) => (
            <label key={p} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={allowExternal ? selected.includes(p) : p === "local"}
                onChange={(e) => setSelected((s) => (e.target.checked ? [...s, p] : s.filter((x) => x !== p)))}
              />
              {PROVIDER_LABEL[p]}
            </label>
          ))}
        </fieldset>
      </div>
      {policy.updated_at && <p className="text-meta text-text-secondary">Last changed {formatWhen(policy.updated_at)}</p>}
      {canEdit ? (
        <Button className="w-fit" disabled={!dirty || !valid || save.isPending} onClick={() => save.mutate()} data-testid="policy-save">
          Save policy
        </Button>
      ) : (
        <p className="flex items-center gap-2 text-meta text-text-secondary" data-testid="policy-readonly">
          <LockIcon aria-hidden="true" className="size-3.5" /> Only organization owners and admins can change the model policy.
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
    </SurfaceCard>
  );
}

function PrivacyTab() {
  return (
    <SurfaceCard className="grid gap-3 p-5 text-sm" data-testid="privacy-tab">
      <h2 className="text-card-title font-semibold">How your data is handled</h2>
      <ul className="grid list-disc gap-2 pl-5">
        <li>Your documents are stored per organization and are only visible to people with access to the workspace.</li>
        <li>By default, answers are produced by a model that runs on this server. Content leaves it only if an owner or admin allows external providers and the server permits them.</li>
        <li>Every answer is grounded in sources you can open. Answers without evidence are declined, not guessed.</li>
        <li>Integration tokens are stored encrypted, used read-only, and are never shown again.</li>
      </ul>
      <p className="text-meta text-text-secondary">This page is informational; there is nothing to configure here.</p>
    </SurfaceCard>
  );
}

function UsageTab({ org }: { org: OrgSummary }) {
  const fetcher = useApiFetcher();
  const [group, setGroup] = useState<UsageGroup>("kind");
  const q = useQuery({ queryKey: usageKey(org.id, group), retry: false, queryFn: () => getUsage(fetcher, org.id, group) });
  if (q.isPending) return <Skeleton className="h-40 w-full" role="status" aria-label="Loading usage" />;
  if (q.isError) return <ErrorState testId="usage-error" title="We couldn't load usage" description={actionError(q.error)} onRetry={() => void q.refetch()} />;
  const { totals, rows, scope } = q.data;
  if (totals.events === 0) {
    return <EmptyState testId="usage-tab" icon={<GaugeIcon className="size-5" />} title="No model usage yet" description="Chats, searches, research and indexing record their token counts here as they happen." />;
  }
  const fmt = (n: number) => n.toLocaleString("en-US");
  return (
    <SurfaceCard className="grid gap-4 p-5" data-testid="usage-tab">
      <div>
        <h2 className="text-lg font-semibold">Usage, last 30 days</h2>
        <p className="text-meta text-text-secondary">{scope === "org" ? "All members of this organization." : "Your own usage."} Token counts are estimates (about four characters per token).</p>
      </div>
      <div className="mb-3 flex items-center gap-2">
        <Label htmlFor="usage-group">Group by</Label>
        <select id="usage-group" className="rounded-md border border-border bg-surface px-2 py-1" value={group} onChange={(e) => setGroup(e.target.value as UsageGroup)}>
          {USAGE_GROUPS.map((g) => <option key={g} value={g}>{g}</option>)}
        </select>
      </div>
      <table className="w-full text-left text-meta" data-testid="usage-table">
        <thead>
          <tr><th scope="col">{group}</th><th scope="col">Calls</th><th scope="col">Tokens in</th><th scope="col">Tokens out</th><th scope="col">Est. cost</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}><td>{r.key}</td><td>{fmt(r.events)}</td><td>{fmt(r.tokens_in)}</td><td>{fmt(r.tokens_out)}</td><td>{(r.est_cost_micros / 1_000_000).toFixed(4)}</td></tr>
          ))}
        </tbody>
        <tfoot>
          <tr><th scope="row">Total</th><td>{fmt(totals.events)}</td><td>{fmt(totals.tokens_in)}</td><td>{fmt(totals.tokens_out)}</td><td>{(totals.est_cost_micros / 1_000_000).toFixed(4)}</td></tr>
        </tfoot>
      </table>
    </SurfaceCard>
  );
}
