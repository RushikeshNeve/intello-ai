import Link from "next/link";
import { typeLabel } from "@/features/documents/format";
import { documentHref, evidenceHref } from "@/features/evidence/links";
import type { SearchResult, SearchSnippet } from "./api";
import { segmentText } from "./highlight";

/** Snippet text with `<mark>` around the highlighted ranges. React text nodes only: no HTML is ever parsed. */
export function HighlightedSnippet({ text, ranges }: { text: string; ranges: readonly (readonly number[])[] }) {
  return (
    <>
      {segmentText(text, ranges).map((seg, i) =>
        seg.mark ? (
          <mark key={i} className="rounded-sm bg-yellow-200 px-0.5 text-foreground dark:bg-yellow-500/40">
            {seg.text}
          </mark>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </>
  );
}

export function pageLabel(s: Pick<SearchSnippet, "page_start" | "page_end">): string | null {
  if (s.page_start === null) return null;
  return s.page_end !== null && s.page_end !== s.page_start ? `pp. ${s.page_start}–${s.page_end}` : `p. ${s.page_start}`;
}

export function SearchResultCard({
  result,
  orgSlug,
  workspaceName,
}: {
  result: SearchResult;
  orgSlug: string;
  workspaceName: string;
}) {
  const docLink = documentHref(orgSlug, result.workspace_slug, result.document_id, result.version_id);
  return (
    <li className="grid gap-2 rounded-lg border border-border p-4" data-testid="search-result" data-document-id={result.document_id}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className="text-base font-medium break-words">
          <Link href={docLink} className="underline-offset-2 hover:underline" data-testid="result-title">
            {result.title}
          </Link>
        </h3>
        <span className="rounded-full border border-border px-2 py-0.5 text-xs" data-testid="version-badge">
          Version {result.version_number}
        </span>
        <span className="text-xs text-muted-foreground">
          {typeLabel(result.content_type)} · {workspaceName}
        </span>
      </div>
      <ul className="grid gap-2">
        {result.snippets.map((s, i) => {
          const page = pageLabel(s);
          const href = evidenceHref({
            orgSlug,
            workspaceRef: result.workspace_slug,
            documentId: result.document_id,
            versionId: result.version_id,
            chunkId: s.chunk_id,
            page: s.page_start,
          });
          return (
            <li key={`${s.evidence_id ?? "s"}-${i}`} className="grid gap-1 rounded-md bg-muted/30 p-3 text-sm" data-testid="search-snippet">
              <p className="break-words whitespace-pre-wrap">
                <HighlightedSnippet text={s.text} ranges={s.highlights} />
              </p>
              <p className="flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
                {page && <span data-testid="snippet-page">{page}</span>}
                <Link href={href} className="underline underline-offset-2 hover:text-foreground" data-testid="snippet-evidence-link">
                  Open passage
                  <span className="sr-only">
                    {" "}
                    from {result.title}
                    {page ? `, ${page}` : ""}
                  </span>
                </Link>
              </p>
            </li>
          );
        })}
      </ul>
    </li>
  );
}
