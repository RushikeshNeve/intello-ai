"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/ui/page-header";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { MetricCard, SurfaceCard } from "@/components/ui/surface";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useApiFetcher } from "@/lib/query/provider";
import type { OrgSummary } from "@/features/organizations/api/orgs";
import type { Workspace } from "@/features/workspaces/api";
import { actionError, formatWhen } from "@/features/settings/permissions";
import { addCase, createSet, deleteCase, deleteSet, evalKeys, getRun, listCases, listRuns, listSets, parseIds, pct, startRun, type EvalRun } from "./api";

export function EvalsApp({ org, workspace }: { org: OrgSummary; workspace: Workspace }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<EvalRun | null>(null);
  const sets = useQuery({ queryKey: evalKeys.sets(org.id, workspace.id), retry: false, queryFn: () => listSets(fetcher, org.id, workspace.id) });
  const current = sets.data?.find((s) => s.id === selected) ?? null;
  const fail = (e: unknown) => setError(actionError(e));

  const create = useMutation({
    mutationFn: () => createSet(fetcher, org.id, workspace.id, newName.trim()),
    onSuccess: (s) => {
      setNewName("");
      setError(null);
      setSelected(s.id);
      void qc.invalidateQueries({ queryKey: evalKeys.sets(org.id, workspace.id) });
    },
    onError: fail,
  });
  const remove = useMutation({
    mutationFn: (id: string) => deleteSet(fetcher, org.id, workspace.id, id),
    onSuccess: () => {
      setSelected(null);
      setShown(null);
      void qc.invalidateQueries({ queryKey: evalKeys.sets(org.id, workspace.id) });
    },
    onError: fail,
  });

  return (
    <div className="mx-auto grid max-w-5xl gap-6" data-workspace-scope={workspace.id} data-testid="evals-app">
      <PageHeader eyebrow={`${org.name} · ${workspace.name}`} title="Golden sets" description="Questions with the sources that should answer them. Run them to see whether retrieval still finds the right documents." />
      {sets.isPending ? (
        <Skeleton className="h-40 w-full" role="status" aria-label="Loading golden sets" />
      ) : sets.isError ? (
        <ErrorState testId="evals-error" title="We couldn't load golden sets" description={actionError(sets.error)} onRetry={() => void sets.refetch()} />
      ) : (
        <>
          <SurfaceCard className="grid gap-3 p-4">
            <form
              className="flex flex-wrap items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (newName.trim()) create.mutate();
              }}
            >
              <div className="grid gap-1">
                <Label htmlFor="new-set">New set name</Label>
                <Input id="new-set" value={newName} maxLength={200} onChange={(e) => setNewName(e.target.value)} />
              </div>
              <Button type="submit" disabled={!newName.trim() || create.isPending}>
                Create set
              </Button>
            </form>
            {sets.data.length === 0 ? (
              <EmptyState title="No golden sets yet" description="Create one, then add questions with the documents that answer them." />
            ) : (
              <ul className="flex flex-wrap gap-2" aria-label="Golden sets">
                {sets.data.map((s) => (
                  <li key={s.id}>
                    <Button size="sm" variant={selected === s.id ? "secondary" : "ghost"} aria-pressed={selected === s.id} onClick={() => { setSelected(s.id); setShown(null); }}>
                      {s.name} ({s.case_count})
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </SurfaceCard>
          {error ? <p role="alert" className="text-meta text-destructive">{error}</p> : null}
          {current ? <SetPanel key={current.id} org={org} workspace={workspace} setId={current.id} name={current.name} version={current.version} shown={shown} setShown={setShown} onError={fail} onDelete={() => remove.mutate(current.id)} /> : null}
        </>
      )}
    </div>
  );
}

function SetPanel({ org, workspace, setId, name, version, shown, setShown, onError, onDelete }: { org: OrgSummary; workspace: Workspace; setId: string; name: string; version: number; shown: EvalRun | null; setShown: (r: EvalRun | null) => void; onError: (e: unknown) => void; onDelete: () => void }) {
  const fetcher = useApiFetcher();
  const qc = useQueryClient();
  const [question, setQuestion] = useState("");
  const [docs, setDocs] = useState("");
  const [abstain, setAbstain] = useState(false);
  const [topK, setTopK] = useState(5);
  const cases = useQuery({ queryKey: evalKeys.cases(org.id, workspace.id, setId), retry: false, queryFn: () => listCases(fetcher, org.id, workspace.id, setId) });
  const runs = useQuery({ queryKey: evalKeys.runs(org.id, workspace.id, setId), retry: false, queryFn: () => listRuns(fetcher, org.id, workspace.id, setId) });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: evalKeys.cases(org.id, workspace.id, setId) });
    void qc.invalidateQueries({ queryKey: evalKeys.sets(org.id, workspace.id) });
  };
  const add = useMutation({
    mutationFn: () => addCase(fetcher, org.id, workspace.id, setId, { question: question.trim(), expected_document_ids: parseIds(docs), should_abstain: abstain }),
    onSuccess: () => {
      setQuestion("");
      setDocs("");
      setAbstain(false);
      refresh();
    },
    onError,
  });
  const drop = useMutation({ mutationFn: (id: string) => deleteCase(fetcher, org.id, workspace.id, setId, id), onSuccess: refresh, onError });
  const run = useMutation({
    mutationFn: () => startRun(fetcher, org.id, workspace.id, setId, topK),
    onSuccess: (r) => {
      setShown(r);
      void qc.invalidateQueries({ queryKey: evalKeys.runs(org.id, workspace.id, setId) });
    },
    onError,
  });
  const view = useMutation({ mutationFn: (id: string) => getRun(fetcher, org.id, workspace.id, setId, id), onSuccess: setShown, onError });
  const list = cases.data ?? [];

  return (
    <SurfaceCard className="grid gap-4 p-4" data-testid="evals-set">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">{name} <span className="text-meta text-text-secondary">v{version}</span></h2>
        <Button size="sm" variant="ghost" onClick={onDelete}>Delete set</Button>
      </div>
      <form
        className="grid gap-2 sm:grid-cols-[2fr_2fr_auto_auto] sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim()) add.mutate();
        }}
      >
        <div className="grid gap-1">
          <Label htmlFor="case-question">Question</Label>
          <Input id="case-question" value={question} maxLength={1000} onChange={(e) => setQuestion(e.target.value)} />
        </div>
        <div className="grid gap-1">
          <Label htmlFor="case-docs">Expected document ids</Label>
          <Input id="case-docs" value={docs} placeholder="comma separated" onChange={(e) => setDocs(e.target.value)} />
        </div>
        <label className="flex items-center gap-2 text-meta">
          <input type="checkbox" checked={abstain} onChange={(e) => setAbstain(e.target.checked)} /> Should abstain
        </label>
        <Button type="submit" disabled={!question.trim() || add.isPending}>Add case</Button>
      </form>
      {cases.isError ? <ErrorState title="We couldn't load cases" description={actionError(cases.error)} onRetry={() => void cases.refetch()} /> : null}
      {list.length > 0 ? (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Question</TableHead>
              <TableHead>Expected</TableHead>
              <TableHead>Abstain</TableHead>
              <TableHead><span className="sr-only">Actions</span></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.map((c) => (
              <TableRow key={c.id}>
                <TableCell>{c.question}</TableCell>
                <TableCell>{c.expected_document_ids.length + c.expected_chunk_ids.length}</TableCell>
                <TableCell>{c.should_abstain ? "Yes" : "No"}</TableCell>
                <TableCell><Button size="sm" variant="ghost" aria-label={`Delete case ${c.question}`} onClick={() => drop.mutate(c.id)}>Delete</Button></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : null}
      <div className="flex flex-wrap items-end gap-2">
        <div className="grid gap-1">
          <Label htmlFor="run-topk">Top k</Label>
          <Input id="run-topk" type="number" min={1} max={20} value={topK} onChange={(e) => setTopK(Math.min(20, Math.max(1, Number(e.target.value) || 5)))} className="w-24" />
        </div>
        <Button disabled={list.length === 0 || run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Running…" : "Run set"}</Button>
        {(runs.data ?? []).slice(0, 5).map((r) => (
          <Button key={r.id} size="sm" variant="ghost" onClick={() => view.mutate(r.id)}>
            Run {formatWhen(r.started_at)}
          </Button>
        ))}
      </div>
      {shown ? <RunView run={shown} /> : null}
    </SurfaceCard>
  );
}

function RunView({ run }: { run: EvalRun }) {
  const s = run.summary;
  return (
    <div className="grid gap-3" data-testid="evals-run">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricCard label={`Hit@${s.top_k}`} value={pct(s.hit_at_k)} />
        <MetricCard label="MRR" value={s.mrr === null ? "n/a" : s.mrr.toFixed(2)} />
        <MetricCard label="Abstention" value={pct(s.abstention_accuracy)} />
        <MetricCard label="Passed" value={pct(s.pass_rate)} />
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Question</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Rank</TableHead>
            <TableHead>Result</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {run.results.map((r) => (
            <TableRow key={r.id}>
              <TableCell>{r.question}</TableCell>
              <TableCell>{r.answer_status}</TableCell>
              <TableCell>{r.metrics.rank ?? "-"}</TableCell>
              <TableCell>{r.metrics.passed ? "Pass" : "Fail"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
