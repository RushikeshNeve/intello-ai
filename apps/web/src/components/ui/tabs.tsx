"use client";

/**
 * Controlled tabs (WAI-ARIA tabs pattern, automatic activation): ArrowLeft/Right/Home/End move and select, only the
 * selected tab is in the tab order. Panels are the caller's: render the panel with `role="tabpanel"`,
 * `id={tabPanelId(idBase, value)}` and `aria-labelledby={tabId(idBase, value)}`.
 */
import { useRef, type ReactNode } from "react";
import { cn } from "cn";

export type TabItem = { value: string; label: ReactNode; count?: number; disabled?: boolean };

export const tabId = (base: string, value: string) => `${base}-tab-${value}`;
export const tabPanelId = (base: string, value: string) => `${base}-panel-${value}`;

export function Tabs({ items, value, onChange, idBase, label, className }: { items: TabItem[]; value: string; onChange: (value: string) => void; idBase: string; label: string; className?: string }) {
  const refs = useRef(new Map<string, HTMLButtonElement>());
  const enabled = items.filter((i) => !i.disabled);

  function onKeyDown(e: React.KeyboardEvent) {
    const idx = enabled.findIndex((i) => i.value === value);
    let to: number | null = null;
    if (e.key === "ArrowRight") to = (idx + 1) % enabled.length;
    else if (e.key === "ArrowLeft") to = (idx - 1 + enabled.length) % enabled.length;
    else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = enabled.length - 1;
    if (to === null || enabled.length === 0) return;
    e.preventDefault();
    const target = enabled[to];
    onChange(target.value);
    refs.current.get(target.value)?.focus();
  }

  return (
    <div role="tablist" aria-label={label} onKeyDown={onKeyDown} className={cn("flex gap-1 overflow-x-auto border-b border-border", className)}>
      {items.map((t) => {
        const selected = t.value === value;
        return (
          <button
            key={t.value}
            ref={(el) => {
              if (el) refs.current.set(t.value, el);
              else refs.current.delete(t.value);
            }}
            type="button"
            role="tab"
            id={tabId(idBase, t.value)}
            aria-selected={selected}
            aria-controls={tabPanelId(idBase, t.value)}
            tabIndex={selected ? 0 : -1}
            disabled={t.disabled}
            onClick={() => onChange(t.value)}
            className={cn(
              "focus-ring -mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium whitespace-nowrap transition-colors disabled:opacity-50",
              selected ? "border-brand-blue text-info" : "border-transparent text-text-secondary hover:text-foreground",
            )}
          >
            {t.label}
            {t.count !== undefined && <span className="rounded-full bg-muted px-1.5 text-meta text-text-secondary">{t.count}</span>}
          </button>
        );
      })}
    </div>
  );
}
