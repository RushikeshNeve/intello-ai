/**
 * Research API client (contract m07 §4). EVERY wire shape the research UI relies on lives here.
 * Assumptions where the contract is open: list = `{items:[{id,question,state,created_at}], next_cursor}`; report =
 * `{sections:[{heading, statements:[{text, citations:[Citation]}]}], verification}` with chat-shaped Citations;
 * `limits` keys = `max_subqueries|max_evidence|max_model_calls|timeout_seconds`; cancel returns 200/202/204 (body ignored).
 */
import { parseCitations, type Citation } from "@/features/chat/api";
import { ApiError, invalid, isRecord, jsonBody, oneOf, requestJson, str, type ApiPath, type Fetcher } from "@/features/organizations/api/http";

export const RESEARCH_STATES = ["planned", "running", "completed", "failed", "cancelled"] as const;
export type ResearchState = (typeof RESEARCH_STATES)[number];
export const isTerminal = (s: ResearchState): boolean => s === "completed" || s === "failed" || s === "cancelled";
export const POLL_MS = 2000;

/** The server defaults (contract §4/§6): the form may only lower them. */
export const DEFAULT_LIMITS = { max_subqueries: 6, max_evidence: 60, max_model_calls: 12, timeout_seconds: 600 } as const;
export type Limits = { -readonly [K in keyof typeof DEFAULT_LIMITS]: number };

export type Step = { seq: number; kind: string; query: string | null; state: "pending" | "running" | "done" | "failed" };
export type Statement = { text: string; citations: Citation[] };
export type Section = { heading: string; statements: Statement[] };
export type Report = { sections: Section[]; withheld: number };

export type ResearchSession = {
  id: string;
  question: string;
  state: ResearchState;
  subqueries: string[];
  steps: Step[];
  error_code: string | null;
  message: string | null;
  report: Report | null;
};
export type ResearchListItem = { id: string; question: string; state: ResearchState; created_at: string };
export type ResearchPage = { items: ResearchListItem[]; next_cursor: string | null };

function parseStep(v: unknown): Step {
  if (!isRecord(v)) invalid();
  const st = v.state;
  return {
    seq: typeof v.seq === "number" ? v.seq : 0,
    kind: typeof v.kind === "string" ? v.kind : "step",
    query: typeof v.query === "string" ? v.query : null,
    state: st === "running" || st === "done" || st === "failed" ? st : "pending",
  };
}

function parseReport(v: unknown): Report | null {
  if (!isRecord(v) || !Array.isArray(v.sections)) return null;
  const sections: Section[] = v.sections.filter(isRecord).map((s) => ({
    heading: typeof s.heading === "string" ? s.heading : "",
    statements: (Array.isArray(s.statements) ? s.statements : []).filter(isRecord).map((st) => ({
      text: typeof st.text === "string" ? st.text : "",
      citations: parseCitations(st.citations),
    })),
  }));
  const ver = isRecord(v.verification) ? v.verification : {};
  const withheld = typeof ver.withheld_count === "number" ? ver.withheld_count : 0;
  return { sections, withheld };
}

export function parseSession(v: unknown): ResearchSession {
  if (!isRecord(v)) invalid();
  const plan = isRecord(v.plan) ? v.plan : {};
  return {
    id: str(v, "id"),
    question: typeof v.question === "string" ? v.question : "",
    state: oneOf(v, "state", RESEARCH_STATES),
    subqueries: Array.isArray(plan.subqueries) ? plan.subqueries.filter((q): q is string => typeof q === "string") : [],
    steps: Array.isArray(v.steps) ? v.steps.map(parseStep) : [],
    error_code: typeof v.error_code === "string" ? v.error_code : null,
    message: typeof v.message === "string" ? v.message : null,
    report: v.report ? parseReport(v.report) : null,
  };
}

export function parseResearchPage(v: unknown): ResearchPage {
  if (!isRecord(v) || !Array.isArray(v.items)) invalid();
  const items = v.items.filter(isRecord).map((i) => ({
    id: str(i, "id"),
    question: typeof i.question === "string" ? i.question : "",
    state: oneOf(i, "state", RESEARCH_STATES),
    created_at: typeof i.created_at === "string" ? i.created_at : "",
  }));
  return { items, next_cursor: typeof v.next_cursor === "string" ? v.next_cursor : null };
}

const base = (orgId: string) => `/v1/orgs/${encodeURIComponent(orgId)}/research`;
const one = (orgId: string, id: string) => `${base(orgId)}/${encodeURIComponent(id)}`;

export function startResearch(fetcher: Fetcher, orgId: string, input: { question: string; workspace_ids?: string[]; limits?: Partial<Limits> }) {
  const body: Record<string, unknown> = { question: input.question };
  if (input.workspace_ids?.length) body.workspace_ids = input.workspace_ids;
  if (input.limits && Object.keys(input.limits).length) body.limits = input.limits;
  return requestJson(fetcher, base(orgId) as ApiPath, (v) => ({ id: isRecord(v) ? str(v, "id") : invalid() }), jsonBody(body));
}

export function getResearch(fetcher: Fetcher, orgId: string, id: string, signal?: AbortSignal) {
  return requestJson(fetcher, one(orgId, id) as ApiPath, parseSession, { signal });
}

export function listResearch(fetcher: Fetcher, orgId: string, opts: { after?: string | null; limit?: number } = {}, signal?: AbortSignal) {
  const qs = new URLSearchParams({ limit: String(opts.limit ?? 30) });
  if (opts.after) qs.set("after", opts.after);
  return requestJson(fetcher, `${base(orgId)}?${qs}` as ApiPath, parseResearchPage, { signal });
}

export async function cancelResearch(fetcher: Fetcher, orgId: string, id: string): Promise<void> {
  let res: Response;
  try {
    res = await fetcher(`${one(orgId, id)}/cancel` as ApiPath, jsonBody({}));
  } catch {
    throw new ApiError(0, "network_error");
  }
  if (!res.ok) throw new ApiError(res.status, `http_${res.status}`);
}

/** Same-origin BFF URL for the markdown export (a plain link; the browser sends its session cookie). */
export const exportUrl = (orgId: string, id: string): string => `/api${one(orgId, id)}/export?format=md`;

/** Refetch interval for the session poll: every 2 s until a terminal state (a 4xx/invalid response stops it too). */
export function pollInterval(data: ResearchSession | undefined, failed = false): number | false {
  if (failed) return false;
  if (!data) return POLL_MS;
  return isTerminal(data.state) ? false : POLL_MS;
}

const ERROR_COPY: Record<string, string> = {
  model_unavailable: "The language model isn't available right now.",
  policy_denied: "This research isn't allowed for the selected sources.",
  search_unavailable: "Search isn't available right now.",
  timeout: "The research ran out of time.",
  limit_exceeded: "The research reached its limits.",
  internal_error: "Something went wrong while researching.",
};
export const errorCopy = (code: string | null): string => ERROR_COPY[code ?? ""] ?? "The research could not be completed.";
