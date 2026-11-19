"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useApiFetcher } from "@/lib/query/provider";
import { badgeText } from "./messages";
import {
  getIngestion,
  ingestionEventsPath,
  isTerminal,
  parseStreamFrame,
  type IngestionEvent,
  type IngestionStatus,
  type IngestionSummary,
} from "./api";

/** The slice of the DOM `EventSource` the hook uses, so tests can inject a fake. */
export type EventSourceLike = {
  addEventListener(type: string, listener: (e: MessageEvent<string>) => void): void;
  close(): void;
  onopen: ((e: Event) => void) | null;
  onerror: ((e: Event) => void) | null;
};
export type EventSourceFactory = (url: string) => EventSourceLike;

export const POLL_MS = 5_000;
export const RECONNECT_BASE_MS = 1_000;
export const RECONNECT_MAX_MS = 30_000;
/** Consecutive stream failures before the hook gives up on SSE and polls instead. */
export const MAX_SSE_FAILURES = 4;
const MAX_EVENTS = 100;

/** `stopped`: the server said the stream is unavailable; we stop calmly instead of hammering it. */
export type Connection = "idle" | "connecting" | "live" | "polling" | "stopped";

export type IngestionLive = {
  summary: IngestionSummary | null;
  events: IngestionEvent[];
  connection: Connection;
  /** Re-reads the status now (e.g. after a retry) and restarts the stream if the job became active. */
  refresh: () => Promise<void>;
};

type State = { key: string; summary: IngestionSummary | null; events: IngestionEvent[]; connection: Connection };

const defaultFactory: EventSourceFactory | null =
  typeof EventSource === "undefined" ? null : (url) => new EventSource(url, { withCredentials: false }) as unknown as EventSourceLike;

export type UseIngestionStatusOptions = {
  orgId: string;
  workspaceId: string;
  documentId: string;
  /** Latest version id: a new version is a new key, so nothing carries over from the previous one. */
  versionRef: string | null;
  /** Server-rendered summary shown before the first live update. */
  initial: IngestionSummary | null;
  eventSource?: EventSourceFactory | null;
  pollMs?: number;
};

function mergeEvents(prev: IngestionEvent[], incoming: IngestionEvent[]): IngestionEvent[] {
  const out = [...prev];
  for (const e of incoming) {
    const dup = out.some((p) => (e.seq !== null && p.seq === e.seq) || (e.seq === null && p.seq === null && p.label === e.label && p.at === e.at));
    if (!dup) out.push(e);
  }
  return out.slice(-MAX_EVENTS);
}

/**
 * Live ingestion state of one document version.
 *  - Opens `GET …/ingestion/events` (SSE, same-origin BFF); the first `snapshot` is authoritative, `job` events
 *    update it. A failed stream is retried with exponential back-off (capped); after MAX_SSE_FAILURES in a row,
 *    or without EventSource, it polls `GET …/ingestion` every pollMs. The browser's own `Last-Event-ID` replay is
 *    not relied on: every (re)connect starts with a fresh snapshot from PostgreSQL, so nothing can be missed.
 *  - Stops at a terminal state, pauses while the tab is hidden (stream closed, timers cleared; one refresh on
 *    return), and closes everything on unmount or when the document/version changes.
 */
