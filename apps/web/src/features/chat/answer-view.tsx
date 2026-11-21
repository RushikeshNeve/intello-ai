"use client";

import { useState, type MouseEvent } from "react";
import Link from "next/link";
import { CheckIcon, CopyIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Workspace } from "@/features/workspaces/api";
import type { Citation, Verification } from "./api";
import { citationByMarker, citationHref, citationLabel } from "./citations";
import { parseBlocks, type Inline } from "./format";
import { verificationNote } from "./messages";

export type LinkCtx = {
  orgSlug: string;
  workspaces: readonly Workspace[];
  /** Select a citation into the evidence panel. Without it chips are plain links to the evidence page. */
  onCite?: (c: Citation) => void;
  /** Chunk currently shown in the panel (marks its chip). */
  activeChunkId?: string | null;
};

/** Plain left click selects the citation; modified clicks keep the link's normal behaviour (new tab etc.). */
function cite(ctx: LinkCtx, c: Citation) {
  return (e: MouseEvent) => {
    if (!ctx.onCite || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    ctx.onCite(c);
  };
}

function InlineNodes({ nodes, byMarker, ctx }: { nodes: Inline[]; byMarker: Map<string, Citation>; ctx: LinkCtx }) {
  return (
    <>
      {nodes.map((n, i) => {
        if (n.t === "text") return <span key={i}>{n.text}</span>;
        if (n.t === "code") {
          return (
            <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.9em]">
              {n.text}
            </code>
          );
        }
        const c = byMarker.get(n.marker);
        if (!c) return <span key={i}>{n.raw}</span>; // a marker without a citation stays plain text
        const active = ctx.activeChunkId === c.chunk_id;
        return (
          <Link
            key={i}
            href={citationHref(c, ctx.orgSlug, ctx.workspaces)}
            onClick={cite(ctx, c)}
            aria-label={citationLabel(c)}
            aria-current={active ? "true" : undefined}
            title={citationLabel(c)}
            className="focus-ring mx-0.5 inline-flex min-w-5 items-center justify-center rounded-control border border-border bg-info-soft px-1 text-xs font-semibold text-brand-blue no-underline hover:bg-accent aria-[current=true]:border-brand-blue aria-[current=true]:bg-brand-blue aria-[current=true]:text-white"
            data-testid="citation-chip"
            data-chunk-id={c.chunk_id}
            data-version-id={c.version_id}
            data-document-id={c.document_id}
          >
            {n.raw}
          </Link>
        );
      })}
    </>
  );
}

/** The answer text as paragraphs/lists with `[n]` chips that open the cited passage. */
export function AnswerBody({ text, citations, ctx }: { text: string; citations: readonly Citation[]; ctx: LinkCtx }) {
  const byMarker = citationByMarker(citations);
  return (
    <div className="grid gap-2 text-sm leading-relaxed break-words" data-testid="answer-body">
      {parseBlocks(text).map((b, i) => {
        if (b.t === "p") {
          return (
            <p key={i} className="whitespace-pre-line">
              <InlineNodes nodes={b.inline} byMarker={byMarker} ctx={ctx} />
            </p>
          );
        }
        const Tag = b.t === "ul" ? "ul" : "ol";
        return (
          <Tag key={i} className={b.t === "ul" ? "ml-5 list-disc" : "ml-5 list-decimal"}>
            {b.items.map((item, j) => (
              <li key={j}>
                <InlineNodes nodes={item} byMarker={byMarker} ctx={ctx} />
              </li>
            ))}
          </Tag>
        );
      })}
    </div>
  );
}

const BANNER_TONE = {
  abstained: "border-border bg-muted/50",
  partial: "border-border bg-warning-soft",
  flagged: "border-border bg-warning-soft",
  unverified: "border-border bg-warning-soft",
} as const;

export function VerificationBanner({ verification }: { verification: Verification | null }) {
  const note = verificationNote(verification);
  if (!note) return null;
  return (
    <p role="note" className={`rounded-control border px-3 py-2 text-sm ${BANNER_TONE[note.tone]}`} data-testid="verification-banner" data-tone={note.tone}>
      {note.text}
    </p>
  );
}

export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false); // clipboard unavailable: nothing to report
    }
  }
  return (
    <Button type="button" variant="ghost" size="sm" onClick={() => void copy()} data-testid="copy-answer">
      {copied ? <CheckIcon aria-hidden="true" /> : <CopyIcon aria-hidden="true" />}
      {copied ? "Copied" : "Copy"}
    </Button>
  );
}

/** Sources behind an answer: one line per citation, each opening exactly its passage. */
export function SourceList({ citations, ctx }: { citations: readonly Citation[]; ctx: LinkCtx }) {
  if (citations.length === 0) return null;
  return (
    <details className="text-sm" data-testid="source-list">
      <summary className="focus-ring cursor-pointer rounded-control text-text-secondary">Sources ({citations.length})</summary>
      <ol className="mt-2 grid gap-2">
        {citations.map((c) => (
          <li
            key={c.marker}
            className="grid gap-0.5 rounded-control border border-border-subtle bg-muted/30 p-2 aria-[current=true]:border-brand-blue"
            aria-current={ctx.activeChunkId === c.chunk_id ? "true" : undefined}
            data-testid="source-item"
          >
            <Link href={citationHref(c, ctx.orgSlug, ctx.workspaces)} onClick={cite(ctx, c)} className="focus-ring font-medium underline-offset-2 hover:underline" data-testid="source-link">
              {c.marker} {c.title || "Document"}
            </Link>
            <span className="text-xs text-text-secondary">
              {c.version_number !== null ? `Version ${c.version_number}` : ""}
              {c.page_start !== null ? `${c.version_number !== null ? " · " : ""}${c.page_end !== null && c.page_end !== c.page_start ? `pages ${c.page_start}–${c.page_end}` : `page ${c.page_start}`}` : ""}
            </span>
            {c.excerpt && <span className="text-xs break-words text-text-secondary">{c.excerpt}</span>}
          </li>
        ))}
      </ol>
    </details>
  );
}
