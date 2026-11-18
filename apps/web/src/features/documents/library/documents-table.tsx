"use client";

import Link from "next/link";
import { ArrowDownIcon, ArrowUpIcon, ChevronsUpDownIcon, PencilIcon, Trash2Icon } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatBytes } from "@/features/documents/upload/messages";
import { formatDate, typeLabel } from "../format";
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
        className="-mx-1 inline-flex items-center gap-1 rounded px-1 py-0.5 outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
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
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </div>
    );
  }

  if (status === "error") {
    return (
      <div role="alert" className="grid justify-items-start gap-3 rounded-lg border border-border p-4" data-testid="documents-error">
        <p className="text-sm">We couldn&apos;t load the documents. Your files are safe.</p>
        <Button variant="outline" onClick={props.onRetry}>
          Retry
        </Button>
      </div>
    );
  }

  if (items.length === 0) {
    return filtered ? (
      <div className="grid justify-items-start gap-3 rounded-lg border border-dashed border-border p-6" data-testid="documents-no-results">
        <p className="text-sm">No documents match your search or filters.</p>
        <Button variant="outline" onClick={props.onClear}>
          Clear filters
        </Button>
      </div>
    ) : (
      <div className="grid gap-1 rounded-lg border border-dashed border-border p-6" data-testid="documents-empty">
        <p className="text-base font-medium">No documents yet</p>
        <p className="text-sm text-muted-foreground">
          {canUpload
            ? "Drop a file into the upload area above to add your first document."
            : "Documents added to this workspace will appear here."}
        </p>
      </div>
    );
  }

  return (
    <Table data-testid="documents-table">
      <TableCaption className="sr-only">Documents in this workspace</TableCaption>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <SortHeader label="Title" column="title" sort={props.sort} order={props.order} onSort={props.onSort} />
          <TableHead scope="col">Type</TableHead>
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
                <Link
                  id={`doc-link-${doc.id}`}
                  href={href}
                  className="block truncate rounded font-medium outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
                >
                  {doc.title}
                </Link>
                {doc.latest_version && (
                  <span className="block truncate text-xs text-muted-foreground">{doc.latest_version.original_filename}</span>
                )}
              </TableCell>
              <TableCell>{typeLabel(doc.latest_version?.content_type)}</TableCell>
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
  );
}
