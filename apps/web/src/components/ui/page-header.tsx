import type { ReactNode } from "react";
import { cn } from "cn";

/** Small uppercase label above a title or card ("KNOWLEDGE", "SOURCES"). Decorative hierarchy only. */
export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("text-[0.6875rem] font-bold tracking-[0.14em] text-text-secondary uppercase", className)}>{children}</p>;
}

/**
 * Page title block. Renders the page's single `<h1>` with `tabIndex={-1}` so `RouteFocus` can move focus to it after
 * navigation. `actions` sits on the right (wraps under the title on narrow screens).
 */
export function PageHeader({
  title,
  eyebrow,
  description,
  actions,
  className,
  titleTestId,
}: {
  title: ReactNode;
  eyebrow?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
  titleTestId?: string;
}) {
  return (
    <header className={cn("flex flex-wrap items-end justify-between gap-x-6 gap-y-3", className)}>
      <div className="grid min-w-0 gap-1.5">
        {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
        <h1 tabIndex={-1} data-testid={titleTestId} className="text-2xl font-bold tracking-title text-brand-indigo outline-none sm:text-title dark:text-foreground">
          {title}
        </h1>
        {description && <p className="max-w-prose text-sm text-text-secondary">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}
