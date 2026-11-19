"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useApiFetcher } from "@/lib/query/provider";
import { cancelRun, getRun, isTerminalState, parseRunEvent, runEventsUrl, type StreamEnd } from "./api";
import { INITIAL_RUN_VIEW, applyRunEvent, isTerminalStatus, type RunStatus, type RunView } from "./run-state";
import { fetchStream, StreamHttpError, type RunStream } from "./sse";

export const RECONNECT_BASE_MS = 1_000;
export const RECONNECT_MAX_MS = 15_000;
/** Consecutive stream failures before the hook stops streaming and polls `GET /chat/runs/{r}` instead. */
export const MAX_STREAM_FAILURES = 5;
export const POLL_MS = 3_000;

/** `stopped`: the server said the stream is unavailable (calm state, no hammering). */
export type Connection = "idle" | "connecting" | "live" | "reconnecting" | "polling" | "stopped";

export type UseChatRunOptions = {
  orgId: string;
  /** Part of the identity: a different thread (or run) never shows another's state. */
  threadId: string;
  runId: string | null;
  stream?: RunStream;
  /** Called once when the run is over (any terminal state, or the stream ended for good). */
  onSettled?: (info: { runId: string; status: RunStatus }) => void;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  maxFailures?: number;
  pollMs?: number;
};

export type ChatRunLive = {
  /** Null while there is no run. */
  view: RunView | null;
  connection: Connection;
  cancelling: boolean;
  /** Asks the server to cancel; resolves false when the request failed. The `cancelled` event arrives on the stream. */
  cancel: () => Promise<boolean>;
};

type State = { key: string; view: RunView; connection: Connection; cancelling: boolean };

/**
 * Live state of one chat run over SSE (`GET /chat/runs/{r}/events`).
 *  - Frames are applied in order and de-duplicated by `seq`; every (re)connect sends `Last-Event-ID` = last applied seq,
 *    so a replay can never duplicate tokens. `completed.answer_text` replaces the streamed text.
 *  - `end` frames: `terminal` stops, `timeout` reconnects at once, `unavailable` stops calmly. Without an `end` (a
 *    dropped connection) it reconnects with exponential back-off, then falls back to polling the run.
 *  - A hidden tab closes the stream and resumes (from the last seq) when shown; unmount or a different run aborts everything.
 */
