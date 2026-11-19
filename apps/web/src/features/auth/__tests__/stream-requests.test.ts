import { describe, expect, it } from "vitest";
import { buildUpstreamHeaders, isStreamRequest } from "../bff";

describe("BFF stream detection", () => {
  it("treats Accept: text/event-stream as a stream", () => {
    expect(isStreamRequest(["orgs", "o", "workspaces", "w", "documents", "d", "ingestion", "events"], "text/event-stream")).toBe(true);
  });

  it("treats the chat run events path as a stream even without the Accept header", () => {
    expect(isStreamRequest(["orgs", "o1", "chat", "runs", "r1", "events"], null)).toBe(true);
    expect(isStreamRequest(["orgs", "o1", "chat", "runs", "r1", "events"], "application/json")).toBe(true);
  });

  it("does not stretch the timeout for other chat or events-like paths", () => {
    expect(isStreamRequest(["orgs", "o1", "chat", "runs", "r1"], null)).toBe(false);
    expect(isStreamRequest(["orgs", "o1", "chat", "threads", "t1", "events"], null)).toBe(false);
    expect(isStreamRequest(["orgs", "o1", "search"], "application/json")).toBe(false);
  });

  it("forwards Last-Event-ID (and only allow-listed headers) upstream", () => {
    const h = new Headers({ "last-event-id": "42", cookie: "x=1", authorization: "Bearer nope", accept: "text/event-stream" });
    const out = buildUpstreamHeaders(h, "tok");
    expect(out.get("last-event-id")).toBe("42");
    expect(out.get("cookie")).toBeNull();
    expect(out.get("authorization")).toBe("Bearer tok");
  });
});
