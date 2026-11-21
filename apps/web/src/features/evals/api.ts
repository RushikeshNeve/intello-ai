/**
 * Golden sets client (M12.03). Workspace-admin only; routes under
 * `/v1/orgs/{o}/workspaces/{w}/evals`. A run is synchronous (<= 50 cases) and retrieval-only.
 */
import { invalid, isRecord, jsonBody, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";

export type EvalSet = { id: string; name: string; version: number; case_count: number; updated_at: string };
export type EvalCase = {
  id: string;
  question: string;
  expected_document_ids: string[];
  expected_chunk_ids: string[];
  should_abstain: boolean;
  notes: string | null;
};
export type CaseDraft = { question: string; expected_document_ids: string[]; should_abstain: boolean; notes?: string | null };
export type Retrieved = { document_id: string; chunk_id: string; title: string; score: number };
export type CaseMetrics = { hit: boolean | null; rank: number | null; should_abstain: boolean; abstain_correct: boolean; passed: boolean };
export type RunResult = { id: string; question: string; retrieved: Retrieved[]; answer_status: "answered" | "abstained" | "error"; metrics: CaseMetrics };
export type RunSummary = {
  total: number;
  errors: number;
  top_k: number;
  hit_at_k: number | null;
  mrr: number | null;
  abstention_accuracy: number | null;
  pass_rate: number | null;
};
export type EvalRun = { id: string; set_version: number; started_at: string; summary: RunSummary; results: RunResult[] };

const num = (v: unknown): number | null => (typeof v === "number" ? v : null);
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export function parseSet(v: unknown): EvalSet {
  if (!isRecord(v)) invalid();
  return { id: str(v, "id"), name: str(v, "name"), version: num(v.version) ?? 1, case_count: num(v.case_count) ?? 0, updated_at: typeof v.updated_at === "string" ? v.updated_at : "" };
}

export function parseCase(v: unknown): EvalCase {
  if (!isRecord(v)) invalid();
  return {
    id: str(v, "id"),
    question: str(v, "question"),
    expected_document_ids: strs(v.expected_document_ids),
    expected_chunk_ids: strs(v.expected_chunk_ids),
    should_abstain: v.should_abstain === true,
    notes: typeof v.notes === "string" ? v.notes : null,
  };
}

function parseMetrics(v: unknown): CaseMetrics {
  const m = isRecord(v) ? v : {};
  return {
    hit: typeof m.hit === "boolean" ? m.hit : null,
    rank: num(m.rank),
    should_abstain: m.should_abstain === true,
    abstain_correct: m.abstain_correct === true,
    passed: m.passed === true,
  };
}

function parseResult(v: unknown): RunResult {
  if (!isRecord(v)) invalid();
  const status = v.answer_status;
  if (status !== "answered" && status !== "abstained" && status !== "error") invalid();
  const retrieved = Array.isArray(v.retrieved)
    ? v.retrieved.filter(isRecord).map((r) => ({ document_id: str(r, "document_id"), chunk_id: str(r, "chunk_id"), title: typeof r.title === "string" ? r.title : "Untitled", score: num(r.score) ?? 0 }))
    : [];
  return { id: str(v, "id"), question: str(v, "question"), retrieved, answer_status: status, metrics: parseMetrics(v.metrics) };
}

export function parseRun(v: unknown): EvalRun {
  if (!isRecord(v) || !isRecord(v.summary)) invalid();
  const s = v.summary;
  return {
    id: str(v, "id"),
    set_version: num(v.set_version) ?? 1,
    started_at: typeof v.started_at === "string" ? v.started_at : "",
    summary: {
      total: num(s.total) ?? 0,
      errors: num(s.errors) ?? 0,
      top_k: num(s.top_k) ?? 0,
      hit_at_k: num(s.hit_at_k),
      mrr: num(s.mrr),
      abstention_accuracy: num(s.abstention_accuracy),
      pass_rate: num(s.pass_rate),
    },
    results: Array.isArray(v.results) ? v.results.map(parseResult) : [],
  };
}

const items = <T,>(body: unknown, one: (v: unknown) => T): T[] => {
  if (!isRecord(body) || !Array.isArray(body.items)) invalid();
  return body.items.map(one);
};

export const evalKeys = {
  sets: (o: string, w: string) => ["org", o, "workspace", w, "evals", "sets"] as const,
  cases: (o: string, w: string, s: string) => ["org", o, "workspace", w, "evals", "cases", s] as const,
  runs: (o: string, w: string, s: string) => ["org", o, "workspace", w, "evals", "runs", s] as const,
};

const base = (o: string, w: string) => `/v1/orgs/${encodeURIComponent(o)}/workspaces/${encodeURIComponent(w)}/evals`;
const patch = (value: unknown): RequestInit => ({ ...jsonBody(value), method: "PATCH" });

export const listSets = (f: Fetcher, o: string, w: string) => requestJson(f, `${base(o, w)}/sets` as ApiPath, (b) => items(b, parseSet));
export const createSet = (f: Fetcher, o: string, w: string, name: string) => requestJson(f, `${base(o, w)}/sets` as ApiPath, parseSet, jsonBody({ name }));
export const renameSet = (f: Fetcher, o: string, w: string, id: string, name: string) => requestJson(f, `${base(o, w)}/sets/${id}` as ApiPath, parseSet, patch({ name }));
export const deleteSet = async (f: Fetcher, o: string, w: string, id: string) => {
  await f(`${base(o, w)}/sets/${id}` as ApiPath, { method: "DELETE" });
};
export const listCases = (f: Fetcher, o: string, w: string, s: string) => requestJson(f, `${base(o, w)}/sets/${s}/cases` as ApiPath, (b) => items(b, parseCase));
export const addCase = (f: Fetcher, o: string, w: string, s: string, draft: CaseDraft) => requestJson(f, `${base(o, w)}/sets/${s}/cases` as ApiPath, parseCase, jsonBody(draft));
export const deleteCase = async (f: Fetcher, o: string, w: string, s: string, id: string) => {
  await f(`${base(o, w)}/sets/${s}/cases/${id}` as ApiPath, { method: "DELETE" });
};
export const startRun = (f: Fetcher, o: string, w: string, s: string, topK: number) => requestJson(f, `${base(o, w)}/sets/${s}/runs` as ApiPath, parseRun, jsonBody({ top_k: topK }));
export const listRuns = (f: Fetcher, o: string, w: string, s: string) => requestJson(f, `${base(o, w)}/sets/${s}/runs` as ApiPath, (b) => items(b, parseRun));
export const getRun = (f: Fetcher, o: string, w: string, s: string, run: string) => requestJson(f, `${base(o, w)}/sets/${s}/runs/${run}` as ApiPath, parseRun);

export const pct = (v: number | null): string => (v === null ? "n/a" : `${Math.round(v * 100)}%`);

/** Comma/space separated document ids -> the ids that look like UUIDs (the server validates the rest). */
export const parseIds = (raw: string): string[] => raw.split(/[\s,]+/).filter((x) => /^[0-9a-f-]{36}$/i.test(x));
