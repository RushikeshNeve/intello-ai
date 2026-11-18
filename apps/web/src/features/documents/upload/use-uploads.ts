"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { useApiFetcher } from "@/lib/query/provider";
import { createUploadsApi, type FinalizeResult } from "./api";
import { UploadEngine, type EngineOptions, type UploadItem } from "./upload-engine";

export type UseUploadsOptions = {
  orgId: string;
  workspaceId: string;
  /** Upload new VERSIONS of this document instead of new documents. */
  documentId?: string;
  maxBytes?: number;
  onUploaded?: (result: FinalizeResult) => void;
  /** Test seam: replaces engine collaborators (hash, XHR, timers, API). */
  engineOptions?: Partial<EngineOptions>;
};

export function useUploads(o: UseUploadsOptions) {
  const fetcher = useApiFetcher();
  // One engine per mounted uploader (the fetcher is stable; `engineOptions` is a mount-time test seam).
  const [engine] = useState(() => new UploadEngine({ api: createUploadsApi(fetcher), ...o.engineOptions }));
  const { onUploaded } = o;
  useEffect(() => {
    engine.setOnDone(onUploaded ? (r) => onUploaded(r) : undefined);
  }, [engine, onUploaded]);
  const items: readonly UploadItem[] = useSyncExternalStore(engine.subscribe, engine.getSnapshot, engine.getSnapshot);
  const { orgId, workspaceId, documentId, maxBytes } = o;
  return {
    items,
    addFiles: (files: File[]) => engine.add(files, { orgId, workspaceId, documentId }, maxBytes),
    retry: (id: string) => engine.retry(id),
    cancel: (id: string) => engine.cancel(id),
    remove: (id: string) => engine.remove(id),
    clearFinished: () => engine.clearFinished(),
  };
}
