"use client";

/**
 * Toasts: mount `<ToastProvider>` once (done in the signed-in `(app)/layout.tsx`), then `const { push } = useToast()`.
 * Messages appear in a polite live region (`role="status"`), auto-dismiss after `durationMs` (default 5 s, pass 0 to
 * keep until dismissed) and can always be dismissed with the close button. Never the only place an error is shown.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { XIcon } from "lucide-react";
import { cn } from "cn";

export type ToastTone = "info" | "success" | "danger";
export type ToastInput = { title: string; description?: string; tone?: ToastTone; durationMs?: number };
type ToastItem = ToastInput & { id: number };

const ToastContext = createContext<{ push: (t: ToastInput) => number; dismiss: (id: number) => void } | null>(null);

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}

const TONE: Record<ToastTone, string> = {
  info: "border-border",
  success: "border-success/40",
  danger: "border-danger/40",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const next = useRef(1);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    const t = timers.current.get(id);
    if (t) clearTimeout(t);
    timers.current.delete(id);
    setItems((xs) => xs.filter((x) => x.id !== id));
  }, []);

  const push = useCallback(
    (t: ToastInput) => {
      const id = next.current++;
      setItems((xs) => [...xs, { ...t, id }].slice(-4));
      const ms = t.durationMs ?? 5000;
      if (ms > 0) timers.current.set(id, setTimeout(() => dismiss(id), ms));
      return id;
    },
    [dismiss],
  );

  useEffect(() => {
    const live = timers.current;
    return () => live.forEach((t) => clearTimeout(t));
  }, []);

  const value = useMemo(() => ({ push, dismiss }), [push, dismiss]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div role="status" aria-live="polite" className="pointer-events-none fixed right-4 bottom-4 z-[60] grid w-[min(92vw,22rem)] gap-2" data-testid="toast-region">
        {items.map((t) => (
          <div key={t.id} className={cn("pointer-events-auto flex items-start gap-3 rounded-control border bg-popover p-3 text-popover-foreground shadow-pop motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-2", TONE[t.tone ?? "info"])} data-testid="toast">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold">{t.title}</p>
              {t.description && <p className="mt-0.5 text-sm text-text-secondary">{t.description}</p>}
            </div>
            <button type="button" aria-label="Dismiss notification" onClick={() => dismiss(t.id)} className="focus-ring grid size-6 shrink-0 place-items-center rounded-md text-text-secondary hover:bg-muted">
              <XIcon aria-hidden="true" className="size-3.5" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