export function useIngestionStatus(opts: UseIngestionStatusOptions): IngestionLive {
  const { orgId, workspaceId, documentId, versionRef, initial } = opts;
  const fetcher = useApiFetcher();
  const key = `${orgId}/${workspaceId}/${documentId}/${versionRef ?? ""}`;
  const factory = opts.eventSource === undefined ? defaultFactory : opts.eventSource;
  const pollMs = opts.pollMs ?? POLL_MS;

  const [state, setState] = useState<State>({ key, summary: initial, events: [], connection: "idle" });
  // Never show another document's data: a state for a different key is ignored until the effect replaces it.
  const current: State = state.key === key ? state : { key, summary: initial, events: [], connection: "idle" };
  const active = current.summary !== null && !isTerminal(current.summary.state);
  const [nonce, setNonce] = useState(0);
  const keyRef = useRef(key);
  useEffect(() => {
    keyRef.current = key;
  });

  const apply = useCallback((forKey: string, summary: IngestionSummary, status: IngestionStatus | null, event: IngestionEvent | null) => {
    setState((prev) => {
      const base: State = prev.key === forKey ? prev : { key: forKey, summary: null, events: [], connection: "idle" };
      let events = base.events;
      if (status && status.events.length > 0) events = mergeEvents(events, status.events);
      else if (event) events = mergeEvents(events, [event]);
      else if (events.length === 0) {
        events = [{ seq: null, type: "state", label: badgeText(summary), stage: summary.stage, attempt: null, at: summary.updated_at }];
      }
      return { ...base, summary, events };
    });
  }, []);

  const setConnection = useCallback((forKey: string, connection: Connection) => {
    setState((prev) => (prev.key === forKey ? { ...prev, connection } : prev));
  }, []);

  const refresh = useCallback(async () => {
    const forKey = keyRef.current;
    try {
      const status = await getIngestion(fetcher, orgId, workspaceId, documentId);
      if (keyRef.current !== forKey) return;
      apply(forKey, status, status, null);
      setNonce((n) => n + 1);
    } catch {
      // keep what we have; the stream or the poll will try again
    }
  }, [fetcher, orgId, workspaceId, documentId, apply]);

  useEffect(() => {
    if (!active) return;
    let disposed = false;
    let es: EventSourceLike | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;
    let polling = false;
    const url = `/api${ingestionEventsPath(orgId, workspaceId, documentId)}`;

    const clear = () => {
      if (timer) clearTimeout(timer);
      timer = null;
      es?.close();
      es = null;
    };

    let reopened = false; // a `timeout` end may be reopened once per snapshot

    const onFrame = (type: "snapshot" | "job" | "end") => (e: MessageEvent<string>) => {
      if (disposed) return;
      const frame = parseStreamFrame(type, e.data);
      if (!frame) return;
      if (frame.kind === "end") {
        // The server closes after `end`: close ours too, or EventSource would reconnect on its own.
        es?.close();
        es = null;
        if (frame.reason === "unavailable") return setConnection(key, "stopped");
        if (frame.reason === "timeout" && !reopened) {
          reopened = true;
          return connect();
        }
        void fetchOnce(); // terminal (or a repeated timeout): read the persisted state once
        return;
      }
      if (frame.kind === "snapshot") reopened = false;
      apply(key, frame.summary, null, frame.kind === "job" ? frame.event : null);
    };

    /** One status read; returns true while the job is still active. */
    const fetchOnce = async (): Promise<boolean> => {
      try {
        const status = await getIngestion(fetcher, orgId, workspaceId, documentId);
        if (disposed) return false;
        apply(key, status, status, null);
        return !isTerminal(status.state); // a terminal state re-runs this effect, which then stays idle
      } catch {
        return true; // transient: keep going
      }
    };

    const poll = async () => {
      timer = null;
      if (disposed) return;
      if ((await fetchOnce()) && !disposed) timer = setTimeout(() => void poll(), pollMs);
    };

    const startPolling = () => {
      polling = true;
      setConnection(key, "polling");
      timer = setTimeout(() => void poll(), pollMs);
    };

    const connect = () => {
      if (disposed || !factory) return startPolling();
      setConnection(key, "connecting");
      let source: EventSourceLike;
      try {
        source = factory(url);
      } catch {
        return startPolling();
      }
      es = source;
      source.onopen = () => {
        failures = 0;
        if (!disposed) setConnection(key, "live");
      };
      source.addEventListener("snapshot", onFrame("snapshot"));
      source.addEventListener("job", onFrame("job"));
      source.addEventListener("end", onFrame("end"));
      source.onerror = () => {
        if (disposed || es !== source) return;
        source.close();
        es = null;
        failures += 1;
        if (failures >= MAX_SSE_FAILURES) return startPolling();
        setConnection(key, "connecting");
        const delay = Math.min(RECONNECT_BASE_MS * 2 ** (failures - 1), RECONNECT_MAX_MS);
        timer = setTimeout(connect, delay);
      };
    };

    const start = () => {
      if (polling) startPolling();
      else connect();
    };

    const onVisibility = () => {
      if (disposed) return;
      clear();
      if (document.hidden) return;
      void fetchOnce(); // catch up on whatever happened while hidden
      start();
    };

    if (!document.hidden) start(); // started in a hidden tab: wait until it is shown
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisibility);
      clear();
    };
    // `nonce` restarts the stream after refresh(); `active` flips it off at a terminal state.
  }, [key, active, nonce, factory, pollMs, orgId, workspaceId, documentId, fetcher, apply, setConnection]);

  return { summary: current.summary, events: current.events, connection: active ? current.connection : "idle", refresh };
}
