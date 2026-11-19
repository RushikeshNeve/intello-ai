/**
 * A small `text/event-stream` client on top of `fetch`. Native `EventSource` cannot set `Last-Event-ID` on a new
 * connection, and the chat run stream must resume from an exact sequence number, so the stream is read with
 * `fetch` (the BFF forwards the `last-event-id` request header). No dependencies.
 */
export type SseFrame = { id: string | null; event: string; data: string };

/** Incremental parser (WHATWG "interpreting an event stream"): LF, CRLF and CR line ends; comments ignored. */
export class SseParser {
  private buffer = "";
  private id: string | null = null;
  private event = "";
  private data: string[] = [];
  private hasData = false;

  push(chunk: string): SseFrame[] {
    let text = this.buffer + chunk;
    let tail = "";
    // A trailing CR may be the first half of CRLF: keep it until the next chunk decides.
    if (text.endsWith("\r")) {
      tail = "\r";
      text = text.slice(0, -1);
    }
    const lines = text.replace(/\r\n?/g, "\n").split("\n");
    this.buffer = (lines.pop() ?? "") + tail;
    const out: SseFrame[] = [];
    for (const line of lines) {
      const frame = this.line(line);
      if (frame) out.push(frame);
    }
    return out;
  }

  private line(line: string): SseFrame | null {
    if (line === "") {
      if (!this.hasData && this.event === "") return null; // an empty block carries nothing
      const frame: SseFrame = { id: this.id, event: this.event || "message", data: this.data.join("\n") };
      this.event = "";
      this.data = [];
      this.hasData = false;
      return frame;
    }
    if (line.startsWith(":")) return null; // keep-alive / comment
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") {
      this.data.push(value);
      this.hasData = true;
    } else if (field === "id" && !value.includes("\0")) this.id = value;
    // `retry:` and unknown fields are ignored: the caller owns the back-off policy
    return null;
  }
}

export class StreamHttpError extends Error {
  constructor(readonly status: number) {
    super(`stream HTTP ${status}`);
    this.name = "StreamHttpError";
  }
}

export type RunStream = (url: string, opts: { lastEventId: number | null; signal: AbortSignal }) => AsyncIterable<SseFrame>;

/** The default transport: `fetch` + a streaming body reader. Throws `StreamHttpError` for a non-2xx answer. */
export const fetchStream: RunStream = async function* (url, { lastEventId, signal }) {
  const headers: Record<string, string> = { accept: "text/event-stream" };
  if (lastEventId !== null) headers["last-event-id"] = String(lastEventId);
  const res = await fetch(url, { headers, signal, credentials: "same-origin", cache: "no-store" });
  if (!res.ok) {
    await res.body?.cancel().catch(() => undefined);
    throw new StreamHttpError(res.status);
  }
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = new SseParser();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) yield frame;
    }
    for (const frame of parser.push(decoder.decode())) yield frame;
  } finally {
    await reader.cancel().catch(() => undefined);
  }
};
