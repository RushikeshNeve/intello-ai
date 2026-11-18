import type { UploadTarget } from "./api";

export type PutOutcome =
  | { kind: "ok" }
  | { kind: "retryable" } // network error, timeout or 5xx: retry the SAME session
  | { kind: "rejected"; status: number } // 4xx (or any other non-2xx) from the store: needs a NEW session
  | { kind: "aborted" };

export type XhrLike = {
  open(method: string, url: string): void;
  setRequestHeader(name: string, value: string): void;
  send(body: Blob): void;
  abort(): void;
  withCredentials: boolean;
  status: number;
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  ontimeout: (() => void) | null;
  onabort: (() => void) | null;
};

export type XhrFactory = () => XhrLike;
export const browserXhr: XhrFactory = () => new XMLHttpRequest() as unknown as XhrLike;

/**
 * PUTs the file straight to the object store with EXACTLY `upload.headers` (verbatim: the presigned
 * signature covers them) and no credentials. This never goes through the BFF.
 */
export function putFile(
  createXhr: XhrFactory,
  file: Blob,
  target: UploadTarget,
  onProgress: (fraction: number) => void,
  signal: AbortSignal,
): Promise<PutOutcome> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve({ kind: "aborted" });
    const xhr = createXhr();
    const onAbort = () => xhr.abort();
    const done = (o: PutOutcome) => {
      signal.removeEventListener("abort", onAbort);
      resolve(o);
    };
    xhr.open(target.method, target.url);
    xhr.withCredentials = false; // never send cookies/credentials to the store
    for (const [name, value] of Object.entries(target.headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) onProgress(Math.min(1, e.loaded / e.total));
    };
    xhr.onload = () => {
      const s = xhr.status;
      if (s >= 200 && s < 300) done({ kind: "ok" });
      else if (s >= 500) done({ kind: "retryable" });
      else done({ kind: "rejected", status: s });
    };
    xhr.onerror = () => done({ kind: "retryable" });
    xhr.ontimeout = () => done({ kind: "retryable" });
    xhr.onabort = () => done({ kind: "aborted" });
    signal.addEventListener("abort", onAbort, { once: true });
    xhr.send(file);
  });
}
