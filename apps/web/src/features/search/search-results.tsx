import Link from "next/link";
import { FileTypeBadge, ScopeBadge, StatusBadge } from "@/components/ui/badges";
import { SurfaceCard } from "@/components/ui/surface";
import { documentHref, evidenceHref } from "@/features/evidence/links";
import type { SearchResult, SearchSnippet } from "./api";
import { segmentText } from "./highlight";

/** Snippet text with `<mark>` around the highlighted ranges. React text nodes only: no HTML is ever parsed. */
export function HighlightedSnippet({ text, ranges }: { text: string; ranges: readonly (readonly number[])[] }) {
  return (
    <>
      {segmentText(text, ranges).map((seg, i) =>
        seg.mark ? (
          <mark key={i} className="rounded-sm bg-warning-soft px-0.5 font-semibold text-foreground">
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

/** One ranked document with its matching passages. The scope badge is always "workspace": results are workspace documents. */
export function SearchResultRow({
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
    <SurfaceCard as="li" className="grid gap-3 p-4" data-testid="search-result" data-document-id={result.document_id}>
      <div className="flex items-start gap-3">
        <FileTypeBadge type={result.content_type} className="mt-0.5 shrink-0" />
        <div className="grid min-w-0 gap-1">
          <h3 className="text-card-title font-semibold break-words text-brand-indigo dark:text-foreground">
            <Link href={docLink} className="focus-ring rounded underline-offset-2 hover:underline" data-testid="result-title">
              {result.title}
            </Link>
          </h3>
          <div className="flex flex-wrap items-center gap-1.5 text-meta text-text-secondary">
            <span data-testid="version-badge">
              <StatusBadge tone="neutral">Version {result.version_number}</StatusBadge>
            </span>
            <ScopeBadge scope="workspace" />
            <span>{workspaceName}</span>
          </div>
        </div>
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
            <li key={`${s.evidence_id ?? "s"}-${i}`} className="grid gap-1 rounded-control border border-border-subtle bg-canvas p-3 text-sm" data-testid="search-snippet">
              <p className="break-words whitespace-pre-wrap">
                <HighlightedSnippet text={s.text} ranges={s.highlights} />
              </p>
              <p className="flex flex-wrap items-center gap-x-3 text-meta text-text-secondary">
                {page && <span data-testid="snippet-page">{page}</span>}
                <Link href={href} className="focus-ring rounded font-medium text-brand-blue underline underline-offset-2" data-testid="snippet-evidence-link">
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
    </SurfaceCard>
  );
}

/** Back-compat name. */
export const SearchResultCard = SearchResultRow;
