import type { IngestionSummary, Stage } from "../api";

/** A full `Document.ingestion` summary (real shape, m03 §13). */
export const summary = (over: Partial<IngestionSummary> = {}): IngestionSummary => ({
  state: "queued",
  stage: null,
  attempts_label: null,
  error_code: null,
  message: null,
  updated_at: null,
  can_retry: false,
  ...over,
});

/** `GET …/ingestion` body. */
export const statusBody = (s: IngestionSummary, extra: Record<string, unknown> = {}) => ({
  ...s,
  document_id: "d-1",
  version_id: "v-1",
  job_id: "j-1",
  revision_id: "rev-secret-id",
  attempts: 1,
  max_attempts: 5,
  published_at: null,
  events: [],
  next_after: null,
  ...extra,
});

/** SSE `snapshot` data. */
export const snap = (s: IngestionSummary) => ({ cursor: 0, document_id: "d-1", version_id: "v-1", ingestion: s });

/** SSE `job` data. */
export const job = (seq: number, s: IngestionSummary, label = "Stage started", stage: Stage | null = s.stage) => ({
  seq,
  document_id: "d-1",
  version_id: "v-1",
  type: "stage_started",
  label,
  stage,
  attempt: 1,
  at: "2026-11-18T12:00:0" + (seq % 10) + "Z",
  ingestion: s,
});
