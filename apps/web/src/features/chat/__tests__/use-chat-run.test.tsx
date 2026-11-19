// @vitest-environment jsdom
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { fakeFetcher, jsonResponse } from "@/features/organizations/__tests__/helpers";
import { QueryProvider, makeQueryClient } from "@/lib/query/provider";
import { useChatRun } from "../use-chat-run";
import { END, cit, frame, scripted, type Conn } from "./fixtures";

afterEach(() => {
  cleanup();
  Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
});

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event("visibilitychange"));
}

function setup(conns: Conn[], over: { runId?: string | null; routes?: Parameters<typeof fakeFetcher>[0]; onSettled?: () => void } = {}) {
  const { stream, calls } = scripted(conns);
  const api = fakeFetcher(over.routes ?? {});
  const client = makeQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryProvider client={client} fetcher={api.fetcher}>
      {children}
    </QueryProvider>
  );
  const onSettled = over.onSettled ?? vi.fn();
  const hook = renderHook(
    (p: { threadId: string; runId: string | null }) =>
      useChatRun({ orgId: "o1", threadId: p.threadId, runId: p.runId, stream, onSettled, reconnectBaseMs: 1, reconnectMaxMs: 2, maxFailures: 3, pollMs: 5 }),
    { wrapper, initialProps: { threadId: "t-1", runId: over.runId === undefined ? "r-1" : over.runId } },
  );
  return { ...hook, calls, api, onSettled };
}

