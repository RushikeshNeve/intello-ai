import type { ElementType, ReactNode } from "react";
import { cn } from "cn";

/**
 * Accessible truncation (no tooltip library): visually clips with an ellipsis (one line, or `lines` lines), but the
 * FULL text stays in the DOM (screen readers read it all) and in the native `title` so sighted users can recover it.
 * Only for text that is also readable in full elsewhere (detail page); never truncate the only copy of data.
 * The parent must be allowed to shrink (`min-w-0`).
 */
export function Truncate({ children, text, lines = 1, as: Tag = "span", className }: { children?: ReactNode; text: string; lines?: 1 | 2 | 3; as?: ElementType; className?: string }) {
  const clamp = lines === 1 ? "truncate" : lines === 2 ? "line-clamp-2" : "line-clamp-3";
  return (
    <Tag title={text} className={cn("block min-w-0", clamp, className)}>
      {children ?? text}
    </Tag>
  );
}
