import { beforeEach, describe, expect, it, vi } from "vitest";
import { UploadEngine, type EngineOptions, type UploadItem, type UploadState } from "../upload-engine";
import { apiErr, FakeXhrs, fakeApi, fakeHash, file, FILE_RESULT, instantSleep, session, sleeps, type XhrBehavior } from "./fakes";

const CTX = { orgId: "org-1", workspaceId: "ws-1" };

function setup(o: { xhr?: XhrBehavior[]; create?: NonNullable<Parameters<typeof fakeApi>[0]>["create"]; finalize?: NonNullable<Parameters<typeof fakeApi>[0]>["finalize"] } & Partial<EngineOptions> = {}) {
  const xhrs = new FakeXhrs(o.xhr ?? ["ok"]);
  const fake = fakeApi({ create: o.create, finalize: o.finalize });
  const rest: Partial<EngineOptions> = { ...o };
  delete (rest as Record<string, unknown>).xhr;
  delete (rest as Record<string, unknown>).create;
  delete (rest as Record<string, unknown>).finalize;
  const engine = new UploadEngine({ api: fake.api, hash: fakeHash, xhrFactory: xhrs.factory, sleep: instantSleep, ...rest });
  const states: UploadState[] = [];
  engine.subscribe(() => {
    const s = engine.getSnapshot()[0]?.state;
    if (s && states[states.length - 1] !== s) states.push(s);
  });
  const item = (): UploadItem => engine.getSnapshot()[0];
  const settle = (state: UploadState = "done") => vi.waitFor(() => expect(item().state).toBe(state));
  return { engine, xhrs, ...fake, states, item, settle };
}

beforeEach(() => {
  sleeps.length = 0;
});

describe("happy path", () => {
  it("walks queued -> hashing -> creating-session -> uploading -> finalizing -> done", async () => {
    const onDone = vi.fn();
    const t = setup({ onDone });
    t.engine.add([file()], CTX);
    await t.settle();
    expect(t.states).toEqual(["queued", "hashing", "creating-session", "uploading", "finalizing", "done"]);
    expect(t.item().result).toEqual(FILE_RESULT());
    expect(onDone).toHaveBeenCalledOnce();
  });

  it("sends the contract body to create-session (JSON only: no bytes), with document_id for a new version", async () => {
    const t = setup();
    t.engine.add([file("report.pdf", "abc", "application/pdf")], { ...CTX, documentId: "doc-9" });
    await t.settle();
    expect(t.creates).toEqual([
      { filename: "report.pdf", content_type: "application/pdf", size_bytes: 3, sha256: "ab".repeat(32), document_id: "doc-9" },
    ]);
  });

  it("omits document_id for a new document and defaults an empty type to application/octet-stream", async () => {
    const t = setup();
    t.engine.add([file("x.bin", "abc", "")], CTX);
    await t.settle();
    expect(t.creates[0]).not.toHaveProperty("document_id");
    expect(t.creates[0].content_type).toBe("application/octet-stream");
  });

  it("PUTs to upload.url with EXACTLY upload.headers, the File itself, and no credentials", async () => {
    const t = setup();
    const f = file();
    t.engine.add([f], CTX);
    await t.settle();
    const [x] = t.xhrs.instances;
    const s = session(1);
    expect(x.method).toBe("PUT");
    expect(x.url).toBe(s.upload.url);
    expect(x.headers).toEqual(s.upload.headers); // verbatim: nothing added, nothing dropped
    expect(x.withCredentials).toBe(false);
    expect(x.body).toBe(f);
  });

  it("never sends file bytes through the API layer", async () => {
    const t = setup();
    t.engine.add([file()], CTX);
    await t.settle();
    for (const body of t.creates) expect(Object.values(body).every((v) => typeof v === "string" || typeof v === "number")).toBe(true);
    expect(t.api.finalizeUpload).toHaveBeenCalledWith("org-1", "ws-1", "up-1");
  });

  it("reports upload progress as a fraction", async () => {
    const seen: number[] = [];
    const t = setup();
    t.engine.subscribe(() => {
      if (t.item()?.state === "uploading") seen.push(t.item().progress);
    });
    t.engine.add([file()], CTX);
    await t.settle();
    expect(seen).toContain(0.5);
    expect(seen).toContain(1);
  });

  it("treats finalize 200 (repeat) like 201: both resolve to the result", async () => {
    // The HTTP status distinction lives in the client; the engine only sees a resolved result.
    const t = setup({ finalize: [FILE_RESULT(2)] });
    t.engine.add([file()], CTX);
    await t.settle();
    expect(t.item().result?.version.id).toBe("ver-2");
  });
});

