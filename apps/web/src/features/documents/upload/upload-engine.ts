/**
 * Framework-free upload state machine (contract m02 §5 + §7).
 *
 *   queued -> hashing -> creating-session -> uploading(progress) -> finalizing -> done
 *   any state -> failed(code) | cancelled
 *
 * Retry rules (§7):
 *  - PUT network error / timeout / 5xx: retry the SAME session, up to PUT_RETRIES times, exponential backoff.
 *  - PUT 4xx, finalize `upload_expired` / `checksum_mismatch` / `size_mismatch`: NEW session, at most MAX_RESTARTS.
 *  - finalize `object_missing`: re-PUT the same session, then finalize again (bounded).
 *  - finalize 200 and 201 are both success.
 *  - `file_too_large`, `quota_exceeded`, `forbidden` (and other non-recoverable API errors) are terminal.
 */
import {
  toUploadErrorCode,
  type FinalizeResult,
  type UploadErrorCode,
  type UploadSession,
  type UploadsApi,
} from "./api";
import { DEFAULT_MAX_BYTES, FileTooLargeError, sha256Hex } from "./hash";
import { browserXhr, putFile, type XhrFactory } from "./put";

export const PUT_RETRIES = 3;
export const MAX_RESTARTS = 2;
export const OBJECT_MISSING_RETRIES = 2;
export const FINALIZE_RETRIES = 3;
export const DEFAULT_CONCURRENCY = 3;
export const DEFAULT_BACKOFF_MS = [500, 1000, 2000] as const;

export type UploadState =
  | "queued"
  | "hashing"
  | "creating-session"
  | "uploading"
  | "finalizing"
  | "done"
  | "failed"
  | "cancelled";

export type UploadContext = { orgId: string; workspaceId: string; documentId?: string };

export type UploadItem = {
  readonly id: string;
  readonly file: File;
  readonly context: UploadContext;
  readonly state: UploadState;
  /** 0..1, meaningful while `uploading`. */
  readonly progress: number;
  readonly error: UploadErrorCode | null;
  readonly result: FinalizeResult | null;
  /** The limit in force (server `limits.max_bytes` once known). */
  readonly maxBytes: number;
};

export const isActive = (s: UploadState) => s === "queued" || s === "hashing" || s === "creating-session" || s === "uploading" || s === "finalizing";

export type EngineOptions = {
  api: UploadsApi;
  hash?: (file: File, limit: number) => Promise<string>;
  xhrFactory?: XhrFactory;
  concurrency?: number;
  /** Client-side limit before the server has told us its own. */
  maxBytes?: number;
  /** Delay (ms) before PUT/finalize retry number n (1-based) = `backoffMs[n-1]` (last value repeats). */
  backoffMs?: readonly number[];
  /** Injectable timer; must reject-or-resolve promptly when `signal` aborts. */
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  onDone?: (result: FinalizeResult, item: UploadItem) => void;
};

class Failure extends Error {
  constructor(readonly code: UploadErrorCode) {
    super(code);
  }
}
class Aborted extends Error {}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const t = setTimeout(done, ms);
    function done() {
      clearTimeout(t);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done, { once: true });
  });
}

type Internal = { item: UploadItem; abort: AbortController; sha256?: string };

export class UploadEngine {
  private readonly opts: Required<Omit<EngineOptions, "onDone">> & Pick<EngineOptions, "onDone">;
  private entries: Internal[] = [];
  private listeners = new Set<() => void>();
  private snapshot: readonly UploadItem[] = [];
  private running = 0;
  private seq = 0;

  constructor(opts: EngineOptions) {
    this.opts = {
      hash: sha256Hex,
      xhrFactory: browserXhr,
      concurrency: DEFAULT_CONCURRENCY,
      maxBytes: DEFAULT_MAX_BYTES,
      backoffMs: DEFAULT_BACKOFF_MS,
      sleep: defaultSleep,
      ...opts,
    };
  }

  /** Replaces the completion callback (the React hook keeps it current). */
  setOnDone(fn: EngineOptions["onDone"]) {
    this.opts.onDone = fn;
  }

  // --- store (useSyncExternalStore-compatible) ---
  subscribe = (l: () => void) => {
    this.listeners.add(l);
    return () => void this.listeners.delete(l);
  };
  getSnapshot = (): readonly UploadItem[] => this.snapshot;

  private emit() {
    this.snapshot = this.entries.map((e) => e.item);
    for (const l of [...this.listeners]) l();
  }
  private patch(e: Internal, p: Partial<UploadItem>) {
    e.item = { ...e.item, ...p };
    this.emit();
  }

  // --- commands ---
  /**
   * Queues files. A File object that is already in the list (and not removed) is ignored, so an accidental
   * double drop does not upload twice. Returns the ids of the newly queued items.
   */
  add(files: readonly File[], context: UploadContext, maxBytes?: number): string[] {
    const ids: string[] = [];
    for (const file of files) {
      if (this.entries.some((e) => e.item.file === file)) continue;
      const id = `u${++this.seq}`;
      const item: UploadItem = {
        id,
        file,
        context,
        state: "queued",
        progress: 0,
        error: null,
        result: null,
        maxBytes: maxBytes ?? this.opts.maxBytes,
      };
      this.entries.push({ item, abort: new AbortController() });
      ids.push(id);
    }
    if (ids.length) {
      this.emit();
      this.pump();
    }
    return ids;
  }

  /** Re-runs a failed/cancelled item from the start (the hash is reused). */
  retry(id: string) {
    const e = this.find(id);
    if (!e || (e.item.state !== "failed" && e.item.state !== "cancelled")) return;
    e.abort = new AbortController();
    this.patch(e, { state: "queued", progress: 0, error: null });
    this.pump();
  }

