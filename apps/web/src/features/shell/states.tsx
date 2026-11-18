import type { ReactNode } from "react";
import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";

/** `standalone` renders the page's own `<main>`; otherwise the content sits inside an existing shell `<main>`. */
function StateFrame({ standalone, testId, children }: { standalone: boolean; testId: string; children: ReactNode }) {
  const className = "flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center outline-none";
  return standalone ? (
    <main id="main" className={className} data-testid={testId}>
      {children}
    </main>
  ) : (
    <div className={className} data-testid={testId}>
      {children}
    </div>
  );
}

/**
 * One calm page for "this org/workspace does not exist" and "you may not see it": the two are never
 * distinguished, and nothing about the requested scope is echoed back.
 */
export function ScopeNotFound({
  standalone = true,
  backHref = "/home",
  backLabel = "Go to your organizations",
}: {
  standalone?: boolean;
  backHref?: string;
  backLabel?: string;
}) {
  return (
    <StateFrame standalone={standalone} testId="scope-not-found">
      <h1 tabIndex={-1} className="text-xl font-semibold tracking-tight outline-none">
        Not found or no access
      </h1>
      <p className="max-w-md text-sm text-muted-foreground">
        This page doesn&apos;t exist, or your account doesn&apos;t have access to it. If you think you should have
        access, ask an administrator of the organization to invite you.
      </p>
      <Link href={backHref} className={buttonVariants({ variant: "outline" })}>
        {backLabel}
      </Link>
    </StateFrame>
  );
}

export function ServiceUnavailable({ standalone = true }: { standalone?: boolean }) {
  return (
    <StateFrame standalone={standalone} testId="service-unavailable">
      <h1 tabIndex={-1} className="text-xl font-semibold tracking-tight outline-none">
        Temporarily unavailable
      </h1>
      <p className="max-w-md text-sm text-muted-foreground">
        We couldn&apos;t load this page right now. Your data is safe; please try again in a moment.
      </p>
    </StateFrame>
  );
}

export function PageLoading({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" className="flex flex-1 items-center justify-center p-8 text-sm text-muted-foreground">
      {label}
    </div>
  );
}
