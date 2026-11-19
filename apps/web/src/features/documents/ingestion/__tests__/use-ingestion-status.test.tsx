// @vitest-environment jsdom
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { fakeFetcher, jsonResponse } from "@/features/organizations/__tests__/helpers";
import { QueryProvider, makeQueryClient } from "@/lib/query/provider";
import type { IngestionSummary } from "../api";
import { job, snap, statusBody, summary } from "./fixtures";
import { MAX_SSE_FAILURES, RECONNECT_BASE_MS, useIngestionStatus, type EventSourceLike } from "../use-ingestion-status";

const WS = "/v1/orgs/o1/workspaces/w1/documents";
const queued = summary({ state: "queued", updated_at: "2026-11-18T12:00:00Z" });

class FakeES implements EventSourceLike {
  static all: FakeES[] = [];
  onopen: ((e: Event) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  closed = false;
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  constructor(readonly url: string) {
    FakeES.all.push(this);
  }
  addEventListener(t: string, l: (e: MessageEvent<string>) => void) {
    this.listeners.set(t, l);
  }
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.(new Event("open"));
  }
  emit(type: "snapshot" | "job", body: unknown) {
    this.listeners.get(type)?.({ data: JSON.stringify(body) } as MessageEvent<string>);
  }
  fail() {
    this.onerror?.(new Event("error"));
  }
}
const factory = (url: string) => new FakeES(url);
const live = () => FakeES.all.filter((s) => !s.closed);

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event("visibilitychange"));
}

function setup(over: { documentId?: string; versionRef?: string; initial?: IngestionSummary | null; eventSource?: typeof factory | null; status?: () => Response } = {}) {
  const status = over.status ?? (() => jsonResponse(200, statusBody(queued)));
  const { fetcher, calls } = fakeFetcher({
    [`GET ${WS}/d-1/ingestion`]: () => status(),
    [`GET ${WS}/d-2/ingestion`]: () => status(),
  });
  const client = makeQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryProvider client={client} fetcher={fetcher}>
      {children}
    </QueryProvider>
  );
  const hook = renderHook(
    (p: { documentId: string; versionRef: string }) =>
      useIngestionStatus({
        orgId: "o1",
        workspaceId: "w1",
        documentId: p.documentId,
        versionRef: p.versionRef,
        initial: over.initial === undefined ? queued : over.initial,
        eventSource: over.eventSource === undefined ? factory : over.eventSource,
        pollMs: 5000,
      }),
    { wrapper, initialProps: { documentId: over.documentId ?? "d-1", versionRef: over.versionRef ?? "v-1" } },
  );
  return { ...hook, calls };
}

