"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useApiFetcher } from "@/lib/query/provider";
import type { Workspace } from "@/features/workspaces/api";
import { ApiError } from "@/features/organizations/api/http";
import { DEFAULT_LIMITS, listResearch, startResearch, type Limits } from "./api";

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

export function ResearchHome({ orgId, orgSlug, workspaces }: { orgId: string; orgSlug: string; workspaces: readonly Workspace[] }) {
  const fetcher = useApiFetcher();
  const router = useRouter();
  const [question, setQuestion] = useState("");
  const [scope, setScope] = useState("");
  const [limits, setLimits] = useState<Limits>({ ...DEFAULT_LIMITS });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const list = useInfiniteQuery({
    queryKey: ["org", orgId, "research", "list"],
    initialPageParam: null as string | null,
    retry: false,
    queryFn: ({ pageParam, signal }) => listResearch(fetcher, orgId, { after: pageParam }, signal),
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];

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
    <div className="mx-auto grid max-w-4xl gap-6" data-testid="research-home">
      <h1 tabIndex={-1} className="text-xl font-semibold tracking-tight outline-none">
        Research
      </h1>
      <form onSubmit={(e) => void submit(e)} className="grid gap-3 rounded-lg border border-border p-4" data-testid="research-form">
        <label className="grid gap-1 text-sm">
          Question
          <textarea
            className="min-h-24 w-full rounded-md border border-input bg-transparent p-2 text-sm"
            value={question}
            maxLength={MAX_QUESTION}
            onChange={(e) => setQuestion(e.target.value)}
            data-testid="research-question"
          />
        </label>
        <label className="grid gap-1 text-sm">
          Scope
          <select className="h-8 rounded-md border border-input bg-transparent px-2 text-sm" value={scope} onChange={(e) => setScope(e.target.value)} data-testid="research-scope">
            <option value="">All workspaces I can read</option>
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
        <fieldset className="grid gap-2 text-sm">
          <legend className="mb-1 text-xs text-muted-foreground">Limits (you can only lower the defaults)</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {(Object.keys(DEFAULT_LIMITS) as (keyof Limits)[]).map((k) => (
              <label key={k} className="grid gap-1">
                {LIMIT_LABELS[k]} (max {DEFAULT_LIMITS[k]})
                <input
                  type="number"
                  min={1}
                  max={DEFAULT_LIMITS[k]}
                  className="h-8 rounded-md border border-input bg-transparent px-2 text-sm"
                  value={limits[k]}
                  onChange={(e) => setLimits({ ...limits, [k]: Number(e.target.value) })}
                  data-testid={`limit-${k}`}
                />
              </label>
            ))}
          </div>
        </fieldset>
        <div className="flex items-center gap-3">
          <Button type="submit" disabled={busy || !question.trim()} data-testid="research-start">
            {busy ? "Starting…" : "Start research"}
          </Button>
          {error && (
            <p role="alert" className="text-sm text-destructive" data-testid="research-start-error">
              {error}
            </p>
          )}
        </div>
      </form>

      <section aria-label="Research sessions" className="grid gap-2">
        <h2 className="text-sm font-medium">Your research</h2>
        {list.isPending ? (
          <p role="status" className="text-sm text-muted-foreground">
            Loading…
          </p>
        ) : list.isError ? (
          <p role="alert" className="text-sm">
            We couldn&apos;t load your research sessions.
          </p>
        ) : items.length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid="research-empty">
            No research yet.
          </p>
        ) : (
          <ul className="grid gap-1" data-testid="research-list">
            {items.map((s) => (
              <li key={s.id}>
                <Link href={`/o/${encodeURIComponent(orgSlug)}/research/${encodeURIComponent(s.id)}`} className="flex flex-wrap items-baseline gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted" data-testid="research-link">
                  <span className="min-w-0 break-words">{s.question}</span>
                  <span className="text-xs text-muted-foreground">{s.state}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
        {list.hasNextPage && (
          <Button variant="outline" size="sm" className="w-fit" onClick={() => void list.fetchNextPage()}>
            Load more
          </Button>
        )}
      </section>
    </div>
  );
}
