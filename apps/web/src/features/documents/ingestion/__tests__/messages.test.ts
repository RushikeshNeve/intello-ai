import { describe, expect, it } from "vitest";
import { INGESTION_STATES, parseStatus, parseSummaryLoose, toRetryOutcome } from "../api";
import { ApiError } from "@/features/organizations/api/http";
import { ERROR_MESSAGES, GENERIC_FAILURE, badgeText, displayKind, failureMessage, stepIndex } from "../messages";
import { summary } from "./fixtures";

// The closed set of m03 §7 plus the pipeline codes written by the workers.
const KNOWN = [
  "unsupported_type", "no_text_extracted", "pdf_unreadable", "encoding_unreadable", "too_large", "too_many_pages", "malformed_input",
  "attempts_exhausted", "timeout", "lease_expired", "internal_error", "source_missing", "embedding_error", "organization_unavailable", "processing_failed", "storage_unavailable", "index_unavailable", "index_error", "index_incomplete",
  "embedding_count_mismatch", "embedding_space_mismatch", "lease_lost", "visibility_timeout",
];

describe("failure messages", () => {
  it("has a human message for every known code, with no code or internals in it", () => {
    for (const code of KNOWN) {
      const m = failureMessage(code);
      if (code !== "processing_failed") expect(m, code).not.toBe(GENERIC_FAILURE);
      expect(m, code).not.toContain(code);
      expect(m).not.toMatch(/traceback|exception|postgres|rushdb|celery|stack|uuid|[0-9a-f]{8}-[0-9a-f]{4}/i);
    }
    expect(Object.keys(ERROR_MESSAGES).sort()).toEqual(expect.arrayContaining(KNOWN));
  });

  it("falls back to a generic safe message for unknown, empty or hostile codes", () => {
    for (const code of ["totally_new", "", null, undefined, "__proto__", "constructor", "toString", "Traceback (most recent call last)", "<script>alert(1)</script>"]) {
      expect(failureMessage(code as string | null)).toBe(GENERIC_FAILURE);
    }
  });
});

describe("display kinds and badge text", () => {
  it("covers every API state", () => {
    const t = (over: Parameters<typeof summary>[0]) => badgeText(summary(over));
    expect(t({ state: "queued" })).toBe("Queued");
    expect(t({ state: "processing", stage: "index" })).toBe("Processing (Indexing)");
    expect(t({ state: "processing" })).toBe("Processing");
    expect(t({ state: "ready" })).toBe("Ready");
    expect(t({ state: "failed" })).toBe("Failed");
    expect(t({ state: "unsupported" })).toBe("Not supported");
    expect(t({ state: "no_text" })).toBe("No text found");
    expect(t({ state: "cancelled" })).toBe("Cancelled");
    expect(t({ state: "none" })).toBe("Not processed");
    expect(t({ state: "unsupported", error_code: "no_text_extracted" })).toBe("No text found");
    expect(displayKind({ state: "failed", error_code: "no_text_extracted" })).toBe("no_text");
    expect(INGESTION_STATES.length).toBe(8);
  });

  it("maps stages onto the five visible steps (validate belongs to index)", () => {
    expect(stepIndex("extract")).toBe(0);
    expect(stepIndex("validate")).toBe(stepIndex("index"));
    expect(stepIndex("publish")).toBe(4);
    expect(stepIndex(null)).toBe(-1);
  });

  it("prefers the server message, falls back to the local map", () => {
    expect(failureMessage("pdf_unreadable", "Server says hi.")).toBe("Server says hi.");
    expect(failureMessage("pdf_unreadable", "  ")).toBe(ERROR_MESSAGES.pdf_unreadable);
    expect(failureMessage("nope", null)).toBe(GENERIC_FAILURE);
  });
});

describe("parsing", () => {
  it("parses the real status body: events use id, summary fields and can_retry", () => {
    const s = parseStatus({
      state: "failed", stage: null, attempts_label: null, error_code: "pdf_unreadable", message: "The PDF could not be read.", updated_at: "2026-11-18T12:00:00Z", can_retry: true,
      document_id: "d", version_id: "v", job_id: "j", revision_id: null, attempts: 5, max_attempts: 5, published_at: null,
      events: [{ id: 7, type: "dead", label: "Failed", stage: "extract", attempt: 5, at: "2026-11-18T12:00:00Z" }], next_after: null,
    });
    expect(s).toMatchObject({ can_retry: true, message: "The PDF could not be read.", attempts: 5 });
    expect(s.events[0]).toMatchObject({ seq: 7, label: "Failed", stage: "extract", attempt: 5 });
  });
  it("rejects unknown states, tolerates unknown stages and a missing timeline", () => {
    expect(() => parseStatus({ state: "exploded" })).toThrow();
    const s = parseStatus({ state: "processing", stage: "teleport", attempts: 1, max_attempts: 5 });
    expect(s.stage).toBeNull();
    expect(s.events).toEqual([]);
  });
  it("a malformed Document.ingestion is null, not an error", () => {
    expect(parseSummaryLoose(undefined)).toBeNull();
    expect(parseSummaryLoose({ state: 7 })).toBeNull();
    expect(parseSummaryLoose({ state: "queued", stage: "extract", can_retry: true })).toMatchObject({ state: "queued", can_retry: true, message: null });
    expect(parseSummaryLoose({ state: "indexed" })).toBeNull(); // pre-§13 vocabulary is not accepted
  });
  it("maps retry errors to calm outcomes", () => {
    expect(toRetryOutcome(new ApiError(409, "conflict"))).toBe("already_running");
    expect(toRetryOutcome(new ApiError(403, "forbidden"))).toBe("not_allowed");
    expect(toRetryOutcome(new ApiError(404, "not_found"))).toBe("not_found");
    expect(toRetryOutcome(new ApiError(0, "network_error"))).toBe("unavailable");
    expect(toRetryOutcome(new ApiError(500, "boom"))).toBe("failed");
    expect(toRetryOutcome(new Error("x"))).toBe("failed");
  });
});
