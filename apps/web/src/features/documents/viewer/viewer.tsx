"use client";

import { useCallback, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeftIcon, PencilIcon, Trash2Icon } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { useApiFetcher } from "@/lib/query/provider";
import { DocumentUploader } from "@/features/documents/upload";
import { deleteDocument, renameDocument } from "../api";
import { documentKeys } from "../keys";
import { formatBytes } from "@/features/documents/upload/messages";
import type { Document, Version } from "../types";
import { DeleteDialog, RenameDialog } from "../library/document-dialogs";
import { NotAvailable } from "./not-available";
import { DownloadButton, Preview } from "./preview";
import { VersionPicker } from "./version-picker";

export type DocumentViewerProps = {
  orgId: string;
  workspaceId: string;
  /** `/o/{org}/w/{ws}/documents` */
  libraryPath: string;
  document: Document;
  /** Newest first. */
  versions: Version[];
  /** The version asked for (`?v=`, default latest); null when it is unknown or belongs to another document. */
  selected: Version | null;
};

export function DocumentViewer({ orgId, workspaceId, libraryPath, document: doc, versions, selected }: DocumentViewerProps) {
  const router = useRouter();
  const fetcher = useApiFetcher();
  const queryClient = useQueryClient();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [gone, setGone] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const documentPath = `${libraryPath}/${encodeURIComponent(doc.id)}`;
  const ids = { fetcher, orgId, workspaceId, documentId: doc.id };

  // The preview reports "this version is gone" (404/410): remember which one, so switching versions resets.
  const selectedId = selected?.id ?? null;
  const onGone = useCallback(() => setGone(selectedId), [selectedId]);
  const unavailable = selected !== null && gone === selected.id;
  const latest = versions[0] ?? doc.latest_version;

  async function doRename(title: string) {
    await renameDocument(fetcher, orgId, workspaceId, doc.id, title);
    void queryClient.invalidateQueries({ queryKey: documentKeys.all(orgId, workspaceId) });
    setRenaming(false);
    router.refresh();
    setTimeout(() => headingRef.current?.focus(), 0);
  }

  async function doDelete() {
    await deleteDocument(fetcher, orgId, workspaceId, doc.id);
    void queryClient.invalidateQueries({ queryKey: documentKeys.all(orgId, workspaceId) });
    setDeleting(false);
    router.push(libraryPath);
  }

  return (
    <div className="mx-auto grid max-w-6xl gap-6" data-testid="document-viewer" data-document-id={doc.id}>
      <div className="grid gap-3">
        <Link href={libraryPath} className="inline-flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeftIcon aria-hidden="true" className="size-3.5" />
          Documents
        </Link>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="grid min-w-0 gap-1">
            <h1 ref={headingRef} tabIndex={-1} className="text-2xl font-semibold tracking-tight break-words outline-none" data-testid="document-title">
              {doc.title}
            </h1>
            {selected && (
              <p className="text-sm text-muted-foreground" data-testid="selected-version-meta">
                Version {selected.version_number} · {selected.original_filename} · {formatBytes(selected.size_bytes)}
              </p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {selected && !unavailable && <DownloadButton ids={ids} versionId={selected.id} />}
            {doc.permissions.write && (
              <Button variant="outline" onClick={() => setRenaming(true)}>
                <PencilIcon aria-hidden="true" />
                Rename
              </Button>
            )}
            {doc.permissions.manage && (
              <Button variant="outline" onClick={() => setDeleting(true)}>
                <Trash2Icon aria-hidden="true" />
                Delete
              </Button>
            )}
          </div>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <section aria-label="Preview" className="grid min-w-0 content-start gap-4" data-testid="preview-region">
          {selected && !unavailable ? (
            <Preview key={selected.id} ids={ids} version={selected} title={doc.title} onGone={onGone} />
          ) : (
            <NotAvailable backHref={libraryPath} backLabel="Back to documents" headingLevel={2}>
              {selected === null && latest ? (
                <Link href={documentPath} className={buttonVariants({ variant: "outline" })}>
                  View the latest version
                </Link>
              ) : null}
            </NotAvailable>
          )}
        </section>
        <aside className="grid content-start gap-6">
          {doc.permissions.write && (
            <section aria-labelledby="new-version-heading" className="grid gap-2">
              <h2 id="new-version-heading" className="text-sm font-medium">
                Upload new version
              </h2>
              <DocumentUploader
                orgId={orgId}
                workspaceId={workspaceId}
                documentId={doc.id}
                onUploaded={(result) => {
                  void queryClient.invalidateQueries({ queryKey: documentKeys.all(orgId, workspaceId) });
                  router.push(`${documentPath}?v=${encodeURIComponent(result.version.id)}`);
                  router.refresh();
                }}
              />
            </section>
          )}
          <VersionPicker versions={versions} selectedId={selected?.id ?? null} documentPath={documentPath} />
        </aside>
      </div>

      {renaming && <RenameDialog title={doc.title} onClose={() => setRenaming(false)} onRename={doRename} />}
      {deleting && <DeleteDialog title={doc.title} onClose={() => setDeleting(false)} onDelete={doDelete} />}
    </div>
  );
}
