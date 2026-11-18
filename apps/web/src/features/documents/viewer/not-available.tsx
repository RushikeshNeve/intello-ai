import type { ReactNode } from "react";
import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";

/**
 * Calm "not available" state for a missing/deleted/unauthorized document or version. Deliberately says
 * nothing about WHY (404, 410, 403 and deletion are indistinguishable to the user).
 */
export function NotAvailable({
  backHref,
  backLabel = "Back to documents",
  headingLevel = 1,
  children,
}: {
  backHref: string;
  backLabel?: string;
  headingLevel?: 1 | 2;
  children?: ReactNode;
}) {
  const Heading = headingLevel === 1 ? "h1" : "h2";
  return (
    <div className="grid justify-items-start gap-3 rounded-lg border border-dashed border-border p-6" data-testid="document-unavailable">
      <Heading tabIndex={-1} className="text-lg font-semibold tracking-tight outline-none">
        This document isn&apos;t available
      </Heading>
      <p className="max-w-md text-sm text-muted-foreground">
        It may have been removed, or your account may not have access to it.
      </p>
      {children}
      <Link href={backHref} className={buttonVariants({ variant: "outline" })}>
        {backLabel}
      </Link>
    </div>
  );
}
