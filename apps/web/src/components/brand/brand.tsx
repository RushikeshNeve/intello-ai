/**
 * Brand components. The SVGs in `public/brand/` are the approved canonical files: used as-is, never recoloured.
 * `BrandLockup` shows the light lockup in light mode and the dark lockup (which carries its own navy plate) in dark
 * mode; the swap is pure CSS (`dark:` variant) so there is no flash and no client JS.
 */
import { cn } from "cn";

/** The prism mark alone (decorative unless `title` is given). */
export function BrandMark({ className, title }: { className?: string; title?: string }) {
  return (
    // eslint-disable-next-line @next/next/no-img-element -- static brand SVG, nothing to optimise
    <img
      src="/brand/intello-prism.svg"
      alt={title ?? ""}
      aria-hidden={title ? undefined : true}
      className={cn("size-8 shrink-0", className)}
      width={32}
      height={31}
    />
  );
}

/** Mark + wordmark. Accessible name "Intello". Size it with `className` on the height (e.g. `h-9`). */
export function BrandLockup({ className }: { className?: string }) {
  const base = cn("h-9 w-auto", className);
  return (
    <span className="inline-flex" data-testid="brand-lockup">
      {/* eslint-disable-next-line @next/next/no-img-element -- static brand SVG */}
      <img src="/brand/intello-lockup-light.svg" alt="Intello" className={cn(base, "dark:hidden")} width={131} height={36} />
      {/* eslint-disable-next-line @next/next/no-img-element -- static brand SVG */}
      <img src="/brand/intello-lockup-dark.svg" alt="Intello" className={cn(base, "hidden dark:block")} width={131} height={36} />
    </span>
  );
}
