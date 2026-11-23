"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { SurfaceCard } from "@/components/ui/surface";
import { useToast } from "@/components/ui/toast";
import { useApiFetcher } from "@/lib/query/provider";
import { invalid, isRecord, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";
import { actionError } from "./permissions";

export const INPUT_MODES = ["off", "flag", "block"] as const;
export const OUTPUT_MODES = ["off", "flag", "block", "redact"] as const;

export type FirewallPolicy = { input_user: string; input_evidence: string; output: string; threshold: number; is_default: boolean };
export type FirewallDraft = Omit<FirewallPolicy, "is_default">;

export function parseFirewall(v: unknown): FirewallPolicy {
  if (!isRecord(v) || typeof v.threshold !== "number" || typeof v.is_default !== "boolean") invalid();
  return { input_user: str(v, "input_user"), input_evidence: str(v, "input_evidence"), output: str(v, "output"), threshold: v.threshold, is_default: v.is_default };
}

const path = (o: string) => `/v1/orgs/${encodeURIComponent(o)}/firewall-policy` as ApiPath;
export const firewallKey = (o: string) => ["org", o, "firewall-policy"] as const;
export const getFirewall = (f: Fetcher, o: string) => requestJson(f, path(o), parseFirewall);
export const putFirewall = (f: Fetcher, o: string, d: FirewallDraft) =>
  requestJson(f, path(o), parseFirewall, { method: "PUT", body: JSON.stringify(d), headers: { "content-type": "application/json" } });

const HINT: Record<string, string> = {
  off: "Not checked.",
  flag: "Checked and counted; nothing is changed.",
  block: "Checked; a hostile message is refused, a hostile passage is left out, a leaking answer is withheld.",
  redact: "Secrets in answers are replaced before they are shown.",
};

/** Org owners/admins choose how the AI firewall treats questions, retrieved passages and answers. Default: all off. */
export function FirewallPanel({ orgId, canEdit }: { orgId: string; canEdit: boolean }) {
  const fetcher = useApiFetcher();
  const q = useQuery({ queryKey: firewallKey(orgId), retry: false, queryFn: () => getFirewall(fetcher, orgId) });
  return (
    <SurfaceCard className="grid gap-3 p-5" data-testid="firewall-panel">
      <h2 className="text-card-title font-semibold">AI firewall</h2>
      <p className="text-sm text-text-secondary">Screens questions and retrieved passages for injected instructions, and answers for leaked secrets or system text. Only counts and rule names are recorded, never the text.</p>
      {q.isPending ? <Skeleton className="h-24 w-full" /> : q.isError ? <p role="alert" className="text-sm text-danger">{actionError(q.error)}</p> : <FirewallForm key={q.dataUpdatedAt} orgId={orgId} initial={q.data} canEdit={canEdit} />}
    </SurfaceCard>
  );
}

function FirewallForm({ orgId, initial, canEdit }: { orgId: string; initial: FirewallPolicy; canEdit: boolean }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState<FirewallDraft>({ input_user: initial.input_user, input_evidence: initial.input_evidence, output: initial.output, threshold: initial.threshold });
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<FirewallDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const save = useMutation({
    mutationFn: () => putFirewall(fetcher, orgId, draft),
    onSuccess: (p) => {
      setError(null);
      qc.setQueryData(firewallKey(orgId), p);
      toast.push({ title: "AI firewall saved", tone: "success" });
    },
    onError: (e) => setError(actionError(e)),
  });
  return (
    <form className="grid gap-3" onSubmit={(e) => { e.preventDefault(); if (canEdit) save.mutate(); }}>
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="grid gap-1.5">
          <Label htmlFor="fw-user">Questions</Label>
          <Select id="fw-user" value={draft.input_user} onChange={(e) => set({ input_user: e.target.value })} disabled={!canEdit}>
            {INPUT_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </Select>
          <p className="text-meta text-text-secondary">{HINT[draft.input_user]}</p>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="fw-evidence">Retrieved passages</Label>
          <Select id="fw-evidence" value={draft.input_evidence} onChange={(e) => set({ input_evidence: e.target.value })} disabled={!canEdit}>
            {INPUT_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </Select>
          <p className="text-meta text-text-secondary">{HINT[draft.input_evidence]}</p>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="fw-output">Answers</Label>
          <Select id="fw-output" value={draft.output} onChange={(e) => set({ output: e.target.value })} disabled={!canEdit}>
            {OUTPUT_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </Select>
          <p className="text-meta text-text-secondary">{HINT[draft.output]}</p>
        </div>
      </div>
      <div className="grid gap-1.5 sm:max-w-xs">
        <Label htmlFor="fw-threshold">Sensitivity threshold ({draft.threshold.toFixed(2)})</Label>
        <input id="fw-threshold" type="range" min={0.05} max={1} step={0.05} value={draft.threshold} onChange={(e) => set({ threshold: Number(e.target.value) })} disabled={!canEdit} />
        <p className="text-meta text-text-secondary">Lower catches more but may refuse harmless text.</p>
      </div>
      {canEdit ? (
        <Button type="submit" className="w-fit" disabled={save.isPending} data-testid="firewall-save">Save firewall</Button>
      ) : (
        <p className="text-meta text-text-secondary" data-testid="firewall-readonly">Only organization admins can change the firewall.</p>
      )}
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    </form>
  );
}
