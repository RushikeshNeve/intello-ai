"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { SearchIcon } from "lucide-react";
import { useOrgScope } from "@/features/organizations/components/org-scope";
import { useWorkspaces } from "@/features/workspaces/hooks";
import { buildItems, moveActive, type PaletteCtx } from "./model";

/** Ctrl/⌘+K command palette, mounted once in the org shell. Fully keyboard operable; focus stays in the dialog. */
export function CommandPalette() {
  const { org, orgs } = useOrgScope();
  const { data: workspaces = [] } = useWorkspaces(org.id);
  return <PaletteView ctx={{ orgSlug: org.slug, orgs, workspaces }} />;
}

export function PaletteView({ ctx, navigate }: { ctx: PaletteCtx; navigate?: (href: string) => void }) {
  const router = useRouter();
  const go = navigate ?? ((href: string) => router.push(href));
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const listId = useId();
  const items = useMemo(() => buildItems(ctx, query), [ctx, query]);

  function show() {
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery("");
    setActive(0);
    setOpen(true);
  }
  function hide() {
    setOpen(false);
    returnFocus.current?.focus?.();
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (open) hide();
        else show();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  function choose(i: number) {
    const item = items[i];
    if (!item) return;
    hide();
    go(item.href);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.preventDefault();
      hide();
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => moveActive(a, e.key === "ArrowDown" ? 1 : -1, items.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(Math.min(active, items.length - 1));
    } else if (e.key === "Tab") {
      // Focus trap: the input is the only tabbable control inside the dialog.
      e.preventDefault();
      inputRef.current?.focus();
    }
  }

  const activeId = items[active] ? `${listId}-${active}` : undefined;
  return (
    <>
      <button
        type="button"
        onClick={show}
        className="focus-ring inline-flex h-9 items-center gap-2 rounded-control border border-border bg-background px-2.5 text-sm text-text-secondary hover:bg-muted sm:w-56 min-[1101px]:w-72"
        aria-label="Open command palette"
        data-testid="palette-trigger"
      >
        <SearchIcon aria-hidden="true" className="size-4 shrink-0" />
        <span className="hidden flex-1 truncate text-left sm:inline">Search or jump to...</span>
        <kbd className="hidden rounded border border-border bg-muted px-1.5 text-meta sm:inline">Ctrl K</kbd>
      </button>
      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-[10vh]" onMouseDown={(e) => e.target === e.currentTarget && hide()} data-testid="palette-overlay">
          <div role="dialog" aria-modal="true" aria-label="Command palette" className="w-full max-w-lg rounded-lg border border-border bg-popover text-popover-foreground shadow-lg" onKeyDown={onKeyDown}>
            <input
              ref={inputRef}
              role="combobox"
              aria-expanded="true"
              aria-controls={listId}
              aria-activedescendant={activeId}
              aria-label="Type a command or search"
              placeholder="Type a command or search…"
              className="w-full rounded-t-lg border-b border-border bg-transparent px-3 py-2.5 text-sm outline-none"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setActive(0);
              }}
              data-testid="palette-input"
            />
            <ul id={listId} role="listbox" aria-label="Results" className="max-h-80 overflow-auto p-1">
              {items.length === 0 && <li className="px-3 py-2 text-sm text-muted-foreground">No matches.</li>}
              {items.map((it, i) => (
                <li
                  key={it.id}
                  id={`${listId}-${i}`}
                  role="option"
                  aria-selected={i === active}
                  className={`flex cursor-pointer items-center justify-between gap-2 rounded-md px-3 py-1.5 text-sm ${i === active ? "bg-accent text-accent-foreground" : ""}`}
                  onMouseMove={() => setActive(i)}
                  onClick={() => choose(i)}
                  data-testid="palette-option"
                >
                  <span className="min-w-0 break-words">{it.label}</span>
                  <span className="text-xs text-muted-foreground">{it.group}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </>
  );
}