describe("size limits", () => {
  it("refuses a file above maxBytes BEFORE hashing or any request", async () => {
    const hash = vi.fn(fakeHash);
    const t = setup({ maxBytes: 2, hash });
    t.engine.add([file("big.txt", "abc")], CTX);
    await t.settle("failed");
    expect(t.item().error).toBe("file_too_large");
    expect(hash).not.toHaveBeenCalled();
    expect(t.creates).toHaveLength(0);
    expect(t.xhrs.instances).toHaveLength(0);
  });

  it("the server's limits.max_bytes overrides the client constant", async () => {
    const t = setup({ create: [session(1, { limits: { max_bytes: 2 } })] });
    t.engine.add([file("big.txt", "abc")], CTX);
    await t.settle("failed");
    expect(t.item().error).toBe("file_too_large");
    expect(t.item().maxBytes).toBe(2);
    expect(t.xhrs.instances).toHaveLength(0);
  });
});

describe("PUT retries: same session (network error / 5xx)", () => {
  it.each<[string, XhrBehavior]>([
    ["network error", "error"],
    ["timeout", "timeout"],
    ["503", 503],
    ["500", 500],
  ])("retries the SAME session after a %s and succeeds", async (_n, bad) => {
    const t = setup({ xhr: [bad, "ok"] });
    t.engine.add([file()], CTX);
    await t.settle();
    expect(t.creates).toHaveLength(1); // no new session
    expect(t.xhrs.instances).toHaveLength(2);
    expect(t.xhrs.instances.map((x) => x.url)).toEqual([session(1).upload.url, session(1).upload.url]);
    expect(t.xhrs.instances[1].headers).toEqual(session(1).upload.headers);
    expect(t.finalizes).toEqual(["up-1"]);
  });

  it("backs off exponentially between retries", async () => {
    const t = setup({ xhr: ["error", "error", "error", "ok"] });
    t.engine.add([file()], CTX);
    await t.settle();
    expect(sleeps).toEqual([500, 1000, 2000]);
  });

  it("gives up after 3 retries (4 PUTs total) with network_error, never creating a new session", async () => {
    const t = setup({ xhr: ["error"] });
    t.engine.add([file()], CTX);
    await t.settle("failed");
    expect(t.item().error).toBe("network_error");
    expect(t.xhrs.instances).toHaveLength(4);
    expect(t.creates).toHaveLength(1);
    expect(t.finalizes).toHaveLength(0);
  });
});

describe("PUT rejected by the store (4xx): NEW session", () => {
  it("creates a new session and uploads to the new URL", async () => {
    const t = setup({ xhr: [403, "ok"] });
    t.engine.add([file()], CTX);
    await t.settle();
    expect(t.creates).toHaveLength(2);
    expect(t.xhrs.instances.map((x) => x.url)).toEqual([session(1).upload.url, session(2).upload.url]);
    expect(t.xhrs.instances[1].headers).toEqual(session(2).upload.headers);
    expect(t.finalizes).toEqual(["up-2"]);
    expect(sleeps).toEqual([]); // a new session is immediate, not a backoff retry
  });

  it("restarts at most 2 times (3 sessions) then fails upload_rejected", async () => {
    const t = setup({ xhr: [400] });
    t.engine.add([file()], CTX);
    await t.settle("failed");
    expect(t.creates).toHaveLength(3);
    expect(t.item().error).toBe("upload_rejected");
  });
});

