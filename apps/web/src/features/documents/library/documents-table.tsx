"use client";

import Link from "next/link";
import { ArrowDownIcon, ArrowUpIcon, ChevronsUpDownIcon, PencilIcon, Trash2Icon } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { FileTypeBadge } from "@/components/ui/badges";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatBytes } from "@/features/documents/upload/messages";
import { formatDate, typeLabel } from "../format";
import { IngestionBadge } from "../ingestion";
import type { Document, Order, Sort } from "../types";

export type DocumentsTableProps = {
  status: "loading" | "error" | "ready";
  items: Document[];
  sort: Sort;
  order: Order;
  onSort: (sort: Sort) => void;
  /** Any search text or type filter is active. */
  filtered: boolean;
  canUpload: boolean;
  onClear: () => void;
  onRetry: () => void;
  /** `/o/{org}/w/{ws}/documents` */
  basePath: string;
  onRename: (doc: Document) => void;
  onDelete: (doc: Document) => void;
};

function ariaSort(active: boolean, order: Order): "ascending" | "descending" | "none" {
  return active ? (order === "asc" ? "ascending" : "descending") : "none";
}

function SortHeader({ label, column, sort, order, onSort }: { label: string; column: Sort } & Pick<DocumentsTableProps, "sort" | "order" | "onSort">) {
  const active = sort === column;
  const Icon = !active ? ChevronsUpDownIcon : order === "asc" ? ArrowUpIcon : ArrowDownIcon;
  return (
    <TableHead aria-sort={ariaSort(active, order)} scope="col">
      <button
        type="button"
        onClick={() => onSort(column)}
        className="-mx-1 inline-flex items-center gap-1 focus-ring rounded px-1 py-0.5 hover:text-foreground"
      >
        {label}
        <Icon aria-hidden="true" className="size-3" />
      </button>
    </TableHead>
  );
}

export function DocumentsTable(props: DocumentsTableProps) {
  const { status, items, filtered, canUpload } = props;

  if (status === "loading") {
    return (
      <div role="status" aria-label="Loading documents" className="grid gap-2" data-testid="documents-loading">
        {Array.from({ length: 6 }, (_, i) => (
          <Skeleton key={i} className="h-12 w-full rounded-control" />
        ))}
      </div>
    );
  }

  if (status === "error") {
    return (
      <div data-testid="documents-error">
        <ErrorState title="We couldn't load the documents" description="Your files are safe." onRetry={props.onRetry} />
      </div>
    );
  }

  if (items.length === 0) {
    return filtered ? (
      <div data-testid="documents-no-results">
        <EmptyState
          title="No documents match your search or filters."
          action={
            <Button variant="outline" onClick={props.onClear}>
              Clear filters
            </Button>
          }
        />
      </div>
    ) : (
      <div data-testid="documents-empty">
        <EmptyState
          title="No documents yet"
          description={
            canUpload
              ? "Drop a file into the upload area above to add your first document."
              : "Documents added to this workspace will appear here."
          }
        />
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-card border border-border bg-card shadow-card">
    <Table data-testid="documents-table">
      <TableCaption className="sr-only">Documents in this workspace</TableCaption>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <SortHeader label="Title" column="title" sort={props.sort} order={props.order} onSort={props.onSort} />
          <TableHead scope="col" className="hidden md:table-cell">
            Type
          </TableHead>
          <TableHead scope="col">Status</TableHead>
          <TableHead scope="col" className="text-right">
            Size
          </TableHead>
          <SortHeader label="Updated" column="updated_at" sort={props.sort} order={props.order} onSort={props.onSort} />
          <TableHead scope="col" className="text-right">
            Versions
          </TableHead>
          <TableHead scope="col">
            <span className="sr-only">Actions</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((doc) => {
          const href = `${props.basePath}/${encodeURIComponent(doc.id)}`;
          return (
            <TableRow key={doc.id} data-testid="document-row">
              <TableCell className="max-w-[28rem] min-w-0">
                <div className="flex items-center gap-3">
                  <FileTypeBadge type={doc.latest_version?.content_type ?? doc.latest_version?.original_filename} className="shrink-0" />
                  <div className="min-w-0">
                <Link
                  id={`doc-link-${doc.id}`}
                  href={href}
                  className="block truncate focus-ring rounded font-semibold text-brand-indigo hover:underline dark:text-foreground"
                >
                  {doc.title}
                </Link>
                {doc.latest_version && (
                  <span className="block truncate text-meta text-text-secondary">{doc.latest_version.original_filename}</span>
                )}
                  </div>
                </div>
              </TableCell>
              <TableCell className="hidden md:table-cell">{typeLabel(doc.latest_version?.content_type)}</TableCell>
              <TableCell>
                <IngestionBadge summary={doc.ingestion} />
              </TableCell>
              <TableCell className="text-right whitespace-nowrap tabular-nums">
                {doc.latest_version ? formatBytes(doc.latest_version.size_bytes) : "-"}
              </TableCell>
              <TableCell className="whitespace-nowrap">
                <time dateTime={doc.updated_at}>{formatDate(doc.updated_at)}</time>
              </TableCell>
              <TableCell className="text-right tabular-nums">{doc.version_count}</TableCell>
              <TableCell>
                <div className="flex justify-end gap-1">
                  <Link href={href} className={buttonVariants({ variant: "ghost", size: "sm" })} aria-label={`Open ${doc.title}`}>
                    Open
                  </Link>
                  {doc.permissions.write && (
                    <Button variant="ghost" size="sm" aria-label={`Rename ${doc.title}`} onClick={() => props.onRename(doc)}>
                      <PencilIcon aria-hidden="true" />
                      Rename
                    </Button>
                  )}
                  {doc.permissions.manage && (
                    <Button variant="ghost" size="sm" aria-label={`Delete ${doc.title}`} onClick={() => props.onDelete(doc)}>
                      <Trash2Icon aria-hidden="true" />
                      Delete
                    </Button>
                  )}
                </div>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
    </div>
  );
}
