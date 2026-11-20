"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Modal } from "@/components/ui/overlay";
import { useApiFetcher } from "@/lib/query/provider";
import type { Workspace } from "@/features/workspaces/api";
import { ApiError } from "@/features/organizations/api/http";
import { DEFAULT_LIMITS, startResearch, type Limits } from "./api";

const LIMIT_LABELS: Record<keyof Limits, string> = {
  max_subqueries: "Sub-questions",
  max_evidence: "Evidence passages",
  max_model_calls: "Model calls",
  timeout_seconds: "Time limit (seconds)",
};
export const MAX_QUESTION = 2000;

/** Only values lowered below the defaults are sent (the server never accepts higher ones). */
export function loweredLimits(values: Limits): Partial<Limits> {
  const out: Partial<Limits> = {};
  for (const k of Object.keys(DEFAULT_LIMITS) as (keyof Limits)[]) {
    const v = Math.floor(values[k]);
    if (Number.isFinite(v) && v >= 1 && v < DEFAULT_LIMITS[k]) out[k] = v;
  }
  return out;
}

const field = "h-9 rounded-control border border-input bg-transparent px-2 text-sm";

/** "New research brief": the question, scope and (lower-only) limits in the shared Modal. Opens the session on success. */
export function NewBriefDialog({ open, onClose, orgId, orgSlug, workspaces }: { open: boolean; onClose: () => void; orgId: string; orgSlug: string; workspaces: readonly Workspace[] }) {
  const fetcher = useApiFetcher();
  const router = useRouter();
  const [question, setQuestion] = useState("");
  const [scope, setScope] = useState("");
  const [limits, setLimits] = useState<Limits>({ ...DEFAULT_LIMITS });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !question.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const { id } = await startResearch(fetcher, orgId, { question: question.trim(), workspace_ids: scope ? [scope] : undefined, limits: loweredLimits(limits) });
      router.push(`/o/${encodeURIComponent(orgSlug)}/research/${encodeURIComponent(id)}`);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 403 ? "You can't research in that scope." : "We couldn't start the research. Try again.");
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="New research brief"
      description="Ask a question. Intello searches your knowledge, then writes a sourced report."
      data-testid="research-dialog"
      className="max-w-xl"
      footer={
        <>
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form="research-form" disabled={busy || !question.trim()} data-testid="research-start">
            {busy ? "Starting…" : "Start research"}
          </Button>
        </>
      }
    >
      <form id="research-form" onSubmit={(e) => void submit(e)} className="grid gap-3" data-testid="research-form">
        <label className="grid gap-1 text-sm font-medium">
          Question
          <textarea className="min-h-24 w-full rounded-control border border-input bg-transparent p-2 text-sm font-normal" value={question} maxLength={MAX_QUESTION} onChange={(e) => setQuestion(e.target.value)} data-testid="research-question" />
        </label>
        <label className="grid gap-1 text-sm font-medium">
          Scope
          <select className={`${field} font-normal`} value={scope} onChange={(e) => setScope(e.target.value)} data-testid="research-scope">
            <option value="">All workspaces I can read</option>
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
        <fieldset className="grid gap-2 text-sm">
          <legend className="mb-1 text-meta text-text-secondary">Limits (you can only lower the defaults)</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {(Object.keys(DEFAULT_LIMITS) as (keyof Limits)[]).map((k) => (
              <label key={k} className="grid gap-1">
                {LIMIT_LABELS[k]} (max {DEFAULT_LIMITS[k]})
                <input type="number" min={1} max={DEFAULT_LIMITS[k]} className={field} value={limits[k]} onChange={(e) => setLimits({ ...limits, [k]: Number(e.target.value) })} data-testid={`limit-${k}`} />
              </label>
            ))}
          </div>
        </fieldset>
        {error && (
          <p role="alert" className="text-sm text-danger" data-testid="research-start-error">
            {error}
          </p>
        )}
      </form>
    </Modal>
  );
}
