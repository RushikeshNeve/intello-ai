"use client";

import { useCallback, useEffect, useState } from "react";
import type { Fetcher } from "@/features/organizations/api/http";
import { getContentUrl, toDocumentErrorCode, type DocumentErrorCode } from "../api";
import type { ContentUrl, Disposition } from "../types";

export type ContentState =
  | { status: "loading" }
  | { status: "ready"; content: ContentUrl }
  | { status: "error"; code: DocumentErrorCode };

/** Renew this long before `expires_at` so a URL is never used (or kept) past its life. */
export const EXPIRY_MARGIN_MS = 15_000;
const MIN_REFRESH_MS = 1_000;

type Args = {
  fetcher: Fetcher;
  orgId: string;
  workspaceId: string;
  documentId: string;
  versionId: string;
  disposition: Disposition;
};

/**
 * Presigned content URL for one version, held in component state only (never in a persistent cache).
 * It is re-requested shortly before `expires_at`, on `refresh()` (the store answered 401/403) and whenever
 * the version changes.
 */
export function useContentUrl({ fetcher, orgId, workspaceId, documentId, versionId, disposition }: Args) {
  const identity = `${orgId}/${workspaceId}/${documentId}/${versionId}/${disposition}`;
  const [result, setResult] = useState<{ identity: string; state: ContentState }>({ identity, state: { status: "loading" } });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    getContentUrl(fetcher, orgId, workspaceId, documentId, versionId, disposition).then(
      (content) => {
        if (cancelled) return;
        setResult({ identity, state: { status: "ready", content } });
        const ttl = new Date(content.expires_at).getTime() - Date.now() - EXPIRY_MARGIN_MS;
        timer = setTimeout(() => setNonce((n) => n + 1), Math.max(ttl, MIN_REFRESH_MS));
      },
      (err: unknown) => {
        if (!cancelled) setResult({ identity, state: { status: "error", code: toDocumentErrorCode(err) } });
      },
    );
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [fetcher, orgId, workspaceId, documentId, versionId, disposition, identity, nonce]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  // A result for another version is never shown: derive "loading" instead of resetting state in an effect.
  const state: ContentState = result.identity === identity ? result.state : { status: "loading" };
  return { state, refresh };
}

/** One-shot "give me a fresh attachment URL and start the download". */
export function useDownload(args: Omit<Args, "disposition">) {
  const { fetcher, orgId, workspaceId, documentId, versionId } = args;
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<DocumentErrorCode | null>(null);

  const download = useCallback(async () => {
    setPending(true);
    setError(null);
    try {
      const content = await getContentUrl(fetcher, orgId, workspaceId, documentId, versionId, "attachment");
      const a = document.createElement("a");
      a.href = content.url;
      a.rel = "noopener noreferrer";
      a.download = content.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (err) {
      setError(toDocumentErrorCode(err));
    } finally {
      setPending(false);
    }
  }, [fetcher, orgId, workspaceId, documentId, versionId]);

  return { download, pending, error };
}
