"use client";

import { useEffect, useState, type ReactNode } from "react";
import { AlertTriangleIcon, InboxIcon, WifiOffIcon } from "lucide-react";
import { cn } from "cn";

/**
 * Empty / error / offline states. Honest copy only: say what is missing and what the user can do, never invent data.
 * Headings default to `h2` (the page's `PageHeader` owns the `h1`); pass `as="h1"` only for full-page states.
 */
type Heading = "h1" | "h2" | "h3";

export function EmptyState({
  title,
  description,
  action,
  icon,
  as: H = "h2",
  className,
  testId,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  as?: Heading;
  className?: string;
  testId?: string;
}) {
  return (
    <div className={cn("grid justify-items-center gap-2 rounded-card border border-dashed border-border bg-card px-6 py-10 text-center", className)} data-testid={testId}>
      <span aria-hidden="true" className="grid size-11 place-items-center rounded-full bg-info-soft text-brand-blue">
        {icon ?? <InboxIcon className="size-5" />}
      </span>
      <H className="text-card-title font-semibold text-brand-indigo dark:text-foreground">{title}</H>
      {description && <p className="max-w-md text-sm text-text-secondary">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/** Recoverable failure. `role="alert"` so it is announced; `onRetry` renders a retry button when given. */
export function ErrorState({
  title = "Something went wrong",
  description,
  onRetry,
  as: H = "h2",
  className,
  testId,
}: {
  title?: string;
  description?: ReactNode;
  onRetry?: () => void;
  as?: Heading;
  className?: string;
  testId?: string;
}) {
  return (
    <div role="alert" className={cn("grid justify-items-center gap-2 rounded-card border border-danger/30 bg-danger-soft px-6 py-8 text-center", className)} data-testid={testId}>
      <AlertTriangleIcon aria-hidden="true" className="size-5 text-danger" />
      <H className="text-card-title font-semibold text-danger">{title}</H>
      {description && <p className="max-w-md text-sm text-foreground">{description}</p>}
      {onRetry && (
        <button type="button" onClick={onRetry} className="focus-ring mt-1 h-8 rounded-control border border-border bg-background px-3 text-sm font-medium hover:bg-muted">
          Try again
        </button>
      )}
    </div>
  );
}

/** Banner shown while the browser reports no network. Polite live region; renders nothing when online. */
export function OfflineBanner({ className }: { className?: string }) {
  const [online, setOnline] = useState(true);
  useEffect(() => {
    const sync = () => setOnline(navigator.onLine);
    sync();
    window.addEventListener("online", sync);
    window.addEventListener("offline", sync);
    return () => {
      window.removeEventListener("online", sync);
      window.removeEventListener("offline", sync);
    };
  }, []);
  if (online) return null;
  return (
    <div role="status" className={cn("flex items-center gap-2 bg-warning-soft px-4 py-2 text-sm text-warning", className)} data-testid="offline-banner">
      <WifiOffIcon aria-hidden="true" className="size-4 shrink-0" />
      You are offline. Changes may not be saved until the connection returns.
    </div>
  );
}