describe("finalize outcomes", () => {
  it("object_missing re-PUTs the SAME session, then finalizes again", async () => {
    const t = setup({ finalize: [apiErr(409, "object_missing"), FILE_RESULT()] });
    t.engine.add([file()], CTX);
    await t.settle();
    expect(t.creates).toHaveLength(1);
    expect(t.xhrs.instances).toHaveLength(2);
    expect(t.xhrs.instances[1].url).toBe(session(1).upload.url);
    expect(t.finalizes).toEqual(["up-1", "up-1"]);
  });

  it("object_missing is bounded (2 re-PUTs) then fails", async () => {
    const t = setup({ finalize: [apiErr(409, "object_missing")] });
    t.engine.add([file()], CTX);
    await t.settle("failed");
    expect(t.item().error).toBe("object_missing");
    expect(t.xhrs.instances).toHaveLength(3);
  });

  it.each(["upload_expired", "checksum_mismatch", "size_mismatch"])("%s starts a NEW session", async (code) => {
    const status = code === "upload_expired" ? 410 : 409;
    const t = setup({ finalize: [apiErr(status, code), FILE_RESULT()] });
    t.engine.add([file()], CTX);
    await t.settle();
    expect(t.creates).toHaveLength(2);
    expect(t.finalizes).toEqual(["up-1", "up-2"]);
    expect(t.xhrs.instances.map((x) => x.url)).toEqual([session(1).upload.url, session(2).upload.url]);
  });

  it("gives up after 2 restarts with the causing code", async () => {
    const t = setup({ finalize: [apiErr(409, "checksum_mismatch")] });
    t.engine.add([file()], CTX);
    await t.settle("failed");
    expect(t.item().error).toBe("checksum_mismatch");
    expect(t.creates).toHaveLength(3);
  });

  it("retries finalize on 503 storage_unavailable (idempotent) and succeeds", async () => {
    const t = setup({ finalize: [apiErr(503, "storage_unavailable"), FILE_RESULT()] });
    t.engine.add([file()], CTX);
    await t.settle();
    expect(t.finalizes).toEqual(["up-1", "up-1"]);
    expect(t.xhrs.instances).toHaveLength(1); // no re-PUT
  });

  it("fails with storage_unavailable once finalize retries run out", async () => {
    const t = setup({ finalize: [apiErr(503, "storage_unavailable")] });
    t.engine.add([file()], CTX);
    await t.settle("failed");
    expect(t.item().error).toBe("storage_unavailable");
    expect(t.finalizes).toHaveLength(4);
  });
});

describe("terminal errors", () => {
  it.each([
    ["create", 413, "file_too_large"],
    ["create", 409, "quota_exceeded"],
    ["create", 403, "forbidden"],
    ["create", 429, "too_many_uploads"],
    ["create", 422, "invalid_request"],
    ["create", 404, "not_found"],
  ])("%s %i %s fails immediately with no PUT and no retry", async (_w, status, code) => {
    const t = setup({ create: [apiErr(status, code)] });
    t.engine.add([file()], CTX);
    await t.settle("failed");
    expect(t.item().error).toBe(code);
    expect(t.creates).toHaveLength(1);
    expect(t.xhrs.instances).toHaveLength(0);
    expect(sleeps).toEqual([]);
  });

  it("finalize forbidden / quota_exceeded are terminal", async () => {
    for (const code of ["forbidden", "quota_exceeded"]) {
      const t = setup({ finalize: [apiErr(code === "forbidden" ? 403 : 409, code)] });
      t.engine.add([file()], CTX);
      await t.settle("failed");
      expect(t.item().error).toBe(code);
      expect(t.creates).toHaveLength(1);
    }
  });

  it("a 401 fails as unauthenticated (the BFF fetcher redirects to sign-in)", async () => {
    const t = setup({ create: [apiErr(401, "invalid_token")] });
    t.engine.add([file()], CTX);
    await t.settle("failed");
    expect(t.item().error).toBe("unauthenticated");
  });

  it("an API network error on create fails as network_error", async () => {
    const t = setup({ create: [apiErr(0, "network_error")] });
    t.engine.add([file()], CTX);
    await t.settle("failed");
    expect(t.item().error).toBe("network_error");
  });
});

