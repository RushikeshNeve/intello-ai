import { evidenceHref, workspaceRefFor } from "@/features/evidence/links";
import type { Workspace } from "@/features/workspaces/api";
import type { Citation } from "./api";

/** The evidence-viewer link for exactly this citation: its document, version and chunk (never "latest"). */
export function citationHref(c: Citation, orgSlug: string, workspaces: readonly Pick<Workspace, "id" | "slug">[]): string {
  return evidenceHref({
    orgSlug,
    workspaceRef: workspaceRefFor(workspaces, c.workspace_id),
    documentId: c.document_id,
    versionId: c.version_id,
    chunkId: c.chunk_id,
    page: c.page_start,
  });
}

export function citationLabel(c: Citation): string {
  const pages = c.page_start === null ? "" : c.page_end !== null && c.page_end !== c.page_start ? `, pages ${c.page_start}–${c.page_end}` : `, page ${c.page_start}`;
  const version = c.version_number === null ? "" : `, version ${c.version_number}`;
  return `Source ${c.marker.slice(1, -1)}: ${c.title || "document"}${version}${pages}`;
}

export function citationByMarker(citations: readonly Citation[]): Map<string, Citation> {
  const map = new Map<string, Citation>();
  for (const c of citations) if (!map.has(c.marker)) map.set(c.marker, c);
  return map;
}
