import Link from "next/link";
import type { Workspace } from "@/features/workspaces/api";
import type { Citation, Verification } from "./api";
import { citationByMarker, citationHref, citationLabel } from "./citations";
import { parseBlocks, type Inline } from "./format";
import { verificationNote } from "./messages";

type LinkCtx = { orgSlug: string; workspaces: readonly Workspace[] };

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
        return (
          <Link
            key={i}
            href={citationHref(c, ctx.orgSlug, ctx.workspaces)}
            aria-label={citationLabel(c)}
            title={citationLabel(c)}
            className="mx-0.5 inline-flex min-w-5 items-center justify-center rounded-md border border-border bg-muted px-1 text-xs font-medium no-underline hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
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

export function VerificationBanner({ verification }: { verification: Verification | null }) {
  const note = verificationNote(verification);
  if (!note) return null;
  return (
    <p role="note" className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm" data-testid="verification-banner" data-tone={note.tone}>
      {note.text}
    </p>
  );
}

/** Sources behind an answer: one line per citation, each opening exactly its passage. */
export function SourceList({ citations, ctx }: { citations: readonly Citation[]; ctx: LinkCtx }) {
  if (citations.length === 0) return null;
  return (
    <details className="text-sm" data-testid="source-list">
      <summary className="cursor-pointer text-muted-foreground">
        Sources ({citations.length})
      </summary>
      <ol className="mt-2 grid gap-2">
        {citations.map((c) => (
          <li key={c.marker} className="grid gap-0.5 rounded-md bg-muted/30 p-2" data-testid="source-item">
            <Link href={citationHref(c, ctx.orgSlug, ctx.workspaces)} className="font-medium underline-offset-2 hover:underline">
              {c.marker} {c.title || "Document"}
            </Link>
            <span className="text-xs text-muted-foreground">
              {c.version_number !== null ? `Version ${c.version_number}` : ""}
              {c.page_start !== null ? `${c.version_number !== null ? " · " : ""}${c.page_end !== null && c.page_end !== c.page_start ? `pages ${c.page_start}–${c.page_end}` : `page ${c.page_start}`}` : ""}
            </span>
            {c.excerpt && <span className="text-xs break-words text-muted-foreground">{c.excerpt}</span>}
          </li>
        ))}
      </ol>
    </details>
  );
}
