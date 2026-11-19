"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { DownloadIcon, FileIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatBytes } from "@/features/documents/upload/messages";
import type { Fetcher } from "@/features/organizations/api/http";
import { typeLabel } from "../format";
import type { Version } from "../types";
import { previewKind, TEXT_PREVIEW_CAP_BYTES } from "./preview-kind";
import { useContentUrl, useDownload } from "./use-content-url";

type Ids = { fetcher: Fetcher; orgId: string; workspaceId: string; documentId: string };

export function DownloadButton({ ids, versionId, label = "Download" }: { ids: Ids; versionId: string; label?: string }) {
  const { download, pending, error } = useDownload({ ...ids, versionId });
  return (
    <>
      <Button type="button" variant="outline" onClick={() => void download()} disabled={pending}>
        <DownloadIcon aria-hidden="true" />
        {pending ? "Preparing…" : label}
      </Button>
      {error && (
        <span role="alert" className="text-sm text-destructive">
          {error === "not_found" || error === "forbidden"
            ? "This file isn't available."
            : "We couldn't prepare the download. Try again."}
        </span>
      )}
    </>
  );
}

function DownloadCard({ ids, version, note }: { ids: Ids; version: Version; note?: string }) {
  return (
    <div className="grid justify-items-start gap-3 rounded-lg border border-border p-6" data-testid="preview-download">
      <FileIcon aria-hidden="true" className="size-8 text-muted-foreground" />
      <div className="grid gap-0.5">
        <p className="font-medium break-all">{version.original_filename}</p>
        <p className="text-sm text-muted-foreground">
          {typeLabel(version.content_type)} · {formatBytes(version.size_bytes)}
        </p>
      </div>
      <p className="text-sm text-muted-foreground">{note ?? "This file type can't be previewed in the browser."}</p>
      <DownloadButton ids={ids} versionId={version.id} />
    </div>
  );
}

function PreviewError({ onRetry, children }: { onRetry: () => void; children: string }) {
  return (
    <div role="alert" className="grid justify-items-start gap-3 rounded-lg border border-border p-4" data-testid="preview-error">
      <p className="text-sm">{children}</p>
      <Button variant="outline" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

function PreviewLoading() {
  return (
    <div role="status" aria-label="Loading preview" data-testid="preview-loading">
      <Skeleton className="h-96 w-full" />
    </div>
  );
}

/** Plain text, never HTML: the bytes are decoded and handed to React as a text node inside `<pre>`. */
function TextPreview({
  url,
  sizeBytes,
  onExpired,
  onGone,
  renderText,
}: {
  url: string;
  sizeBytes: number;
  onExpired: () => void;
  onGone: () => void;
  renderText?: (text: string) => ReactNode;
}) {
  const [state, setState] = useState<
    { url: string; status: "loading" } | { url: string; status: "ready"; text: string; truncated: boolean } | { url: string; status: "error" }
  >({ url, status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const renewals = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await fetch(url, { signal: controller.signal, credentials: "omit", referrerPolicy: "no-referrer" });
        if (res.status === 401 || res.status === 403) {
          // Expired/invalid signature: renew the URL once, then give up with an error (never loop).
          if (renewals.current < 1) {
            renewals.current += 1;
            onExpired();
            return;
          }
          setState({ url, status: "error" });
          return;
        }
        if (res.status === 404 || res.status === 410) {
          onGone();
          return;
        }
        if (!res.ok) {
          setState({ url, status: "error" });
          return;
        }
        const { text, truncated } = await readCapped(res, TEXT_PREVIEW_CAP_BYTES);
        renewals.current = 0;
        setState({ url, status: "ready", text, truncated: truncated || sizeBytes > TEXT_PREVIEW_CAP_BYTES });
      } catch {
        if (!controller.signal.aborted) setState({ url, status: "error" });
      }
    })();
    return () => controller.abort();
  }, [url, attempt, sizeBytes, onExpired, onGone]);

  const current = state.url === url ? state : { url, status: "loading" as const };
  if (current.status === "loading") return <PreviewLoading />;
  if (current.status === "error") return <PreviewError onRetry={() => setAttempt((n) => n + 1)}>We couldn&apos;t load the file contents.</PreviewError>;
  return (
    <div className="grid gap-2">
      {current.truncated && (
        <p role="status" className="text-xs text-muted-foreground" data-testid="preview-truncated">
          Showing the first {formatBytes(TEXT_PREVIEW_CAP_BYTES)} of this file. Download it to see everything.
        </p>
      )}
      <pre
        tabIndex={0}
        aria-label="File contents"
        className="max-h-[70vh] overflow-auto rounded-lg border border-border bg-muted/40 p-4 text-sm break-words whitespace-pre-wrap"
        data-testid="preview-text"
      >
        {renderText ? renderText(current.text) : current.text}
      </pre>
    </div>
  );
}

