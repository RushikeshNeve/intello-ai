"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { SurfaceCard } from "@/components/ui/surface";
import { useToast } from "@/components/ui/toast";
import { useApiFetcher } from "@/lib/query/provider";
import { invalid, isRecord, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";
import { actionError } from "./permissions";

export const TONES = ["neutral", "formal", "friendly", "technical"] as const;
export const FORMATS = ["concise", "detailed", "bullets", "table"] as const;
export const CITATIONS = ["numeric", "inline"] as const;
export const CUSTOM_MAX = 1000;

export type AnswerStyle = {
  source: "workspace" | "org_default" | "default";
  tone: string;
  answer_format: string;
  language: string | null;
  citation_style: string;
  custom_instructions: string;
};
export type StyleDraft = Omit<AnswerStyle, "source">;

export function parseStyle(v: unknown): AnswerStyle {
  if (!isRecord(v)) invalid();
  const source = v.source;
  if (source !== "workspace" && source !== "org_default" && source !== "default") invalid();
  return {
    source,
    tone: str(v, "tone"),
    answer_format: str(v, "answer_format"),
    language: typeof v.language === "string" ? v.language : null,
    citation_style: str(v, "citation_style"),
    custom_instructions: typeof v.custom_instructions === "string" ? v.custom_instructions : "",
  };
}

const path = (o: string, w: string) => `/v1/orgs/${encodeURIComponent(o)}/workspaces/${encodeURIComponent(w)}/style` as ApiPath;
export const styleKey = (o: string, w: string) => ["org", o, "workspace", w, "style"] as const;
export const getStyle = (f: Fetcher, o: string, w: string) => requestJson(f, path(o, w), parseStyle);
export const putStyle = (f: Fetcher, o: string, w: string, d: StyleDraft) =>
  requestJson(f, path(o, w), parseStyle, { method: "PUT", body: JSON.stringify({ ...d, language: d.language?.trim() || null }), headers: { "content-type": "application/json" } });

/** Admin-editable answer style. Presentation only: the safety, citation and abstention rules are fixed. */
export function StylePanel({ orgId, workspaceId, canEdit }: { orgId: string; workspaceId: string; canEdit: boolean }) {
  const fetcher = useApiFetcher();
  const q = useQuery({ queryKey: styleKey(orgId, workspaceId), retry: false, queryFn: () => getStyle(fetcher, orgId, workspaceId) });
  return (
    <SurfaceCard className="grid gap-3 p-5" data-testid="style-panel">
      <h2 className="text-card-title font-semibold">Answer style</h2>
      <p className="text-sm text-text-secondary">Changes how answers are worded and laid out. Answers still use only your documents and always cite them.</p>
      {q.isPending ? <Skeleton className="h-24 w-full" /> : q.isError ? <p role="alert" className="text-sm text-danger">{actionError(q.error)}</p> : <StyleForm key={q.dataUpdatedAt} orgId={orgId} workspaceId={workspaceId} initial={q.data} canEdit={canEdit} />}
    </SurfaceCard>
  );
}

function StyleForm({ orgId, workspaceId, initial, canEdit }: { orgId: string; workspaceId: string; initial: AnswerStyle; canEdit: boolean }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState<StyleDraft>({ tone: initial.tone, answer_format: initial.answer_format, language: initial.language, citation_style: initial.citation_style, custom_instructions: initial.custom_instructions });
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<StyleDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const save = useMutation({
    mutationFn: () => putStyle(fetcher, orgId, workspaceId, draft),
    onSuccess: (s) => {
      setError(null);
      qc.setQueryData(styleKey(orgId, workspaceId), s);
      toast.push({ title: "Answer style saved", tone: "success" });
    },
    onError: (e) => setError(actionError(e)),
  });
  const markers = draft.custom_instructions.includes("<<<") || draft.custom_instructions.includes(">>>");
  return (
    <form
      className="grid gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (canEdit && !markers) save.mutate();
      }}
    >
      {initial.source === "org_default" && <p className="text-meta text-text-secondary">Currently inherited from the organisation default. Saving creates a workspace style.</p>}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="grid gap-1.5">
          <Label htmlFor="style-tone">Tone</Label>
          <Select id="style-tone" value={draft.tone} onChange={(e) => set({ tone: e.target.value })} disabled={!canEdit}>
            {TONES.map((t) => <option key={t} value={t}>{t}</option>)}
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="style-format">Answer format</Label>
          <Select id="style-format" value={draft.answer_format} onChange={(e) => set({ answer_format: e.target.value })} disabled={!canEdit}>
            {FORMATS.map((t) => <option key={t} value={t}>{t}</option>)}
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="style-citation">Citation style</Label>
          <Select id="style-citation" value={draft.citation_style} onChange={(e) => set({ citation_style: e.target.value })} disabled={!canEdit}>
            {CITATIONS.map((t) => <option key={t} value={t}>{t}</option>)}
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="style-language">Language (optional)</Label>
          <Input id="style-language" value={draft.language ?? ""} onChange={(e) => set({ language: e.target.value })} disabled={!canEdit} maxLength={40} placeholder="e.g. German" />
        </div>
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor="style-custom">Extra preferences</Label>
        <textarea
          id="style-custom"
          className="min-h-24 rounded-lg border border-input bg-transparent px-2 py-1.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50"
          value={draft.custom_instructions}
          onChange={(e) => set({ custom_instructions: e.target.value })}
          disabled={!canEdit}
          maxLength={CUSTOM_MAX}
          aria-describedby="style-custom-note"
        />
        <p id="style-custom-note" className="text-meta text-text-secondary">
          {draft.custom_instructions.length}/{CUSTOM_MAX}. These cannot override citation or safety rules.
        </p>
        {markers && <p role="alert" className="text-sm text-danger">The characters &lt;&lt;&lt; and &gt;&gt;&gt; are not allowed.</p>}
      </div>
      {canEdit ? (
        <Button type="submit" className="w-fit" disabled={save.isPending || markers} data-testid="style-save">Save style</Button>
      ) : (
        <p className="text-meta text-text-secondary" data-testid="style-readonly">Only workspace admins can change the answer style.</p>
      )}
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    </form>
  );
}
