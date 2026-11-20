import type { ComponentProps, ReactNode } from "react";
import { cn } from "cn";
import { Eyebrow } from "./page-header";

/** White card on the canvas: 16px radius, subtle border, sparse shadow. Pass `as="section"` etc. for landmarks. */
export function SurfaceCard({ as: Tag = "div", className, ...props }: ComponentProps<"div"> & { as?: "div" | "section" | "article" | "li" }) {
  return <Tag className={cn("rounded-card border border-border bg-card p-5 text-card-foreground shadow-card", className)} {...(props as object)} />;
}

/** Single figure with a label. `value` is rendered as given: pass real data only, or `"-"` when unknown. */
export function MetricCard({ label, value, hint, icon, className }: { label: string; value: ReactNode; hint?: ReactNode; icon?: ReactNode; className?: string }) {
  return (
    <SurfaceCard className={cn("grid gap-1.5", className)}>
      <div className="flex items-center justify-between gap-2">
        <Eyebrow>{label}</Eyebrow>
        {icon && <span aria-hidden="true" className="text-brand-blue">{icon}</span>}
      </div>
      <p className="text-3xl font-bold tracking-title text-brand-indigo dark:text-foreground">{value}</p>
      {hint && <p className="text-meta text-text-secondary">{hint}</p>}
    </SurfaceCard>
  );
}