  /** Aborts an in-flight PUT / pending retries; the item ends `cancelled` and nothing further is sent. */
  cancel(id: string) {
    const e = this.find(id);
    if (!e || !isActive(e.item.state)) return;
    e.abort.abort();
    // A queued item never started its runner, so it must be settled here; a running one settles itself.
    if (e.item.state === "queued") this.patch(e, { state: "cancelled" });
  }

  /** Cancels if needed and drops the item from the list (its File may then be added again). */
  remove(id: string) {
    const e = this.find(id);
    if (!e) return;
    if (isActive(e.item.state)) e.abort.abort();
    this.entries = this.entries.filter((x) => x !== e);
    this.emit();
  }

  /** Drops every finished (done/cancelled) item. */
  clearFinished() {
    this.entries = this.entries.filter((e) => e.item.state !== "done" && e.item.state !== "cancelled");
    this.emit();
  }

  private find(id: string) {
    return this.entries.find((e) => e.item.id === id);
  }

  // --- scheduling ---
  private pump() {
    for (const e of this.entries) {
      if (this.running >= this.opts.concurrency) return;
      if (e.item.state !== "queued" || e.abort.signal.aborted) continue;
      this.running++;
      void this.run(e).finally(() => {
        this.running--;
        this.pump();
      });
    }
  }

  private async run(e: Internal) {
    try {
      const result = await this.flow(e);
      this.patch(e, { state: "done", progress: 1, error: null, result });
      this.opts.onDone?.(result, e.item);
    } catch (err) {
      if (err instanceof Aborted || e.abort.signal.aborted) this.patch(e, { state: "cancelled" });
      else this.patch(e, { state: "failed", error: err instanceof Failure ? err.code : toUploadErrorCode(err) });
    }
  }

  private check(e: Internal) {
    if (e.abort.signal.aborted) throw new Aborted();
  }

  private async backoff(e: Internal, attempt: number) {
    const d = this.opts.backoffMs;
    await this.opts.sleep(d[Math.min(attempt, d.length) - 1] ?? 0, e.abort.signal);
    this.check(e);
  }

  /** Runs one item to completion; throws `Failure` / `Aborted`. */
  private async flow(e: Internal): Promise<FinalizeResult> {
    const { file, context } = e.item;
    let limit = e.item.maxBytes;
    if (file.size > limit) throw new Failure("file_too_large"); // refused BEFORE hashing

    if (!e.sha256) {
      this.patch(e, { state: "hashing", progress: 0 });
      try {
        e.sha256 = await this.opts.hash(file, limit);
      } catch (err) {
        this.check(e);
        throw err instanceof FileTooLargeError ? new Failure("file_too_large") : new Failure("unknown");
      }
      this.check(e);
    }
    const sha256 = e.sha256;

    let restarts = 0;
    // Starts a NEW session (or gives up with the code that caused the restart).
    const restart = (code: UploadErrorCode) => {
      if (++restarts > MAX_RESTARTS) throw new Failure(code);
    };

    for (;;) {
      this.patch(e, { state: "creating-session", progress: 0 });
      let session: UploadSession;
      try {
        session = await this.opts.api.createUpload(context.orgId, context.workspaceId, {
          filename: file.name,
          content_type: file.type || "application/octet-stream",
          size_bytes: file.size,
          sha256,
          ...(context.documentId ? { document_id: context.documentId } : {}),
        });
      } catch (err) {
        this.check(e);
        throw new Failure(toUploadErrorCode(err));
      }
      this.check(e);
      limit = session.limits.max_bytes;
      this.patch(e, { maxBytes: limit });
      if (file.size > limit) throw new Failure("file_too_large");

      let putRetries = 0;
      let missingRetries = 0;

      sameSession: for (;;) {
        // --- PUT (same session on network error / 5xx) ---
        for (;;) {
          this.patch(e, { state: "uploading", progress: 0 });
          const out = await putFile(
            this.opts.xhrFactory,
            file,
            session.upload,
            (p) => this.patch(e, { progress: p }),
            e.abort.signal,
          );
          if (out.kind === "aborted") throw new Aborted();
          if (out.kind === "ok") break;
          if (out.kind === "retryable") {
            if (++putRetries > PUT_RETRIES) throw new Failure("network_error");            await this.backoff(e, putRetries);
            continue;
          }
          restart("upload_rejected"); // 4xx from the store: the session is no good
          break sameSession;
        }

        // --- finalize ---
        let finalizeRetries = 0;
        for (;;) {
          this.patch(e, { state: "finalizing", progress: 1 });
          try {
            return await this.opts.api.finalizeUpload(context.orgId, context.workspaceId, session.upload_id);
          } catch (err) {
            this.check(e);
            const code = toUploadErrorCode(err);
            if (code === "object_missing") {
              if (++missingRetries > OBJECT_MISSING_RETRIES) throw new Failure(code);
              continue sameSession; // re-PUT the same session, then finalize again
            }
            if (code === "upload_expired" || code === "checksum_mismatch" || code === "size_mismatch") {
              restart(code);
              break sameSession;
            }
            if (code === "network_error" || code === "storage_unavailable") {
              // finalize is idempotent (200 on repeat): safe to repeat
              if (++finalizeRetries > FINALIZE_RETRIES) throw new Failure(code);
              await this.backoff(e, finalizeRetries);
              continue;
            }
            throw new Failure(code);
          }
        }
      }
    }
  }
}
