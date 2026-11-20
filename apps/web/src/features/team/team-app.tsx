"use client";

import { useState } from "react";
import { LockIcon, UserPlusIcon } from "lucide-react";
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
import { ROLE_LABEL, type OrgRole, type OrgSummary } from "@/features/organizations/api/orgs";
import type { Workspace, WorkspaceRole } from "@/features/workspaces/api";
import { actionError, canAdminWorkspace, isOrgAdmin } from "@/features/settings/permissions";
import {
  assignableOrgRoles,
  getMyId,
  listMembers,
  removeOrgMember,
  removeWorkspaceMember,
  setOrgRole,
  setWorkspaceRole,
  teamKeys,
  WORKSPACE_ROLES,
  WORKSPACE_ROLE_LABEL,
  type OrgMember,
} from "./api";

const SELECT_CLASS =
  "h-9 rounded-control border border-border bg-transparent px-2 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50";

const ROLE_TONE: Record<OrgRole, "violet" | "info" | "neutral"> = { owner: "violet", admin: "info", member: "neutral" };

export function TeamApp({ org, workspace }: { org: OrgSummary; workspace: Workspace }) {
  const fetcher = useApiFetcher();
  const members = useQuery({ queryKey: teamKeys.members(org.id), retry: false, queryFn: () => listMembers(fetcher, org.id) });
  const me = useQuery({ queryKey: teamKeys.me(), retry: false, queryFn: () => getMyId(fetcher) });
  const [inviting, setInviting] = useState(false);

  return (
    <div className="mx-auto grid max-w-5xl gap-6" data-workspace-scope={workspace.id} data-testid="team-app">
      <PageHeader
        eyebrow={`${org.name} · ${workspace.name}`}
        title="People and access"
        description="Who belongs to this organization and what each person can do."
        actions={
          <Button variant="outline" onClick={() => setInviting(true)} data-testid="invite-open">
            <UserPlusIcon aria-hidden="true" /> Invite member
          </Button>
        }
      />
      <InviteDialog open={inviting} onClose={() => setInviting(false)} />
      <section aria-labelledby="org-members-h" className="grid gap-3">
        <h2 id="org-members-h" className="text-section font-semibold tracking-tight">
          Organization members
        </h2>
        {members.isPending ? (
          <div role="status" aria-label="Loading members" className="grid gap-2">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : members.isError ? (
          <ErrorState testId="members-error" title="We couldn't load the members" description={actionError(members.error)} onRetry={() => void members.refetch()} />
        ) : members.data.length === 0 ? (
          <EmptyState testId="members-empty" title="No members" description="This organization has no members to show." />
        ) : (
          <MemberList orgId={org.id} actor={org.role} members={members.data} myId={me.data ?? null} />
        )}
        {!isOrgAdmin(org) && (
          <p className="flex items-center gap-2 text-meta text-text-secondary" data-testid="members-readonly">
            <LockIcon aria-hidden="true" className="size-3.5" /> You can see the member list. Only organization owners and admins can change roles or remove people.
          </p>
        )}
      </section>
      <WorkspaceAccess org={org} workspace={workspace} members={members.data ?? []} />
    </div>
  );
}

function InviteDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title="Invite a member" description="Invitations are not available yet.">
      <div className="grid gap-3" data-testid="invite-dialog">
        <div className="grid gap-1.5">
          <Label htmlFor="invite-email">Email address</Label>
          <Input id="invite-email" type="email" disabled placeholder="name@example.com" />
        </div>
        <p className="text-sm text-text-secondary">
          Invitations are not available yet. For now, a person can join only after they have signed in once; an owner or admin can then set their role here.
        </p>
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
          <Button disabled data-testid="invite-send">
            Send invitation
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function MemberList({ orgId, actor, members, myId }: { orgId: string; actor: OrgRole; members: OrgMember[]; myId: string | null }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const toast = useToast();
  const [error, setError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<OrgMember | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: teamKeys.members(orgId) });

  const changeRole = useMutation({
    mutationFn: (v: { m: OrgMember; role: OrgRole }) => setOrgRole(fetcher, orgId, v.m.user_id, v.role),
    onSuccess: (_r, v) => {
      setError(null);
      toast.push({ title: `${v.m.display_name || v.m.email} is now ${ROLE_LABEL[v.role]}`, tone: "success" });
      void refresh();
    },
    onError: (e) => setError(actionError(e)),
  });
  const remove = useMutation({
    mutationFn: (m: OrgMember) => removeOrgMember(fetcher, orgId, m.user_id),
    onSuccess: () => {
      setError(null);
      setRemoving(null);
      toast.push({ title: "Member removed", tone: "success" });
      void refresh();
    },
    onError: (e) => {
      setRemoving(null);
      setError(actionError(e));
    },
  });

  return (
    <>
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <ul className="grid gap-2" data-testid="member-list">
        {members.map((m) => {
          const self = m.user_id === myId;
          const roles = assignableOrgRoles(actor, m.role);
          const editable = roles.length > 0 && !self;
          const name = m.display_name || m.email;
          return (
            <SurfaceCard as="li" key={m.user_id} className="flex flex-wrap items-center gap-3 p-3" data-testid="member-row">
              <div className="min-w-0 grow">
                <p className="truncate text-card-title font-medium">
                  {name} {self && <span className="text-meta text-text-secondary">(you)</span>}
                </p>
                {m.display_name && <p className="truncate text-meta text-text-secondary">{m.email}</p>}
              </div>
              {editable ? (
                <>
                  <label className="sr-only" htmlFor={`role-${m.user_id}`}>
                    Role of {name}
                  </label>
                  <select
                    id={`role-${m.user_id}`}
                    className={SELECT_CLASS}
                    value={m.role}
                    disabled={changeRole.isPending}
                    onChange={(e) => changeRole.mutate({ m, role: e.target.value as OrgRole })}
                  >
                    {Array.from(new Set<OrgRole>([m.role, ...roles])).map((r) => (
                      <option key={r} value={r}>
                        {ROLE_LABEL[r]}
                      </option>
                    ))}
                  </select>
                  <Button variant="outline" size="sm" onClick={() => setRemoving(m)} aria-label={`Remove ${name}`}>
                    Remove
                  </Button>
                </>
              ) : (
                <StatusBadge tone={ROLE_TONE[m.role]}>{ROLE_LABEL[m.role]}</StatusBadge>
              )}
            </SurfaceCard>
          );
        })}
      </ul>
      <Modal
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title="Remove this member?"
        description="They lose access to the organization and all its workspaces immediately."
        footer={
          <>
            <Button variant="outline" onClick={() => setRemoving(null)}>
              Cancel
            </Button>
            <Button variant="destructive" disabled={remove.isPending} onClick={() => removing && remove.mutate(removing)} data-testid="confirm-remove-member">
              Remove member
            </Button>
          </>
        }
      >
        <p className="text-sm">{removing?.display_name || removing?.email}</p>
      </Modal>
    </>
  );
}