/** Reads at most `cap` bytes of the body (cancelling the rest) and decodes them as UTF-8 text. */
export async function readCapped(res: Response, cap: number): Promise<{ text: string; truncated: boolean }> {
  if (!res.body) {
    const buf = new Uint8Array(await res.arrayBuffer());
    const truncated = buf.byteLength > cap;
    return { text: new TextDecoder().decode(truncated ? buf.subarray(0, cap) : buf), truncated };
  }
  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let truncated = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (total + value.byteLength > cap) {
      chunks.push(value.subarray(0, cap - total));
      total = cap;
      truncated = true;
      await reader.cancel().catch(() => undefined);
      break;
    }
    chunks.push(value);
    total += value.byteLength;
  }
  const all = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    all.set(c, off);
    off += c.byteLength;
  }
  return { text: new TextDecoder("utf-8").decode(all), truncated };
}

/** Image / PDF source: pinned to the first URL so a background renewal does not reload what is on screen. */
function PinnedMedia({
  kind,
  url,
  title,
  version,
  page,
}: {
  kind: "pdf" | "image";
  url: string;
  title: string;
  version: Version;
  page?: number | null;
}) {
  const [pinned, setPinned] = useState(url);
  const [failedOnce, setFailedOnce] = useState(false);
  if (kind === "image") {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- presigned object-store URL, not optimisable by next/image
      <img
        src={pinned}
        alt={title}
        referrerPolicy="no-referrer"
        className="max-h-[75vh] max-w-full rounded-lg border border-border object-contain"
        data-testid="preview-image"
        onError={() => {
          // The signature may have expired while the page was open: try the renewed URL once.
          if (!failedOnce && url !== pinned) setPinned(url);
          setFailedOnce(true);
        }}
      />
    );
  }
  return (
    <iframe
      src={page && page > 0 ? `${pinned}#page=${page}` : pinned}
      title={`Preview of ${version.original_filename}`}
      sandbox="allow-scripts allow-same-origin"
      referrerPolicy="no-referrer"
      className="h-[75vh] w-full rounded-lg border border-border bg-background"
      data-testid="preview-pdf"
    />
  );
}

export function Preview({
  ids,
  version,
  title,
  onGone,
  pdfPage,
  renderText,
}: {
  ids: Ids;
  version: Version;
  title: string;
  /** The version turned out to be gone (404/410 from the API or the store). */
  onGone: () => void;
  /** PDFs open at this 1-based page (`#page=n`). */
  pdfPage?: number | null;
  /** Text previews render through this (the evidence viewer highlights an excerpt); default is the plain text. */
  renderText?: (text: string) => ReactNode;
}) {
  const kind = previewKind(version.content_type);
  if (kind === "download") return <DownloadCard ids={ids} version={version} />;
  return <InlinePreview ids={ids} version={version} title={title} kind={kind} onGone={onGone} pdfPage={pdfPage} renderText={renderText} />;
}

function InlinePreview({
  ids,
  version,
  title,
  kind,
  onGone,
  pdfPage,
  renderText,
}: {
  ids: Ids;
  version: Version;
  title: string;
  kind: "pdf" | "image" | "text";
  onGone: () => void;
  pdfPage?: number | null;
  renderText?: (text: string) => ReactNode;
}) {
  const { state, refresh } = useContentUrl({ ...ids, versionId: version.id, disposition: "inline" });
  const apiGone = state.status === "error" && (state.code === "not_found" || state.code === "forbidden");
  useEffect(() => {
    if (apiGone) onGone();
  }, [apiGone, onGone]);

  if (state.status === "loading") return <PreviewLoading />;
  if (state.status === "error") {
    if (apiGone) return null;
    if (state.code === "unauthenticated") return <PreviewLoading />;
    return <PreviewError onRetry={refresh}>The file store is temporarily unavailable. Try again in a moment.</PreviewError>;
  }
  const { content } = state;
  // The API refuses inline for anything outside the allow-list: honour that and offer a download.
  if (content.disposition !== "inline") {
    return <DownloadCard ids={ids} version={version} note="This file can't be shown in the browser. Download it to view it." />;
  }
  if (kind === "text") {
    return <TextPreview url={content.url} sizeBytes={version.size_bytes} onExpired={refresh} onGone={onGone} renderText={renderText} />;
  }
  return <PinnedMedia key={version.id} kind={kind} url={content.url} title={title} version={version} page={pdfPage} />;
}
