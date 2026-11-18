import { cn } from "cn"

/** Minimal accessible progress bar (no motion when the user prefers reduced motion). */
function Progress({
  value,
  label,
  className,
}: {
  value: number
  label: string
  className?: string
}) {
  const pct = Math.max(0, Math.min(100, Math.round(value)))
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      data-slot="progress"
      className={cn("h-1.5 w-full overflow-hidden rounded-full bg-muted", className)}
    >
      <div
        className="h-full bg-primary transition-[width] duration-200 motion-reduce:transition-none"
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}

export { Progress }
