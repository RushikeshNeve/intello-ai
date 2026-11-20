"use client";

/**
 * Accessible overlays: `useFocusTrap` (shared with the shell's mobile drawer), `Modal` (centered) and `Drawer`
 * (edge sheet). All three: `role="dialog"` + `aria-modal`, focus moves inside on open, Tab/Shift+Tab are trapped,
 * Escape closes, focus returns to the element that opened it, and the page behind does not scroll.
 * Controlled: the caller owns `open` and `onClose`. `title` is required (it is the dialog's accessible name).
 */
import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { XIcon } from "lucide-react";
import { cn } from "cn";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * While `active`: focus the first focusable (or the container), trap Tab inside `ref`, call `onEscape` on Escape,
 * and restore focus to the previously focused element when it turns inactive or unmounts.
 */
export function useFocusTrap(ref: RefObject<HTMLElement | null>, active: boolean, onEscape?: () => void) {
  const escape = useRef(onEscape);
  useEffect(() => {
    escape.current = onEscape;
  });
  useEffect(() => {
    if (!active) return;
    const root = ref.current;
    if (!root) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const items = () => Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
    (items()[0] ?? root).focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        escape.current?.();
        return;
      }
      if (e.key !== "Tab") return;
      const list = items();
      if (list.length === 0) {
        e.preventDefault();
        root.focus();
        return;
      }
      const first = list[0];
      const last = list[list.length - 1];
      const current = document.activeElement;
      if (e.shiftKey && (current === first || !root.contains(current))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (current === last || !root.contains(current))) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      previous?.focus?.();
    };
  }, [ref, active]);
}

function useScrollLock(active: boolean) {
  useEffect(() => {
    if (!active) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [active]);
}

type OverlayProps = {
  open: boolean;
  onClose: () => void;
  /** Accessible name and visible heading. */
  title: string;
  /** Optional supporting text under the title. */
  description?: string;
  children?: ReactNode;
  /** Footer actions (buttons). */
  footer?: ReactNode;
  className?: string;
  "data-testid"?: string;
};

function Sheet({ open, onClose, title, description, children, footer, className, side, ...rest }: OverlayProps & { side?: "left" | "right" }) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descId = useId();
  useFocusTrap(ref, open, onClose);
  useScrollLock(open);
  if (!open || typeof document === "undefined") return null;
  const isDrawer = side !== undefined;
  return createPortal(
    <div
      className={cn("fixed inset-0 z-50 flex bg-brand-indigo/40", isDrawer ? (side === "left" ? "justify-start" : "justify-end") : "items-center justify-center p-4")}
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
      data-testid={rest["data-testid"] ? `${rest["data-testid"]}-overlay` : undefined}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        data-testid={rest["data-testid"]}
        className={cn(
          "flex max-h-full flex-col bg-popover text-popover-foreground shadow-pop outline-none",
          "motion-safe:animate-in motion-safe:duration-200",
          isDrawer
            ? cn("h-full w-[min(92vw,26rem)] border-border", side === "left" ? "border-r motion-safe:slide-in-from-left" : "border-l motion-safe:slide-in-from-right")
            : "w-full max-w-lg rounded-card border border-border motion-safe:fade-in motion-safe:zoom-in-95",
          className,
        )}
      >
        <div className="flex items-start gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-section font-semibold tracking-tight">
              {title}
            </h2>
            {description && (
              <p id={descId} className="mt-0.5 text-sm text-muted-foreground">
                {description}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="focus-ring -mr-1 grid size-8 shrink-0 place-items-center rounded-control text-muted-foreground hover:bg-muted"
          >
            <XIcon aria-hidden="true" className="size-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-5 py-4">{children}</div>
        {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-border px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

/** Centered modal dialog. */
export function Modal(props: OverlayProps) {
  return <Sheet {...props} />;
}

/** Edge drawer (right by default). */
export function Drawer({ side = "right", ...props }: OverlayProps & { side?: "left" | "right" }) {
  return <Sheet {...props} side={side} />;
}