function WorkspaceAccess({ org, workspace, members }: { org: OrgSummary; workspace: Workspace; members: OrgMember[] }) {
  const fetcher = useApiFetcher();
  const toast = useToast();
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState<WorkspaceRole>("viewer");
  const [error, setError] = useState<string | null>(null);
  const [removing, setRemoving] = useState(false);
  const canManage = canAdminWorkspace(org, workspace);
  const target = members.find((m) => m.user_id === userId);

  const set = useMutation({
    mutationFn: () => setWorkspaceRole(fetcher, org.id, workspace.id, userId, role),
    onSuccess: () => {
      setError(null);
      toast.push({ title: `Workspace role set to ${WORKSPACE_ROLE_LABEL[role]}`, tone: "success" });
    },
    onError: (e) => setError(actionError(e)),
  });
  const remove = useMutation({
    mutationFn: () => removeWorkspaceMember(fetcher, org.id, workspace.id, userId),
    onSuccess: () => {
      setError(null);
      setRemoving(false);
      toast.push({ title: "Workspace access removed", tone: "success" });
    },
    onError: (e) => {
      setRemoving(false);
      setError(actionError(e));
    },
  });

  return (
    <section aria-labelledby="ws-access-h" className="grid gap-3" data-testid="workspace-access">
      <h2 id="ws-access-h" className="text-section font-semibold tracking-tight">
        Access to {workspace.name}
      </h2>
      <p className="text-sm text-text-secondary">
        Your role here: {workspace.role ? WORKSPACE_ROLE_LABEL[workspace.role] : isOrgAdmin(org) ? "Implicit admin (organization " + ROLE_LABEL[org.role].toLowerCase() + ")" : "No explicit role"}. Org owners and admins can always manage every workspace.
      </p>
      {!canManage ? (
        <p className="flex items-center gap-2 text-meta text-text-secondary" data-testid="workspace-readonly">
          <LockIcon aria-hidden="true" className="size-3.5" /> Only workspace admins can change who has access.
        </p>
      ) : (
        <SurfaceCard className="grid gap-3 p-4">
          <p className="text-meta text-text-secondary">The server does not list workspace members yet, so choose a person from the organization to set or remove their role.</p>
          <div className="flex flex-wrap items-end gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor="ws-member">Person</Label>
              <select id="ws-member" className={SELECT_CLASS} value={userId} onChange={(e) => setUserId(e.target.value)}>
                <option value="">Choose a member…</option>
                {members.map((m) => (
                  <option key={m.user_id} value={m.user_id}>
                    {m.display_name || m.email}
                  </option>
                ))}
              </select>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="ws-role">Workspace role</Label>
              <select id="ws-role" className={SELECT_CLASS} value={role} onChange={(e) => setRole(e.target.value as WorkspaceRole)}>
                {WORKSPACE_ROLES.map((r) => (
                  <option key={r} value={r}>
                    {WORKSPACE_ROLE_LABEL[r]}
                  </option>
                ))}
              </select>
            </div>
            <Button disabled={!userId || set.isPending} onClick={() => set.mutate()} data-testid="ws-set-role">
              Set role
            </Button>
            <Button variant="outline" disabled={!userId} onClick={() => setRemoving(true)} data-testid="ws-remove">
              Remove access
            </Button>
          </div>
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
        </SurfaceCard>
      )}
      <Modal
        open={removing}
        onClose={() => setRemoving(false)}
        title="Remove workspace access?"
        description="They keep their organization membership but lose their explicit role in this workspace."
        footer={
          <>
            <Button variant="outline" onClick={() => setRemoving(false)}>
              Cancel
            </Button>
            <Button variant="destructive" disabled={remove.isPending} onClick={() => remove.mutate()} data-testid="confirm-ws-remove">
              Remove access
            </Button>
          </>
        }
      >
        <p className="text-sm">{target?.display_name || target?.email}</p>
      </Modal>
    </section>
  );
}
