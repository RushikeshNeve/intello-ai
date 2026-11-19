import type { Citation, ChatMessage, ThreadDetail } from "../api";
import type { RunStream, SseFrame } from "../sse";

export const ORG = "o1";

export function cit(n: number, over: Partial<Citation> = {}): Citation {
  return {
    marker: `[${n}]`,
    chunk_id: `c-${n}`,
    document_id: `d-${n}`,
    version_id: `v-${n}`,
    workspace_id: "w1",
    title: `Doc ${n}`,
    version_number: 2,
    page_start: 3,
    page_end: 3,
    excerpt: `excerpt ${n}`,
    ...over,
  };
}

/** Wire form of a citation (what the API sends). */
export const wireCit = (n: number, over: Record<string, unknown> = {}) => ({ ...cit(n), ...over });

export const frame = (seq: number | null, event: string, data: unknown = {}): SseFrame => ({
  id: seq === null ? null : String(seq),
  event,
  data: JSON.stringify(data),
});
export const END = (reason: "terminal" | "timeout" | "unavailable" = "terminal") => frame(null, "end", { reason });

export type Conn = { frames: SseFrame[]; then?: "close" | "error" | "hang"; httpStatus?: number };

/** A scripted SSE transport: connection N plays `conns[N]`; every connection's `Last-Event-ID` is recorded. */
export function scripted(conns: Conn[]) {
  const calls: { url: string; lastEventId: number | null; aborted: boolean }[] = [];
  const stream: RunStream = async function* (url, { lastEventId, signal }) {
    const call = { url, lastEventId, aborted: false };
    calls.push(call);
    signal.addEventListener("abort", () => (call.aborted = true), { once: true });
    const conn = conns[calls.length - 1] ?? { frames: [], then: "hang" };
    if (conn.httpStatus) {
      const { StreamHttpError } = await import("../sse");
      throw new StreamHttpError(conn.httpStatus);
    }
    for (const f of conn.frames) yield f;
    if (conn.then === "error") throw new Error("connection lost");
    if (conn.then === "hang") {
      await new Promise<void>((_, reject) => signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true }));
    }
  };
  return { stream, calls };
}

export function msg(over: Partial<ChatMessage> & Pick<ChatMessage, "id" | "role">): ChatMessage {
  return { content: "", citations: [], verification: null, run_id: null, created_at: "2026-11-19T10:00:00Z", ...over };
}

export function detail(over: Partial<ThreadDetail> = {}, threadId = "t-1"): ThreadDetail {
  return {
    thread: { id: threadId, workspace_id: null, title: "Quarterly questions", created_at: "2026-11-19T09:00:00Z", updated_at: "2026-11-19T10:00:00Z" },
    messages: [],
    active_run: null,
    ...over,
  };
}