beforeEach(() => {
  FakeES.all = [];
  vi.useFakeTimers();
  setHidden(false);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useIngestionStatus", () => {
  it("opens one stream against the BFF path and applies snapshot then job events", () => {
    const { result } = setup();
    expect(FakeES.all).toHaveLength(1);
    expect(FakeES.all[0].url).toBe("/api/v1/orgs/o1/workspaces/w1/documents/d-1/ingestion/events");
    act(() => FakeES.all[0].open());
    expect(result.current.connection).toBe("live");
    act(() => FakeES.all[0].emit("snapshot", snap(queued)));
    act(() => FakeES.all[0].emit("job", job(1, summary({ state: "processing", stage: "extract" }))));
    expect(result.current.summary).toMatchObject({ state: "processing", stage: "extract" });
    act(() => FakeES.all[0].emit("job", job(1, summary({ state: "processing", stage: "extract" }))));
    expect(result.current.events.filter((e) => e.seq === 1)).toHaveLength(1); // de-duplicated by seq
  });

  it("ignores malformed payloads", () => {
    const { result } = setup();
    act(() => FakeES.all[0].listeners.get("job")?.({ data: "not json" } as MessageEvent<string>));
    act(() => FakeES.all[0].emit("job", { seq: 1, ingestion: { state: "exploded" } }));
    expect(result.current.summary).toEqual(queued);
  });

  it("reconnects with exponential back-off, then falls back to polling after repeated failures", async () => {
    const { result, calls } = setup();
    for (let i = 1; i < MAX_SSE_FAILURES; i++) {
      act(() => live()[0].fail());
      expect(live()).toHaveLength(0);
      act(() => void vi.advanceTimersByTime(RECONNECT_BASE_MS * 2 ** (i - 1) - 1));
      expect(live()).toHaveLength(0); // not yet
      act(() => void vi.advanceTimersByTime(1));
      expect(live()).toHaveLength(1);
    }
    act(() => live()[0].fail());
    expect(result.current.connection).toBe("polling");
    expect(calls.filter((c) => c.path.endsWith("/ingestion"))).toHaveLength(0);
    await act(async () => void (await vi.advanceTimersByTimeAsync(5000)));
    expect(calls.filter((c) => c.path.endsWith("/ingestion"))).toHaveLength(1);
    await act(async () => void (await vi.advanceTimersByTimeAsync(5000)));
    expect(calls.filter((c) => c.path.endsWith("/ingestion"))).toHaveLength(2);
    expect(live()).toHaveLength(0);
  });

  it("a successful open resets the failure count", () => {
    setup();
    for (let round = 0; round < MAX_SSE_FAILURES + 2; round++) {
      act(() => live()[0].open());
      act(() => live()[0].fail());
      act(() => void vi.advanceTimersByTime(RECONNECT_BASE_MS));
      expect(live()).toHaveLength(1); // still streaming, never degraded to polling
    }
  });

  it("polls every 5 s without EventSource and stops at a terminal state", async () => {
    let n = 0;
    const { result, calls } = setup({
      eventSource: null,
      status: () => {
        n++;
        return jsonResponse(200, statusBody(summary({ state: n >= 2 ? "ready" : "processing", stage: n >= 2 ? null : "embed" })));
      },
    });
    expect(result.current.connection).toBe("polling");
    await act(async () => void (await vi.advanceTimersByTimeAsync(5000)));
    expect(result.current.summary?.state).toBe("processing");
    await act(async () => void (await vi.advanceTimersByTimeAsync(5000)));
    expect(result.current.summary?.state).toBe("ready");
    expect(result.current.connection).toBe("idle");
    const total = calls.length;
    await act(async () => void (await vi.advanceTimersByTimeAsync(60_000)));
    expect(calls.length).toBe(total); // nothing after the terminal state
  });

  it("closes the stream when a terminal event arrives", () => {
    const { result } = setup();
    act(() => FakeES.all[0].emit("job", job(2, summary({ state: "ready", stage: null }))));
    expect(result.current.summary?.state).toBe("ready");
    expect(FakeES.all[0].closed).toBe(true);
    expect(live()).toHaveLength(0);
  });

  it("does not open anything when the document is already terminal", () => {
    setup({ initial: summary({ state: "failed", stage: "embed", error_code: "index_error" }) });
    expect(FakeES.all).toHaveLength(0);
  });

  it("pauses while the tab is hidden and resumes with one refresh", async () => {
    const { calls } = setup();
    expect(live()).toHaveLength(1);
    act(() => setHidden(true));
    expect(live()).toHaveLength(0);
    await act(async () => void (await vi.advanceTimersByTimeAsync(30_000)));
    expect(live()).toHaveLength(0);
    expect(calls).toHaveLength(0);
    await act(async () => setHidden(false));
    expect(live()).toHaveLength(1);
    expect(calls.filter((c) => c.path.endsWith("/ingestion"))).toHaveLength(1);
  });

  it("stops polling timers while hidden", async () => {
    const { calls } = setup({ eventSource: null });
    act(() => setHidden(true));
    await act(async () => void (await vi.advanceTimersByTimeAsync(60_000)));
    expect(calls).toHaveLength(0);
  });

  it("cleans up on unmount: stream closed, no timers or listeners left", async () => {
    const { unmount, calls } = setup();
    act(() => live()[0].fail()); // a reconnect timer is pending
    const removeSpy = vi.spyOn(document, "removeEventListener");
    unmount();
    expect(removeSpy).toHaveBeenCalledWith("visibilitychange", expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => void (await vi.advanceTimersByTimeAsync(60_000)));
    expect(live()).toHaveLength(0);
    expect(FakeES.all).toHaveLength(1);
    expect(calls).toHaveLength(0);
    const open = setup();
    open.unmount();
    expect(live()).toHaveLength(0);
  });

  it("switching documents closes the old stream and never shows the old state", () => {
    const { result, rerender } = setup();
    act(() => FakeES.all[0].emit("job", job(1, summary({ state: "processing", stage: "chunk" }))));
    expect(result.current.summary?.stage).toBe("chunk");
    const first = FakeES.all[0];
    rerender({ documentId: "d-2", versionRef: "v-9" });
    expect(first.closed).toBe(true);
    expect(result.current.summary).toEqual(queued); // the new document's own initial state, not d-1's
    expect(result.current.events).toEqual([]);
    expect(FakeES.all[1].url).toContain("/documents/d-2/");
    // a late event from the old stream cannot leak in
    act(() => first.emit("job", job(5, summary({ state: "ready", stage: null }))));
    expect(result.current.summary?.state).toBe("queued");
  });

  it("a new version of the same document is a new key", () => {
    const { result, rerender } = setup();
    act(() => FakeES.all[0].emit("job", job(1, summary({ state: "processing", stage: "chunk" }))));
    rerender({ documentId: "d-1", versionRef: "v-2" });
    expect(result.current.summary).toEqual(queued);
    expect(FakeES.all[0].closed).toBe(true);
  });

  it("refresh() re-reads the status and restarts the stream after a retry", async () => {
    const { result } = setup({
      initial: summary({ state: "failed", stage: "embed", error_code: "index_error" }),
      status: () => jsonResponse(200, statusBody(queued)),
    });
    expect(FakeES.all).toHaveLength(0);
    await act(async () => void (await result.current.refresh()));
    expect(result.current.summary?.state).toBe("queued");
    expect(live()).toHaveLength(1);
  });

  it("`end` (terminal) closes the stream without reconnecting and reads the persisted state once", async () => {
    const { calls } = setup();
    act(() => FakeES.all[0].listeners.get("end")?.({ data: JSON.stringify({ reason: "terminal" }) } as MessageEvent<string>));
    expect(FakeES.all[0].closed).toBe(true);
    await act(async () => void (await vi.advanceTimersByTimeAsync(60_000)));
    expect(FakeES.all).toHaveLength(1); // no automatic reopen
    expect(calls.filter((c) => c.path.endsWith("/ingestion"))).toHaveLength(1);
  });

  it("`end` (timeout) reopens once; a second timeout without a snapshot does not loop", async () => {
    setup();
    const end = (es: FakeES, reason: string) => es.listeners.get("end")?.({ data: JSON.stringify({ reason }) } as MessageEvent<string>);
    act(() => end(FakeES.all[0], "timeout"));
    expect(FakeES.all).toHaveLength(2);
    act(() => end(FakeES.all[1], "timeout"));
    expect(FakeES.all).toHaveLength(2);
    // a snapshot re-arms the single reopen
    act(() => FakeES.all[1].emit("snapshot", snap(queued)));
  });

  it("`end` (unavailable) stops calmly: no reconnect, no polling", async () => {
    const { result, calls } = setup();
    act(() => FakeES.all[0].listeners.get("end")?.({ data: JSON.stringify({ reason: "unavailable" }) } as MessageEvent<string>));
    expect(result.current.connection).toBe("stopped");
    await act(async () => void (await vi.advanceTimersByTimeAsync(60_000)));
    expect(FakeES.all).toHaveLength(1);
    expect(calls).toHaveLength(0);
  });
});
