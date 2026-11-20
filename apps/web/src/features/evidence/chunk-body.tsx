import type { EvidenceChunk, EvidenceErrorCode } from "./api";
import { CellView, LineView } from "./locator-views";

export const ERROR_COPY: Record<EvidenceErrorCode, { title: string; body: string; retry: boolean }> = {
  not_found: { title: "This passage isn't available", body: "It may have been removed, or your account may not have access to it.", retry: false },
  gone: {
    title: "The source text is no longer available",
    body: "The stored text for this passage can't be read any more. The document itself may still be open on the right.",
    retry: false,
  },
  unavailable: { title: "Temporarily unavailable", body: "The document store isn't answering right now. Your data is safe; try again in a moment.", retry: true },
  network: { title: "We couldn't reach the server", body: "Check your connection and try again.", retry: true },
  unauthenticated: { title: "Please sign in again", body: "Your session has ended.", retry: false },
  unknown: { title: "Something went wrong", body: "We couldn't load this passage. Try again.", retry: true },
};

export function pageLabel(start: number | null, end: number | null): string | null {
  if (start === null) return null;
  return end !== null && end !== start ? `pages ${start}–${end}` : `page ${start}`;
}

/** The passage itself, shared by the standalone evidence page and the chat evidence panel: line, cell or plain text view. */
export function ChunkBody({ chunk, highlight = false, maxHeightClass = "max-h-[65vh]" }: { chunk: EvidenceChunk; highlight?: boolean; maxHeightClass?: string }) {
  const line = chunk.locators.find((l) => l.kind === "line" && l.line_start !== null);
  const cell = chunk.locators.find((l) => l.kind === "cell");
  return (
    <>
      {chunk.heading_path.length > 0 && (
        <p className="text-xs text-muted-foreground" data-testid="evidence-headings">
          {chunk.heading_path.join(" › ")}
        </p>
      )}
      {line ? (
        <LineView text={chunk.text} startLine={line.line_start!} highlight={{ start: line.line_start!, end: line.line_end ?? line.line_start! }} />
      ) : cell ? (
        <CellView text={chunk.text} locator={cell} />
      ) : (
        <blockquote
          tabIndex={0}
          aria-label="Passage text"
          className={`${maxHeightClass} overflow-auto rounded-control border border-border bg-muted/30 p-4 text-sm leading-relaxed break-words whitespace-pre-wrap`}
          data-testid="evidence-text"
        >
          {highlight ? <mark className="rounded-sm bg-warning-soft px-0.5 text-inherit">{chunk.text}</mark> : chunk.text}
        </blockquote>
      )}
    </>
  );
}
