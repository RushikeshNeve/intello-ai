/** User-facing text for ingestion. Raw server text, ids and stack traces are never rendered (m03 §12). */
import type { IngestionSummary, Stage } from "./api";

/** Closed set from m03 §7 plus pipeline-level codes. Unknown codes fall back to GENERIC_FAILURE. */
export const ERROR_MESSAGES: Record<string, string> = {
  unsupported_type: "This file type can't be processed yet, so it won't appear in search.",
  no_text_extracted: "We couldn't find any text in this file. Scanned or image-only documents aren't supported yet.",
  no_text: "We couldn't find any text in this file. Scanned or image-only documents aren't supported yet.",
  pdf_unreadable: "This PDF couldn't be read. It may be encrypted, password-protected or damaged.",
  encoding_unreadable: "The text in this file couldn't be decoded. Try saving it as UTF-8 and uploading it again.",
  too_large: "This file is too large to process.",
  too_many_pages: "This file has too many pages to process.",
  malformed_input: "This file looks damaged, so it couldn't be processed.",
  attempts_exhausted: "Processing didn't succeed after several attempts. You can try again.",
  timeout: "Processing took too long and was stopped. You can try again.",
  storage_unavailable: "We couldn't reach file storage. Please try again in a few minutes.",
  index_unavailable: "The search index was unavailable. Please try again in a few minutes.",
  index_error: "We couldn't add this document to search. Please try again.",
  index_incomplete: "We couldn't add this document to search. Please try again.",
  lease_expired: "Processing was interrupted and did not recover. You can try again.",
  internal_error: "Processing failed unexpectedly. You can try again.",
  source_missing: "The stored file could not be found.",
  embedding_error: "We couldn't finish preparing this document for search. Please try again.",
  organization_unavailable: "This organization is unavailable right now.",
  processing_failed: "Something went wrong while processing this file. You can try again.",
  embedding_count_mismatch: "We couldn't finish preparing this document for search. Please try again.",
  embedding_space_mismatch: "We couldn't finish preparing this document for search. Please try again.",
  lease_lost: "Processing was interrupted. It will be retried, or you can try again.",
  visibility_timeout: "Processing was interrupted. It will be retried, or you can try again.",
};

export const GENERIC_FAILURE = "Something went wrong while processing this file. You can try again.";

/**
 * Prefers the server-supplied `message` (a closed, safe table server-side); falls back to the local map.
 * Safe by construction: only own keys of the map are looked up, and the code itself is never shown.
 */
export function failureMessage(code: string | null | undefined, serverMessage?: string | null): string {
  if (serverMessage && serverMessage.trim()) return serverMessage;
  if (code && Object.prototype.hasOwnProperty.call(ERROR_MESSAGES, code)) return ERROR_MESSAGES[code];
  return GENERIC_FAILURE;
}

const NO_TEXT_CODES = new Set(["no_text_extracted", "no_text"]);

/** What the badge shows. Finer than the API state: "No text found" and "Cancelled" are their own labels. */
export type DisplayKind = "none" | "queued" | "processing" | "ready" | "failed" | "unsupported" | "no_text" | "cancelled";

export function displayKind(s: Pick<IngestionSummary, "state" | "error_code">): DisplayKind {
  switch (s.state) {
    case "ready":
      return "ready";
    case "no_text":
      return "no_text";
    case "processing":
      return "processing";
    case "queued":
      return "queued";
    case "cancelled":
      return "cancelled";
    case "unsupported":
    case "failed":
      if (s.error_code && NO_TEXT_CODES.has(s.error_code)) return "no_text";
      return s.state;
    default:
      return "none";
  }
}

export const STAGE_LABELS: Record<Stage, string> = {
  extract: "Extracting text",
  chunk: "Splitting into passages",
  embed: "Preparing for search",
  index: "Indexing",
  validate: "Indexing",
  publish: "Publishing",
};

/** The five steps shown in the panel (`validate` is part of "index"). */
export const PROGRESS_STEPS: readonly { key: Exclude<Stage, "validate">; label: string }[] = [
  { key: "extract", label: "Extract" },
  { key: "chunk", label: "Chunk" },
  { key: "embed", label: "Embed" },
  { key: "index", label: "Index" },
  { key: "publish", label: "Publish" },
];

export function stepIndex(stage: Stage | null): number {
  if (!stage) return -1;
  return PROGRESS_STEPS.findIndex((s) => s.key === (stage === "validate" ? "index" : stage));
}

export const KIND_LABELS: Record<DisplayKind, string> = {
  none: "Not processed",
  queued: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
  unsupported: "Not supported",
  no_text: "No text found",
  cancelled: "Cancelled",
};

/** Badge text, including the stage while processing, e.g. "Processing (Indexing)". */
export function badgeText(s: IngestionSummary): string {
  const kind = displayKind(s);
  if (kind === "processing" && s.stage) return `${KIND_LABELS.processing} (${STAGE_LABELS[s.stage]})`;
  return KIND_LABELS[kind];
}

/** Sentence for the live region. */
export function announcement(s: IngestionSummary): string {
  const kind = displayKind(s);
  switch (kind) {
    case "ready":
      return "Processing finished. The document is ready.";
    case "failed":
    case "unsupported":
    case "no_text":
      return `Processing stopped. ${failureMessage(s.error_code, s.message)}`;
    case "cancelled":
      return "Processing was cancelled.";
    case "none":
      return "This document hasn't been processed yet.";
    default:
      return `Status: ${badgeText(s)}.`;
  }
}

export const RETRY_RESULT: Record<string, string> = {
  started: "Processing restarted. We'll update this page as it progresses.",
  already_running: "This document is already being processed.",
  not_allowed: "You don't have permission to retry processing.",
  not_found: "This document is no longer available.",
  unavailable: "We couldn't reach the server. Please try again in a moment.",
  failed: "We couldn't restart processing. Please try again.",
};
