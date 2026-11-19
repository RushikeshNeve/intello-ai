import { describe, expect, it } from "vitest";
import type { RunEvent } from "../api";
import { INITIAL_RUN_VIEW, applyRunEvent, type RunView } from "../run-state";
import { SseParser } from "../sse";
import { cit } from "./fixtures";

describe("SseParser", () => {
  it("parses frames with id/event/data and ignores comments and retry", () => {
    const p = new SseParser();
    const frames = p.push("retry: 3000\n\n: keep-alive\n\nid: 7\nevent: delta\ndata: {\"text\":\"hi\"}\n\n");
    expect(frames).toEqual([{ id: "7", event: "delta", data: '{"text":"hi"}' }]);
  });

  it("handles chunk boundaries anywhere, CRLF and CR line ends", () => {
    const p = new SseParser();
    const text = "id: 1\r\nevent: started\r\ndata: {}\r\n\r\nid: 2\revent: delta\rdata: {\"text\":\"a\"}\r\r\n"; // a lone trailing CR is held until the next byte decides (CR vs CRLF)
    const out = [];
    for (const ch of text) out.push(...p.push(ch)); // one char at a time
    expect(out.map((f) => [f.id, f.event])).toEqual([
      ["1", "started"],
      ["2", "delta"],
    ]);
  });

  it("joins multi-line data and defaults the event to 'message'", () => {
    const frames = new SseParser().push("data: a\ndata: b\n\n");
    expect(frames).toEqual([{ id: null, event: "message", data: "a\nb" }]);
  });

  it("keeps the last id on later frames (it persists, as in browsers)", () => {
    const p = new SseParser();
    const frames = p.push("id: 5\nevent: a\ndata: 1\n\nevent: b\ndata: 2\n\n");
    expect(frames.map((f) => f.id)).toEqual(["5", "5"]);
  });
});

const run = (events: [number | null, RunEvent][], from: RunView = INITIAL_RUN_VIEW) => events.reduce((v, [s, e]) => applyRunEvent(v, s, e), from);

describe("applyRunEvent", () => {
  it("accumulates deltas in order", () => {
    const v = run([
      [1, { type: "started" }],
      [2, { type: "delta", text: "Hel" }],
      [3, { type: "delta", text: "lo" }],
    ]);
    expect(v).toMatchObject({ status: "streaming", text: "Hello", lastSeq: 3 });
  });

  it("ignores replayed and duplicated seqs (no duplicated tokens)", () => {
    const v = run([
      [1, { type: "started" }],
      [2, { type: "delta", text: "Hel" }],
      [3, { type: "delta", text: "lo" }],
      [2, { type: "delta", text: "Hel" }], // replay after a reconnect
      [3, { type: "delta", text: "lo" }],
      [4, { type: "delta", text: "!" }],
    ]);
    expect(v.text).toBe("Hello!");
    expect(v.lastSeq).toBe(4);
  });

  it("replaces the streamed text with answer_text on completed", () => {
    const v = run([
      [1, { type: "delta", text: "Unsupported claim. Kept claim [1]." }],
      [2, { type: "completed", message_id: "m1", answer_text: "Kept claim [1]." }],
    ]);
    expect(v).toMatchObject({ status: "completed", text: "Kept claim [1].", messageId: "m1" });
  });

  it("stores citations, verification and retrieval info", () => {
    const v = run([
      [1, { type: "retrieval", evidence_count: 4, dropped: 1, degraded: [] }],
      [2, { type: "citations", citations: [cit(1)] }],
      [3, { type: "verification", verification: { status: "partial", withheld_count: 1, flagged_count: 0 } }],
    ]);
    expect(v.citations).toHaveLength(1);
    expect(v.verification?.status).toBe("partial");
    expect(v.retrieval?.evidence_count).toBe(4);
  });

  it("failed and cancelled are terminal and later events change nothing", () => {
    const failed = run([
      [1, { type: "delta", text: "abc" }],
      [2, { type: "failed", error_code: "model_unavailable", message: "x" }],
      [3, { type: "delta", text: "zzz" }],
    ]);
    expect(failed).toMatchObject({ status: "failed", text: "abc", error: { code: "model_unavailable" }, lastSeq: 3 });
    const cancelled = run([[1, { type: "cancelled" }], [2, { type: "completed", message_id: null, answer_text: "late" }]]);
    expect(cancelled.status).toBe("cancelled");
    expect(cancelled.text).toBe("");
  });

  it("applies frames without an id (no dedupe) and keeps lastSeq", () => {
    const v = run([[5, { type: "started" }], [null, { type: "delta", text: "x" }]]);
    expect(v).toMatchObject({ text: "x", lastSeq: 5 });
  });
});
