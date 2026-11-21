/**
 * Pure reducer for the live state of one chat run. Events carry a monotonic `seq` (the SSE `id`); a replayed or
 * duplicated event (seq <= lastSeq) is ignored, so a reconnect can never duplicate tokens.
 */
import type { Citation, RunEvent, Verification } from "./api";

/** What the run is doing now, derived from the events (`verifying` comes from the additive `stage` event). */
export type RunStage = "searching" | "reading" | "writing" | "verifying";

export type RunStatus = "queued" | "streaming" | "completed" | "failed" | "cancelled";

export type RunView = {
  status: RunStatus;
  stage: RunStage;
  /** Streamed text; replaced by `answer_text` when the run completes. */
  text: string;
  citations: Citation[];
  verification: Verification | null;
  retrieval: { evidence_count: number; dropped: number; degraded: string[] } | null;
  error: { code: string; message: string } | null;
  messageId: string | null;
  /** Highest `seq` applied; the `Last-Event-ID` of the next connection. 0 = nothing yet. */
  lastSeq: number;
};

export const INITIAL_RUN_VIEW: RunView = {
  status: "queued",
  stage: "searching",
  text: "",
  citations: [],
  verification: null,
  retrieval: null,
  error: null,
  messageId: null,
  lastSeq: 0,
};

export const isTerminalStatus = (s: RunStatus): boolean => s === "completed" || s === "failed" || s === "cancelled";

/** `seq` is null for a frame without an id (applied, not de-duplicated). */
export function applyRunEvent(view: RunView, seq: number | null, ev: RunEvent): RunView {
  if (seq !== null && seq <= view.lastSeq) return view; // replay
  if (isTerminalStatus(view.status)) return seq === null ? view : { ...view, lastSeq: Math.max(view.lastSeq, seq) };
  const lastSeq = seq === null ? view.lastSeq : seq;
  const base = { ...view, lastSeq };
  switch (ev.type) {
    case "started":
      return { ...base, status: "streaming" };
    case "retrieval":
      return { ...base, status: "streaming", stage: "reading", retrieval: { evidence_count: ev.evidence_count, dropped: ev.dropped, degraded: ev.degraded } };
    case "delta":
      return { ...base, status: "streaming", stage: "writing", text: view.text + ev.text };
    case "citations":
      return { ...base, citations: ev.citations };
    case "verification":
      return { ...base, verification: ev.verification };
    case "stage":
      return ev.stage === "verifying" ? { ...base, stage: "verifying" } : base;
    case "completed":
      // The kept text (unsupported statements removed) replaces whatever was streamed.
      return { ...base, status: "completed", text: ev.answer_text, messageId: ev.message_id };
    case "failed":
      return { ...base, status: "failed", error: { code: ev.error_code, message: ev.message } };
    case "cancelled":
      return { ...base, status: "cancelled", text: view.text.replace(/\s?\[\d{1,3}\]/g, "").trim() };
  }
}