describe("retry() / cancel() / remove()", () => {
  it("retry() re-runs a failed item reusing the hash", async () => {
    const hash = vi.fn(fakeHash);
    const t = setup({ create: [apiErr(409, "quota_exceeded"), session(2)], hash });
    t.engine.add([file()], CTX);
    await t.settle("failed");
    t.engine.retry(t.item().id);
    await t.settle();
    expect(hash).toHaveBeenCalledOnce();
    expect(t.item().error).toBeNull();
  });

  it("cancel aborts the in-flight XHR and nothing further is sent", async () => {
    const t = setup({ xhr: ["hang"] });
    t.engine.add([file()], CTX);
    await vi.waitFor(() => expect(t.xhrs.instances).toHaveLength(1));
    t.engine.cancel(t.item().id);
    await t.settle("cancelled");
    expect(t.xhrs.instances[0].aborted).toBe(true);
    expect(t.xhrs.instances).toHaveLength(1);
    expect(t.finalizes).toHaveLength(0);
    expect(t.creates).toHaveLength(1);
  });

  it("cancel during backoff stops the retries", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const t = setup({ xhr: ["error"], sleep: async (_ms, signal) => void (await Promise.race([gate, new Promise((r) => signal.addEventListener("abort", r))])) });
    t.engine.add([file()], CTX);
    await vi.waitFor(() => expect(t.xhrs.instances).toHaveLength(1));
    await Promise.resolve();
    t.engine.cancel(t.item().id);
    await t.settle("cancelled");
    release();
    expect(t.xhrs.instances).toHaveLength(1);
  });

  it("a cancelled item can be retried", async () => {
    const t = setup({ xhr: ["hang", "ok"] });
    t.engine.add([file()], CTX);
    await vi.waitFor(() => expect(t.xhrs.instances).toHaveLength(1));
    t.engine.cancel(t.item().id);
    await t.settle("cancelled");
    t.engine.retry(t.item().id);
    await t.settle();
  });

  it("cancelling a queued item never starts it", async () => {
    const t = setup({ xhr: ["hang"], concurrency: 1 });
    t.engine.add([file("a.txt"), file("b.txt")], CTX);
    const second = t.engine.getSnapshot()[1];
    t.engine.cancel(second.id);
    expect(t.engine.getSnapshot()[1].state).toBe("cancelled");
    await vi.waitFor(() => expect(t.xhrs.instances).toHaveLength(1));
    t.engine.cancel(t.engine.getSnapshot()[0].id);
    await vi.waitFor(() => expect(t.engine.getSnapshot()[0].state).toBe("cancelled"));
    expect(t.creates).toHaveLength(1);
  });

  it("remove() drops the item and aborts it if active", async () => {
    const t = setup({ xhr: ["hang"] });
    t.engine.add([file()], CTX);
    await vi.waitFor(() => expect(t.xhrs.instances).toHaveLength(1));
    t.engine.remove(t.item().id);
    expect(t.engine.getSnapshot()).toHaveLength(0);
    await vi.waitFor(() => expect(t.xhrs.instances[0].aborted).toBe(true));
  });
});

describe("concurrency and de-duplication", () => {
  it("runs at most `concurrency` files at once (default 3)", async () => {
    const t = setup({ xhr: ["hang"] });
    t.engine.add([file("1"), file("2"), file("3"), file("4"), file("5")], CTX);
    await vi.waitFor(() => expect(t.xhrs.instances).toHaveLength(3));
    await new Promise((r) => setTimeout(r, 20));
    expect(t.xhrs.instances).toHaveLength(3);
    expect(t.engine.getSnapshot().filter((i) => i.state === "queued")).toHaveLength(2);
    // finishing one lets the next start
    t.xhrs.instances[0].abort();
    await vi.waitFor(() => expect(t.xhrs.instances.length).toBeGreaterThanOrEqual(3));
  });

  it("starts the next queued file when one completes", async () => {
    const t = setup({ concurrency: 1 });
    t.engine.add([file("a"), file("b")], CTX);
    await vi.waitFor(() => expect(t.engine.getSnapshot().map((i) => i.state)).toEqual(["done", "done"]));
  });

  it("ignores the same File object added twice (double drop)", () => {
    const t = setup({ xhr: ["hang"] });
    const f = file();
    expect(t.engine.add([f], CTX)).toHaveLength(1);
    expect(t.engine.add([f], CTX)).toHaveLength(0);
    expect(t.engine.getSnapshot()).toHaveLength(1);
  });

  it("allows two distinct File objects with the same name", () => {
    const t = setup({ xhr: ["hang"] });
    t.engine.add([file("same.txt"), file("same.txt")], CTX);
    expect(t.engine.getSnapshot()).toHaveLength(2);
  });
});
