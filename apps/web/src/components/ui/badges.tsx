import type { ReactNode } from "react";
import { cn } from "cn";

const pill = "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-meta font-semibold whitespace-nowrap";

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger" | "violet";
const TONE: Record<StatusTone, string> = {
  neutral: "bg-muted text-text-secondary",
  info: "bg-info-soft text-info",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  danger: "bg-danger-soft text-danger",
  violet: "bg-violet-soft text-brand-violet dark:text-info",
};

/** Status pill. Colour is never the only signal: always pass a text label (`children`). */
export function StatusBadge({ tone = "neutral", children, className }: { tone?: StatusTone; children: ReactNode; className?: string }) {
  return (
    <span className={cn(pill, TONE[tone], className)} data-tone={tone}>
      {children}
    </span>
  );
}

export type Scope = "personal" | "workspace" | "organization";
const SCOPE_LABEL: Record<Scope, string> = { personal: "Personal", workspace: "Workspace", organization: "Organization" };
const SCOPE_TONE: Record<Scope, StatusTone> = { personal: "violet", workspace: "info", organization: "neutral" };

/** Where a thing lives (personal / workspace / organization). */
export function ScopeBadge({ scope, className }: { scope: Scope; className?: string }) {
  return (
    <StatusBadge tone={SCOPE_TONE[scope]} className={className}>
      {SCOPE_LABEL[scope]}
    </StatusBadge>
  );
}

/** Short type label for a document ("PDF", "DOCX"...) from a file name or MIME type; falls back to "FILE". */
export function fileTypeLabel(input: string | null | undefined): string {
  if (!input) return "FILE";
  const mime: Record<string, string> = {
    "application/pdf": "PDF",
    "text/plain": "TXT",
    "text/markdown": "MD",
    "text/html": "HTML",
    "text/csv": "CSV",
    "application/json": "JSON",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PPTX",
  };
  const base = input.split(";")[0].trim().toLowerCase();
  if (mime[base]) return mime[base];
  const ext = /\.([a-z0-9]{1,5})$/i.exec(base)?.[1];
  return ext ? ext.toUpperCase() : "FILE";
}

/** Square-ish type chip for document rows. Pass the file name or content type. */
export function FileTypeBadge({ type, className }: { type: string | null | undefined; className?: string }) {
  return (
    <span className={cn("inline-grid h-7 min-w-9 place-items-center rounded-md bg-info-soft px-1.5 text-[0.625rem] font-bold tracking-wide text-info", className)} data-testid="file-type-badge">
      {fileTypeLabel(type)}
    </span>
  );
}
