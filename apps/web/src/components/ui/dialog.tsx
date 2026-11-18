"use client"

import * as React from "react"
import { cn } from "cn"

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

type DialogProps = {
  open: boolean
  onClose: () => void
  title: string
  description?: string
  children: React.ReactNode
  className?: string
  /** Element to focus when the dialog opens (defaults to the first focusable control). */
  initialFocusRef?: React.RefObject<HTMLElement | null>
}

/**
 * Minimal accessible modal: `role="dialog"` + `aria-modal`, labelled by its title, focus moves in on open,
 * Tab cycles inside, Escape closes, and focus returns to whatever had it before opening.
 */
function Dialog({ open, onClose, title, description, children, className, initialFocusRef }: DialogProps) {
  const ref = React.useRef<HTMLDivElement>(null)
  const titleId = React.useId()
  const descId = React.useId()
  const onCloseRef = React.useRef(onClose)
  React.useEffect(() => {
    onCloseRef.current = onClose
  })

  React.useEffect(() => {
    if (!open) return
    const previous = document.activeElement as HTMLElement | null
    const root = ref.current
    const first = initialFocusRef?.current ?? root?.querySelector<HTMLElement>(FOCUSABLE) ?? root
    first?.focus()
    return () => {
      // Skip when focus has been deliberately moved elsewhere by the caller during close.
      if (previous && previous.isConnected && (document.activeElement === document.body || root?.contains(document.activeElement))) {
        previous.focus()
      }
    }
  }, [open, initialFocusRef])

  if (!open) return null

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.stopPropagation()
      onCloseRef.current()
      return
    }
    if (e.key !== "Tab" || !ref.current) return
    const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE))
    if (items.length === 0) return
    const firstEl = items[0]
    const lastEl = items[items.length - 1]
    if (e.shiftKey && document.activeElement === firstEl) {
      e.preventDefault()
      lastEl.focus()
    } else if (!e.shiftKey && document.activeElement === lastEl) {
      e.preventDefault()
      firstEl.focus()
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onKeyDown={onKeyDown}>
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={cn("grid w-full max-w-md gap-4 rounded-xl border border-border bg-popover p-5 text-popover-foreground shadow-lg outline-none", className)}
      >
        <div className="grid gap-1">
          <h2 id={titleId} className="text-base font-semibold">
            {title}
          </h2>
          {description && (
            <p id={descId} className="text-sm text-muted-foreground">
              {description}
            </p>
          )}
        </div>
        {children}
      </div>
    </div>
  )
}

export { Dialog }
