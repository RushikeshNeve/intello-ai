"use client";

import { useRef, useState, type DragEvent } from "react";
import { UploadCloudIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { cn } from "cn";
import type { FinalizeResult } from "./api";
import { errorMessage, formatBytes, isRetryable } from "./messages";
import { useUploads } from "./use-uploads";
import { isActive, type EngineOptions, type UploadItem } from "./upload-engine";

export type DocumentUploaderProps = {
  orgId: string;
  workspaceId: string;
  /** Upload new VERSIONS of this document instead of creating documents. */
  documentId?: string;
  /** The caller cannot upload (no `can_upload` / `document.write`): the zone is inert and says why. */
  disabled?: boolean;
  onUploaded?: (result: FinalizeResult) => void;
  /** Client-side size limit before the server's `limits.max_bytes` is known. Default 100 MiB. */
  maxBytes?: number;
  /** Test seam only. */
  engineOptions?: Partial<EngineOptions>;
};

const STATE_TEXT: Record<UploadItem["state"], string> = {
  queued: "Waiting",
  hashing: "Preparing",
  "creating-session": "Starting upload",
  uploading: "Uploading",
  finalizing: "Finishing",
  done: "Uploaded",
  failed: "Failed",
  cancelled: "Cancelled",
};

function statusText(i: UploadItem): string {
  if (i.state === "failed" && i.error) return errorMessage(i.error, i.maxBytes);
  return STATE_TEXT[i.state];
}

export function DocumentUploader({ orgId, workspaceId, documentId, disabled, onUploaded, maxBytes, engineOptions }: DocumentUploaderProps) {
  const { items, addFiles, retry, cancel, remove } = useUploads({ orgId, workspaceId, documentId, maxBytes, onUploaded, engineOptions });
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState("");

  const accept = (files: File[]) => {
    if (disabled || files.length === 0) return;
    setNotice("");
    addFiles(files);
  };

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(false);
    if (disabled) return;
    const files: File[] = [];
    let skippedFolder = false;
    const dtItems = Array.from(e.dataTransfer.items ?? []);
    if (dtItems.length > 0) {
      for (const it of dtItems) {
        if (it.kind !== "file") continue;
        if (it.webkitGetAsEntry?.()?.isDirectory) {
          skippedFolder = true;
          continue;
        }
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    } else {
      files.push(...Array.from(e.dataTransfer.files));
    }
    setNotice(skippedFolder ? "Folders can't be uploaded. Drop files instead." : "");
    if (files.length) addFiles(files);
  }

  const live = [
    notice,
    disabled ? "You don't have permission to upload here." : "",
    ...items.map((i) => `${i.file.name}: ${statusText(i)}`),
  ]
    .filter(Boolean)
    .join(". ");

  return (
    <section aria-label="Upload documents" className="space-y-3">
      <div
        data-testid="upload-dropzone"
        aria-disabled={disabled || undefined}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn(
          "flex flex-col items-center gap-2 rounded-card border-2 border-dashed border-input bg-card px-4 py-8 text-center text-sm transition-colors motion-reduce:transition-none",
          dragging && "border-brand-blue bg-info-soft",
          disabled && "opacity-60",
        )}
      >
        <span aria-hidden="true" className="grid size-10 place-items-center rounded-full bg-info-soft text-brand-blue">
          <UploadCloudIcon className="size-5" />
        </span>
        <p className="text-text-secondary">
          {disabled
            ? "You don't have permission to upload here."
            : documentId
              ? "Drag a file here to add a new version, or"
              : "Drag files here, or"}
        </p>
        <Button type="button" variant="outline" disabled={disabled} onClick={() => inputRef.current?.click()}>
          Choose files
        </Button>
        <input
          ref={inputRef}
          data-testid="upload-input"
          type="file"
          multiple={!documentId}
          disabled={disabled}
          tabIndex={-1}
          aria-hidden="true"
          className="sr-only"
          onChange={(e) => {
            accept(Array.from(e.target.files ?? []));
            e.target.value = ""; // allow choosing the same file again
          }}
        />
      </div>

      {notice && <p className="text-sm text-muted-foreground">{notice}</p>}
      <div role="status" aria-live="polite" className="sr-only" data-testid="upload-live">
        {live}
      </div>

      {items.length > 0 && (
        <ul className="divide-y divide-border rounded-card border border-border bg-card" aria-label="Uploads">
          {items.map((i) => (
            <li key={i.id} className="space-y-1.5 px-3 py-2.5 text-sm">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate font-medium">{i.file.name}</p>
                  <p className="text-xs text-muted-foreground">{formatBytes(i.file.size)}</p>
                </div>
                <div className="flex shrink-0 gap-1.5">
                  {(i.state === "failed" || i.state === "cancelled") && (i.error === null || isRetryable(i.error)) && (
                    <Button type="button" size="sm" variant="outline" onClick={() => retry(i.id)} aria-label={`Retry ${i.file.name}`}>
                      Retry
                    </Button>
                  )}
                  {isActive(i.state) ? (
                    <Button type="button" size="sm" variant="ghost" onClick={() => cancel(i.id)} aria-label={`Cancel ${i.file.name}`}>
                      Cancel
                    </Button>
                  ) : (
                    <Button type="button" size="sm" variant="ghost" onClick={() => remove(i.id)} aria-label={`Remove ${i.file.name}`}>
                      Remove
                    </Button>
                  )}
                </div>
              </div>
              {(i.state === "uploading" || i.state === "finalizing") && (
                <Progress value={i.progress * 100} label={`Upload progress for ${i.file.name}`} />
              )}
              <p
                className={cn("text-xs", i.state === "failed" ? "text-destructive" : "text-muted-foreground")}
                data-testid={`status-${i.file.name}`}
              >
                {statusText(i)}
                {i.state === "uploading" ? ` ${Math.round(i.progress * 100)}%` : ""}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