export function useChatRun(opts: UseChatRunOptions): ChatRunLive {
  const { orgId, threadId, runId } = opts;
  const fetcher = useApiFetcher();
  const stream = opts.stream ?? fetchStream;
  const key = `${orgId}/${threadId}/${runId ?? ""}`;
  const [state, setState] = useState<State>({ key, view: INITIAL_RUN_VIEW, connection: "idle", cancelling: false });
  const onSettledRef = useRef(opts.onSettled);
  useEffect(() => {
    onSettledRef.current = opts.onSettled;
  });
  const tuning = useRef({ base: RECONNECT_BASE_MS, max: RECONNECT_MAX_MS, failures: MAX_STREAM_FAILURES, poll: POLL_MS });
  useEffect(() => {
    tuning.current = {
      base: opts.reconnectBaseMs ?? RECONNECT_BASE_MS,
      max: opts.reconnectMaxMs ?? RECONNECT_MAX_MS,
      failures: opts.maxFailures ?? MAX_STREAM_FAILURES,
      poll: opts.pollMs ?? POLL_MS,
    };
  });

  useEffect(() => {
    if (!runId) return;
    const url = runEventsUrl(orgId, runId);
    let disposed = false;
    let view = INITIAL_RUN_VIEW;
    let conn: AbortController | null = null;
    let wake: (() => void) | null = null;
    let settled = false;

    const hidden = () => typeof document !== "undefined" && document.hidden;
    const publish = () => {
      if (!disposed) setState((s) => ({ key, view, connection: s.key === key ? s.connection : "connecting", cancelling: s.key === key ? s.cancelling : false }));
    };
    const setConn = (connection: Connection) => {
      if (!disposed) setState((s) => ({ key, view: s.key === key ? s.view : view, connection, cancelling: s.key === key ? s.cancelling : false }));
    };
    const finish = () => {
      if (settled || disposed) return;
      settled = true;
      onSettledRef.current?.({ runId, status: view.status });
    };
    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        const done = () => {
          clearTimeout(timer);
          if (wake === done) wake = null;
          resolve();
        };
        const timer = setTimeout(done, ms);
        wake = done;
      });
    const waitVisible = () =>
      new Promise<void>((resolve) => {
        wake = () => {
          wake = null;
          resolve();
        };
      });
    const onVisibility = () => {
      if (hidden()) conn?.abort();
      else wake?.();
    };

    const poll = async () => {
      setConn("polling");
      while (!disposed) {
        try {
          const info = await getRun(fetcher, orgId, runId);
          if (disposed) return;
          if (isTerminalState(info.state)) {
            const status: RunStatus = info.state === "completed" ? "completed" : info.state === "failed" ? "failed" : "cancelled";
            view = { ...view, status, error: status === "failed" ? { code: info.error_code ?? "internal_error", message: "" } : null };
            publish();
            setConn("idle");
            finish();
            return;
          }
        } catch {
          // transient: keep polling
        }
        await sleep(tuning.current.poll);
      }
    };

    const loop = async () => {
      let failures = 0;
      while (!disposed) {
        if (hidden()) {
          await waitVisible();
          continue;
        }
        const ctl = new AbortController();
        conn = ctl;
        let end: StreamEnd["reason"] | null = null;
        let fatal = false;
        setConn(failures > 0 ? "reconnecting" : "connecting");
        try {
          for await (const frame of stream(url, { lastEventId: view.lastSeq > 0 ? view.lastSeq : null, signal: ctl.signal })) {
            if (disposed) return;
            failures = 0;
            setConn("live");
            const parsed = parseRunEvent(frame.event, frame.data);
            if (!parsed) continue; // unknown event types and malformed frames are ignored
            if (parsed.type === "end") {
              end = parsed.reason;
              break;
            }
            const seq = frame.id !== null && /^\d{1,15}$/.test(frame.id) ? Number(frame.id) : null;
            view = applyRunEvent(view, seq, parsed);
            publish();
          }
        } catch (err) {
          if (disposed) return;
          // 401/403/404: retrying cannot help (signed out, or the run is not yours)
          if (err instanceof StreamHttpError && (err.status === 401 || err.status === 403 || err.status === 404)) fatal = true;
        }
        if (disposed) return;
        if (fatal) {
          setConn("stopped");
          finish();
          return;
        }
        if (isTerminalStatus(view.status) || end === "terminal") {
          setConn("idle");
          finish();
          return;
        }
        if (end === "unavailable") {
          setConn("stopped");
          finish();
          return;
        }
        if (end === "timeout") {
          failures = 0;
          continue; // reconnect at once, from the last seq
        }
        if (ctl.signal.aborted) continue; // closed because the tab was hidden: the next turn waits for it to be shown
        failures += 1;
        if (failures >= tuning.current.failures) {
          await poll();
          return;
        }
        setConn("reconnecting");
        await sleep(Math.min(tuning.current.base * 2 ** (failures - 1), tuning.current.max));
      }
    };

    document.addEventListener("visibilitychange", onVisibility);
    void loop();
    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisibility);
      conn?.abort();
      wake?.();
    };
  }, [key, orgId, runId, stream, fetcher]);

  const cancel = useCallback(async (): Promise<boolean> => {
    if (!runId) return false;
    setState((s) => (s.key === key ? { ...s, cancelling: true } : s));
    try {
      await cancelRun(fetcher, orgId, runId);
      return true;
    } catch {
      setState((s) => (s.key === key ? { ...s, cancelling: false } : s));
      return false;
    }
  }, [fetcher, orgId, runId, key]);

  if (!runId) return { view: null, connection: "idle", cancelling: false, cancel };
  // A state for another thread/run is never shown: derive the initial state until the effect replaces it.
  const current: State = state.key === key ? state : { key, view: INITIAL_RUN_VIEW, connection: "connecting", cancelling: false };
  return { view: current.view, connection: current.connection, cancelling: current.cancelling && !isTerminalStatus(current.view.status), cancel };
}
