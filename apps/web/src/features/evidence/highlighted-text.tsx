"use client";

import { useEffect, useMemo, useRef } from "react";
import { locateExcerpt } from "./locate";

/**
 * Document text with the evidence excerpt marked. Everything is a React text node (never HTML). When the excerpt
 * cannot be located the text is shown unmarked and `onResolved(false)` lets the viewer say so.
 */
export function HighlightedText({
  text,
  excerpt,
  onResolved,
}: {
  text: string;
  excerpt: string;
  onResolved?: (found: boolean) => void;
}) {
  const range = useMemo(() => locateExcerpt(text, excerpt), [text, excerpt]);
  const markRef = useRef<HTMLElement>(null);
  const resolvedRef = useRef(onResolved);
  useEffect(() => {
    resolvedRef.current = onResolved;
  });
  useEffect(() => {
    resolvedRef.current?.(range !== null);
    markRef.current?.scrollIntoView?.({ block: "center" });
  }, [range]);

  if (!range) return <>{text}</>;
  return (
    <>
      {text.slice(0, range.start)}
      <mark ref={markRef} className="rounded-sm bg-yellow-200 px-0.5 text-foreground dark:bg-yellow-500/40" data-testid="evidence-mark">
        {text.slice(range.start, range.end)}
      </mark>
      {text.slice(range.end)}
    </>
  );
}
