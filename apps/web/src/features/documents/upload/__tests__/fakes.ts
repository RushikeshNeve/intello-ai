import { vi } from "vitest";
import { ApiError } from "@/features/organizations/api/http";
import type { CreateUploadBody, FinalizeResult, UploadSession, UploadsApi } from "../api";
import type { XhrLike } from "../put";

export type XhrBehavior = "ok" | "error" | "timeout" | "hang" | number;

/** A scripted XMLHttpRequest stand-in. Behaviors are consumed in order (last one repeats). */
export class FakeXhrs {
  instances: FakeXhr[] = [];
  constructor(public script: XhrBehavior[] = ["ok"]) {}
  factory = (): XhrLike => {
    const behavior = this.script[Math.min(this.instances.length, this.script.length - 1)];
    const x = new FakeXhr(behavior);
    this.instances.push(x);
    return x;
  };
}

export class FakeXhr implements XhrLike {
  method = "";
  url = "";
  headers: Record<string, string> = {};
  body: Blob | null = null;
  withCredentials = true; // the real default; the code under test must turn it off
  status = 0;
  aborted = false;
  upload: XhrLike["upload"] = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  onabort: (() => void) | null = null;
  constructor(private behavior: XhrBehavior) {}
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(n: string, v: string) {
    this.headers[n] = v;
  }
  send(body: Blob) {
    this.body = body;
    void Promise.resolve().then(() => {
      if (this.behavior === "hang") return;
      this.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 });
      if (this.behavior === "error") return this.onerror?.();
      if (this.behavior === "timeout") return this.ontimeout?.();
      this.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 });
      this.status = this.behavior === "ok" ? 200 : this.behavior;
      this.onload?.();
    });
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
}

export const apiErr = (status: number, code: string) => new ApiError(status, code);

export const FILE_RESULT = (n = 1): FinalizeResult => ({
  document: { id: "doc-1", title: "a" },
  version: { id: `ver-${n}`, document_id: "doc-1", version_number: n, original_filename: "a.txt", content_type: "text/plain", size_bytes: 3, sha256: "x" },
});

export function session(n: number, over: Partial<UploadSession> = {}): UploadSession {
  return {
    upload_id: `up-${n}`,
    document_id: "doc-1",
    version_id: `ver-${n}`,
    expires_at: "2099-01-01T00:00:00Z",
    upload: {
      method: "PUT",
      url: `https://store.invalid/bucket/key-${n}?X-Amz-Signature=s${n}`,
      headers: { "Content-Type": "text/plain", "x-amz-checksum-sha256": `sum-${n}` },
    },
    limits: { max_bytes: 104857600 },
    ...over,
  };
}

type Step<T> = T | Error;

/** createUpload/finalizeUpload answered from scripts (Errors are thrown). Last step repeats. */
export function fakeApi(opts: { create?: Step<UploadSession>[]; finalize?: Step<FinalizeResult>[] } = {}) {
  const creates: CreateUploadBody[] = [];
  const finalizes: string[] = [];
  const pick = <T>(arr: Step<T>[], i: number, dflt: () => T): T => {
    const s = arr.length ? arr[Math.min(i, arr.length - 1)] : dflt();
    if (s instanceof Error) throw s;
    return s;
  };
  const api: UploadsApi = {
    createUpload: vi.fn(async (_o, _w, body) => {
      creates.push(body);
      return pick(opts.create ?? [], creates.length - 1, () => session(creates.length));
    }),
    finalizeUpload: vi.fn(async (_o, _w, id) => {
      finalizes.push(id);
      return pick(opts.finalize ?? [], finalizes.length - 1, () => FILE_RESULT());
    }),
  };
  return { api, creates, finalizes };
}

export const file = (name = "a.txt", content = "abc", type = "text/plain") => new File([content], name, { type });
export const sleeps: number[] = [];
export const instantSleep = async (ms: number) => {
  sleeps.push(ms);
};
export const fakeHash = async () => "ab".repeat(32);
