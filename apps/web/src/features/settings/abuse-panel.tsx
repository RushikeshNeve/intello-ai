"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { SurfaceCard } from "@/components/ui/surface";
import { useToast } from "@/components/ui/toast";
import { useApiFetcher } from "@/lib/query/provider";
import { invalid, isRecord, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";
import { actionError, formatWhen } from "./permissions";

export const ABUSE_ACTIONS = ["off", "flag", "throttle", "block"] as const;
export type AbuseAction = (typeof ABUSE_ACTIONS)[number];
const ACTION_HELP: Record<AbuseAction, string> = {
  off: "Off: no counting.",
  flag: "Flag: record unusual activity for review (default).",
  throttle: "Throttle: slow the user down while the rate stays high.",
  block: "Block: pause the user for a short time.",
};

export type SecurityEvent = { id: string; kind: string; severity: string; user_id: string | null; created_at: string; acknowledged: boolean };
export type AbuseSettings = Record<string, unknown> & { action: AbuseAction };

export function parseEvents(v: unknown): SecurityEvent[] {
  if (!isRecord(v) || !Array.isArray(v.events)) invalid();
  return v.events.map((e): SecurityEvent => {
    if (!isRecord(e)) invalid();
    return { id: str(e, "id"), kind: str(e, "kind"), severity: str(e, "severity"), user_id: typeof e.user_id === "string" ? e.user_id : null, created_at: str(e, "created_at"), acknowledged: typeof e.acknowledged_at === "string" };
  });
}

export function parseAbuseSettings(v: unknown): AbuseSettings {
  if (!isRecord(v) || !(ABUSE_ACTIONS as readonly unknown[]).includes(v.action)) invalid();
  return v as AbuseSettings;
}

const base = (o: string) => `/v1/orgs/${encodeURIComponent(o)}/security` as ApiPath;
const eventsKey = (o: string) => ["org", o, "security-events"] as const;
const settingsKey = (o: string) => ["org", o, "abuse-settings"] as const;
const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" } });
const getEvents = (f: Fetcher, o: string) => requestJson(f, `${base(o)}/events?limit=20` as ApiPath, parseEvents);
const getSettings = (f: Fetcher, o: string) => requestJson(f, `${base(o)}/abuse-settings` as ApiPath, parseAbuseSettings);
const putSettings = (f: Fetcher, o: string, s: AbuseSettings) => requestJson(f, `${base(o)}/abuse-settings` as ApiPath, parseAbuseSettings, { ...post(s), method: "PUT" });
const acknowledge = (f: Fetcher, o: string, id: string) => requestJson(f, `${base(o)}/events/${encodeURIComponent(id)}/acknowledge` as ApiPath, (b) => b, post({}));

/** Org admins only (the caller mounts it for owners/admins; the server enforces it again). */
export function AbusePanel({ orgId }: { orgId: string }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const toast = useToast();
  const events = useQuery({ queryKey: eventsKey(orgId), retry: false, queryFn: () => getEvents(fetcher, orgId) });
  const settings = useQuery({ queryKey: settingsKey(orgId), retry: false, queryFn: () => getSettings(fetcher, orgId) });
  const save = useMutation({
    mutationFn: (action: AbuseAction) => putSettings(fetcher, orgId, { ...(settings.data as AbuseSettings), action }),
    onSuccess: (s) => {
      qc.setQueryData(settingsKey(orgId), s);
      toast.push({ title: "Abuse protection saved", tone: "success" });
    },
    onError: (e) => toast.push({ title: actionError(e), tone: "danger" }),
  });
  const ack = useMutation({
    mutationFn: (id: string) => acknowledge(fetcher, orgId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: eventsKey(orgId) }),
    onError: (e) => toast.push({ title: actionError(e), tone: "danger" }),
  });
  return (
    <SurfaceCard className="grid gap-3 p-5" data-testid="abuse-panel">
      <h2 className="text-card-title font-semibold">Abuse protection</h2>
      <p className="text-sm text-text-secondary">Unusual search or reading volume is flagged here. Events list people and counts, never what was asked.</p>
      {settings.isPending ? (
        <Skeleton className="h-10 w-full" />
      ) : settings.isError ? (
        <p role="alert" className="text-sm text-danger">{actionError(settings.error)}</p>
      ) : (
        <div className="grid gap-1.5">
          <Label htmlFor="abuse-action">When a limit is crossed</Label>
          <Select id="abuse-action" value={settings.data.action} onChange={(e) => save.mutate(e.target.value as AbuseAction)} disabled={save.isPending}>
            {ABUSE_ACTIONS.map((a) => <option key={a} value={a}>{ACTION_HELP[a]}</option>)}
          </Select>
        </div>
      )}
      {events.isPending ? (
        <Skeleton className="h-16 w-full" />
      ) : events.isError ? (
        <p role="alert" className="text-sm text-danger">{actionError(events.error)}</p>
      ) : events.data.length === 0 ? (
        <p className="text-sm text-text-secondary" data-testid="abuse-empty">No security events.</p>
      ) : (
        <ul className="grid gap-2" data-testid="abuse-events">
          {events.data.map((e) => (
            <li key={e.id} className="flex items-center justify-between gap-3 text-sm">
              <span>
                <strong>{e.kind === "canary_hit" ? "Canary document opened" : "Unusual activity"}</strong> ({e.severity}) {formatWhen(e.created_at)}
              </span>
              {e.acknowledged ? (
                <span className="text-meta text-text-secondary">Acknowledged</span>
              ) : (
                <Button variant="secondary" size="sm" onClick={() => ack.mutate(e.id)} disabled={ack.isPending} data-testid={`ack-${e.id}`}>
                  Acknowledge
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </SurfaceCard>
  );
}
