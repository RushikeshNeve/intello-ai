import { describe, expect, it } from "vitest";
import { INGESTION_STATES, parseStatus, parseSummaryLoose, toRetryOutcome } from "../api";
import { ApiError } from "@/features/organizations/api/http";
import { ERROR_MESSAGES, GENERIC_FAILURE, badgeText, displayKind, failureMessage, isRetryable, stepIndex } from "../messages";

// The closed set of m03 §7 plus the pipeline codes written by the workers.
const KNOWN = [
  "unsupported_type", "no_text_extracted", "pdf_unreadable", "encoding_unreadable", "too_large", "too_many_pages", "malformed_input",
  "attempts_exhausted", "timeout", "storage_unavailable", "index_unavailable", "index_error", "index_incomplete",
  "embedding_count_mismatch", "embedding_space_mismatch", "lease_lost", "visibility_timeout",
];

describe("failure messages", () => {
  it("has a human message for every known code, with no code or internals in it", () => {
    for (const code of KNOWN) {
      const m = failureMessage(code);
      expect(m, code).not.toBe(GENERIC_FAILURE);
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
    const base = { error_code: null as string | null };
    expect(badgeText({ ...base, state: "queued", stage: null, updated_at: null })).toBe("Queued");
    expect(badgeText({ ...base, state: "processing", stage: "index", updated_at: null })).toBe("Processing (Indexing)");
    expect(badgeText({ ...base, state: "processing", stage: null, updated_at: null })).toBe("Processing");
    expect(badgeText({ ...base, state: "indexed", stage: null, updated_at: null })).toBe("Ready");
    expect(badgeText({ ...base, state: "failed", stage: null, updated_at: null })).toBe("Failed");
    expect(badgeText({ ...base, state: "unsupported", stage: null, updated_at: null })).toBe("Not supported");
    expect(badgeText({ ...base, state: "cancelled", stage: null, updated_at: null })).toBe("Cancelled");
    expect(badgeText({ ...base, state: "none", stage: null, updated_at: null })).toBe("Not processed");
    expect(badgeText({ state: "failed", stage: "extract", error_code: "no_text_extracted", updated_at: null })).toBe("No text found");
    expect(displayKind({ state: "unsupported", error_code: "no_text_extracted" })).toBe("no_text");
    expect(INGESTION_STATES.length).toBe(7);
  });

  it("maps stages onto the five visible steps (validate belongs to index)", () => {
    expect(stepIndex("extract")).toBe(0);
    expect(stepIndex("validate")).toBe(stepIndex("index"));
    expect(stepIndex("publish")).toBe(4);
    expect(stepIndex(null)).toBe(-1);
  });

  it("only failed and unprocessed documents are retryable", () => {
    expect(INGESTION_STATES.filter(isRetryable)).toEqual(["none", "failed"]);
  });
});

describe("parsing", () => {
  it("rejects unknown states, tolerates unknown stages and a missing timeline", () => {
    expect(() => parseStatus({ state: "exploded" })).toThrow();
    const s = parseStatus({ state: "processing", stage: "teleport", updated_at: null, error_code: null, attempts: 1, max_attempts: 5 });
    expect(s.stage).toBeNull();
    expect(s.events).toEqual([]);
  });
  it("a malformed Document.ingestion is null, not an error", () => {
    expect(parseSummaryLoose(undefined)).toBeNull();
    expect(parseSummaryLoose({ state: 7 })).toBeNull();
    expect(parseSummaryLoose({ state: "queued", stage: "extract", updated_at: null, error_code: null })).toMatchObject({ state: "queued" });
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