describe("useChatRun", () => {
  it("streams deltas in order, applies citations and verification, and replaces the text with answer_text", async () => {
    const { result, calls, onSettled } = setup([
      {
        frames: [
          frame(1, "started"),
          frame(2, "retrieval", { evidence_count: 2, dropped: 0, degraded: [] }),
          frame(3, "delta", { text: "Revenue grew " }),
          frame(4, "delta", { text: "by 10% [1]. Also a bogus claim." }),
          frame(5, "citations", { citations: [cit(1)] }),
          frame(6, "verification", { status: "partial", withheld_count: 1, flagged_count: 0 }),
          frame(7, "completed", { message_id: "m-9", answer_text: "Revenue grew by 10% [1]." }),
          END("terminal"),
        ],
        then: "close",
      },
    ]);
    await waitFor(() => expect(result.current.view?.status).toBe("completed"));
    expect(result.current.view?.text).toBe("Revenue grew by 10% [1].");
    expect(result.current.view?.citations).toHaveLength(1);
    expect(result.current.view?.verification?.status).toBe("partial");
    expect(result.current.view?.messageId).toBe("m-9");
    expect(calls).toHaveLength(1); // stop on terminal: no reconnect
    expect(calls[0]).toMatchObject({ url: "/api/v1/orgs/o1/chat/runs/r-1/events", lastEventId: null });
    await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1));
    expect(result.current.connection).toBe("idle");
  });

  it("reconnects after a dropped connection with Last-Event-ID and never duplicates tokens on replay", async () => {
    const { result, calls } = setup([
      { frames: [frame(1, "started"), frame(2, "delta", { text: "Hel" }), frame(3, "delta", { text: "lo" })], then: "error" },
      // the server replays from the log: seq 2 and 3 arrive again before the new ones
      { frames: [frame(2, "delta", { text: "Hel" }), frame(3, "delta", { text: "lo" }), frame(4, "delta", { text: " there" }), frame(5, "completed", { message_id: "m", answer_text: "Hello there" }), END()], then: "close" },
    ]);
    await waitFor(() => expect(result.current.view?.status).toBe("completed"));
    expect(calls).toHaveLength(2);
    expect(calls[1].lastEventId).toBe(3);
    expect(result.current.view?.text).toBe("Hello there");
  });

  it("an in-progress replay never doubles text before completion", async () => {
    const { result } = setup([
      { frames: [frame(1, "delta", { text: "A" }), frame(2, "delta", { text: "B" })], then: "error" },
      { frames: [frame(1, "delta", { text: "A" }), frame(2, "delta", { text: "B" }), frame(3, "delta", { text: "C" })], then: "hang" },
    ]);
    await waitFor(() => expect(result.current.view?.lastSeq).toBe(3));
    expect(result.current.view?.text).toBe("ABC");
  });

  it("an `end: timeout` reconnects at once from the last seq; `terminal` stops", async () => {
    const { result, calls } = setup([
      { frames: [frame(1, "started"), frame(2, "delta", { text: "x" }), END("timeout")], then: "close" },
      { frames: [frame(3, "delta", { text: "y" }), frame(4, "completed", { message_id: "m", answer_text: "xy" }), END("terminal")], then: "close" },
    ]);
    await waitFor(() => expect(result.current.view?.status).toBe("completed"));
    expect(calls.map((c) => c.lastEventId)).toEqual([null, 2]);
  });

  it("`end: unavailable` stops calmly without reconnecting", async () => {
    const { result, calls, onSettled } = setup([{ frames: [frame(1, "started"), END("unavailable")], then: "close" }]);
    await waitFor(() => expect(result.current.connection).toBe("stopped"));
    await new Promise((r) => setTimeout(r, 30));
    expect(calls).toHaveLength(1);
    expect(result.current.view?.status).toBe("streaming");
    expect(onSettled).toHaveBeenCalledTimes(1);
  });

  it("ignores unknown event types and malformed frames", async () => {
    const { result } = setup([
      {
        frames: [
          frame(1, "started"),
          frame(2, "telemetry", { x: 1 }),
          { id: "3", event: "delta", data: "{not json" },
          frame(4, "delta", { text: "ok" }),
          frame(5, "completed", { message_id: null, answer_text: "ok" }),
          END(),
        ],
      },
    ]);
    await waitFor(() => expect(result.current.view?.status).toBe("completed"));
    expect(result.current.view?.text).toBe("ok");
  });

  it("surfaces failed with its error code and stops", async () => {
    const { result, calls } = setup([{ frames: [frame(1, "started"), frame(2, "failed", { error_code: "model_unavailable", message: "x" }), END()] }]);
    await waitFor(() => expect(result.current.view?.status).toBe("failed"));
    expect(result.current.view?.error?.code).toBe("model_unavailable");
    expect(calls).toHaveLength(1);
  });

  it("surfaces cancelled", async () => {
    const { result } = setup([{ frames: [frame(1, "started"), frame(2, "cancelled"), END()] }]);
    await waitFor(() => expect(result.current.view?.status).toBe("cancelled"));
  });

  it("falls back to polling the run after repeated stream failures", async () => {
    let polls = 0;
    const { result, calls, onSettled } = setup([{ frames: [], then: "error" }, { frames: [], then: "error" }, { frames: [], then: "error" }], {
      routes: {
        "GET /v1/orgs/o1/chat/runs/r-1": () => {
          polls += 1;
          return jsonResponse(200, polls < 2 ? { run_id: "r-1", state: "running" } : { run_id: "r-1", state: "failed", error_code: "timeout" });
        },
      },
    });
    await waitFor(() => expect(result.current.view?.status).toBe("failed"));
    expect(calls).toHaveLength(3);
    expect(result.current.view?.error?.code).toBe("timeout");
    expect(onSettled).toHaveBeenCalledTimes(1);
  });

  it("does not retry on a 404 from the stream (not your run)", async () => {
    const { result, calls } = setup([{ frames: [], httpStatus: 404 }]);
    await waitFor(() => expect(result.current.connection).toBe("stopped"));
    expect(calls).toHaveLength(1);
  });

  it("cancel POSTs and the cancelled event then arrives on the stream", async () => {
    let cancelled = false;
    const { result, api } = setup([{ frames: [frame(1, "started"), frame(2, "delta", { text: "a" })], then: "hang" }], {
      routes: {
        "POST /v1/orgs/o1/chat/runs/r-1/cancel": () => {
          cancelled = true;
          return jsonResponse(202, { state: "cancelled" });
        },
      },
    });
    await waitFor(() => expect(result.current.view?.text).toBe("a"));
    let ok = false;
    await act(async () => {
      ok = await result.current.cancel();
    });
    expect(ok).toBe(true);
    expect(cancelled).toBe(true);
    expect(api.calls.some((c) => c.method === "POST" && c.path.endsWith("/cancel"))).toBe(true);
    expect(result.current.cancelling).toBe(true);
  });

  it("cancel reports failure", async () => {
    const { result } = setup([{ frames: [frame(1, "started")], then: "hang" }], {
      routes: { "POST /v1/orgs/o1/chat/runs/r-1/cancel": () => jsonResponse(404, { detail: "not_found" }) },
    });
    await waitFor(() => expect(result.current.view?.status).toBe("streaming"));
    let ok = true;
    await act(async () => {
      ok = await result.current.cancel();
    });
    expect(ok).toBe(false);
    expect(result.current.cancelling).toBe(false);
  });

  it("aborts the stream on unmount", async () => {
    const { result, calls, unmount } = setup([{ frames: [frame(1, "started")], then: "hang" }]);
    await waitFor(() => expect(result.current.view?.status).toBe("streaming"));
    unmount();
    await waitFor(() => expect(calls[0].aborted).toBe(true));
    await new Promise((r) => setTimeout(r, 20));
    expect(calls).toHaveLength(1);
  });

  it("never shows another thread's or run's state after switching", async () => {
    const { result, rerender, calls } = setup([
      { frames: [frame(1, "started"), frame(2, "delta", { text: "thread one text" })], then: "hang" },
      { frames: [], then: "hang" },
    ]);
    await waitFor(() => expect(result.current.view?.text).toBe("thread one text"));
    rerender({ threadId: "t-2", runId: "r-2" });
    // synchronously after the switch: nothing of the previous run
    expect(result.current.view?.text).toBe("");
    expect(result.current.view?.lastSeq).toBe(0);
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[0].aborted).toBe(true);
    expect(calls[1]).toMatchObject({ url: "/api/v1/orgs/o1/chat/runs/r-2/events", lastEventId: null });
    expect(result.current.view?.text).toBe("");
    rerender({ threadId: "t-2", runId: null });
    expect(result.current.view).toBeNull();
  });

  it("closes the stream while the tab is hidden and resumes from the last seq when shown", async () => {
    const { result, calls } = setup([
      { frames: [frame(1, "started"), frame(2, "delta", { text: "a" })], then: "hang" },
      { frames: [frame(3, "delta", { text: "b" }), frame(4, "completed", { message_id: "m", answer_text: "ab" }), END()] },
    ]);
    await waitFor(() => expect(result.current.view?.text).toBe("a"));
    act(() => setHidden(true));
    await waitFor(() => expect(calls[0].aborted).toBe(true));
    await new Promise((r) => setTimeout(r, 20));
    expect(calls).toHaveLength(1); // no reconnect while hidden
    act(() => setHidden(false));
    await waitFor(() => expect(result.current.view?.status).toBe("completed"));
    expect(calls[1].lastEventId).toBe(2);
  });

  it("does nothing without a run id", () => {
    const { result, calls } = setup([], { runId: null });
    expect(result.current.view).toBeNull();
    expect(calls).toHaveLength(0);
  });
});
