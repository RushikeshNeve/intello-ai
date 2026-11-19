import { describe, expect, it } from "vitest";
import { ApiError } from "@/features/organizations/api/http";
import {
  parseCitations,
  parseMessage,
  parseRunAccepted,
  parseRunEvent,
  parseThread,
  parseThreadDetail,
  parseThreadPage,
  parseVerification,
  runEventsUrl,
  toChatErrorCode,
} from "../api";
import { wireCit } from "./fixtures";

describe("chat parsers", () => {
  it("parses a thread and tolerates a null workspace/title", () => {
    expect(parseThread({ id: "t", workspace_id: null, title: null, created_at: "a", updated_at: "b" })).toEqual({
      id: "t",
      workspace_id: null,
      title: "",
      created_at: "a",
      updated_at: "b",
    });
    expect(() => parseThread({ title: "x" })).toThrow(ApiError);
  });

  it("keeps valid citations and drops malformed ones", () => {
    const out = parseCitations([wireCit(1), { marker: "1" }, null, wireCit(2, { page_start: null })]);
    expect(out.map((c) => c.marker)).toEqual(["[1]", "[2]"]);
    expect(out[1].page_start).toBeNull();
    expect(parseCitations("nope")).toEqual([]);
  });

  it("parses verification strictly and clamps counts", () => {
    expect(parseVerification({ status: "partial", withheld_count: 2, flagged_count: -1 })).toEqual({ status: "partial", withheld_count: 2, flagged_count: 0 });
    expect(parseVerification({ status: "weird" })).toBeNull();
    expect(parseVerification(null)).toBeNull();
  });

  it("parses messages: unknown roles are rejected, optional fields default", () => {
    const m = parseMessage({ id: "m", role: "assistant", content: "hi [1]", citations: [wireCit(1)], verification: { status: "grounded", withheld_count: 0, flagged_count: 0 }, run_id: "r", created_at: "t" });
    expect(m.citations).toHaveLength(1);
    expect(m.verification?.status).toBe("grounded");
    expect(parseMessage({ id: "m2", role: "user", content: "q" }).run_id).toBeNull();
    expect(() => parseMessage({ id: "m", role: "system", content: "x" })).toThrow(ApiError);
  });

  it("parses a thread detail with an active run, dropping bad messages", () => {
    const d = parseThreadDetail({
      thread: { id: "t", title: "T" },
      messages: [{ id: "m1", role: "user", content: "q" }, { bogus: true }],
      active_run: { run_id: "r1", state: "running" },
    });
    expect(d.messages).toHaveLength(1);
    expect(d.active_run).toEqual({ run_id: "r1", state: "running" });
    expect(parseThreadDetail({ thread: { id: "t" }, messages: [], active_run: { run_id: "r", state: "???" } }).active_run).toBeNull();
    expect(() => parseThreadDetail({ messages: [] })).toThrow(ApiError);
  });

  it("parses the thread page and run acceptance", () => {
    expect(parseThreadPage({ items: [{ id: "t" }, {}], next_cursor: "c" })).toMatchObject({ items: [{ id: "t" }], next_cursor: "c" });
    expect(parseRunAccepted({ run_id: "r", message_id: "m", state: "queued" })).toEqual({ run_id: "r", message_id: "m", state: "queued" });
    expect(() => parseRunAccepted({ message_id: "m" })).toThrow(ApiError);
  });

  it("builds the same-origin SSE URL and encodes ids", () => {
    expect(runEventsUrl("o1", "r/1")).toBe("/api/v1/orgs/o1/chat/runs/r%2F1/events");
  });
});

describe("parseRunEvent", () => {
  it("parses every event type", () => {
    expect(parseRunEvent("started", "{}")).toEqual({ type: "started" });
    expect(parseRunEvent("retrieval", '{"evidence_count":3,"dropped":1,"degraded":["embedding_unavailable"]}')).toEqual({
      type: "retrieval",
      evidence_count: 3,
      dropped: 1,
      degraded: ["embedding_unavailable"],
    });
    expect(parseRunEvent("delta", '{"text":"Hel"}')).toEqual({ type: "delta", text: "Hel" });
    expect(parseRunEvent("citations", JSON.stringify({ citations: [wireCit(1)] }))).toMatchObject({ type: "citations", citations: [{ marker: "[1]" }] });
    expect(parseRunEvent("verification", '{"status":"abstained","withheld_count":0,"flagged_count":0}')).toMatchObject({ type: "verification" });
    expect(parseRunEvent("completed", '{"message_id":"m","answer_text":"Done"}')).toEqual({ type: "completed", message_id: "m", answer_text: "Done" });
    expect(parseRunEvent("failed", '{"error_code":"timeout","message":"x"}')).toEqual({ type: "failed", error_code: "timeout", message: "x" });
    expect(parseRunEvent("cancelled", "{}")).toEqual({ type: "cancelled" });
  });

  it("parses end frames; an unknown reason is 'unavailable'", () => {
    expect(parseRunEvent("end", '{"reason":"terminal"}')).toEqual({ type: "end", reason: "terminal" });
    expect(parseRunEvent("end", '{"reason":"timeout"}')).toEqual({ type: "end", reason: "timeout" });
    expect(parseRunEvent("end", '{"reason":"???"}')).toEqual({ type: "end", reason: "unavailable" });
  });

  it("ignores unknown types, bad JSON, non-objects and incomplete events", () => {
    expect(parseRunEvent("mystery", "{}")).toBeNull();
    expect(parseRunEvent("delta", "{nope")).toBeNull();
    expect(parseRunEvent("delta", "[1]")).toBeNull();
    expect(parseRunEvent("delta", "{}")).toBeNull();
    expect(parseRunEvent("completed", "{}")).toBeNull();
    expect(parseRunEvent("verification", '{"status":"x"}')).toBeNull();
  });

  it("defaults a failed event without a code to internal_error", () => {
    expect(parseRunEvent("failed", "{}")).toEqual({ type: "failed", error_code: "internal_error", message: "" });
  });
});

describe("toChatErrorCode", () => {
  it("maps statuses and codes", () => {
    expect(toChatErrorCode(new ApiError(429, "rate_limited"))).toBe("rate_limited");
    expect(toChatErrorCode(new ApiError(409, "too_many_runs"))).toBe("too_many_runs");
    expect(toChatErrorCode(new ApiError(404, "not_found"))).toBe("not_found");
    expect(toChatErrorCode(new ApiError(422, "validation_error"))).toBe("invalid");
    expect(toChatErrorCode(new ApiError(0, "network_error"))).toBe("network");
    expect(toChatErrorCode(new ApiError(503, "x"))).toBe("unavailable");
    expect(toChatErrorCode(new ApiError(401, "x"))).toBe("unauthenticated");
    expect(toChatErrorCode(new Error("x"))).toBe("unknown");
  });
});
