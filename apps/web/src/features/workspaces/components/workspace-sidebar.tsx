"use client";

import { useId, useState } from "react";
import Link from "next/link";
import { useParams, usePathname } from "next/navigation";
import { LayoutGridIcon, MenuIcon, XIcon, FolderIcon } from "lucide-react";
import { cn } from "cn";
import { useOrgScope } from "@/features/organizations/components/org-scope";
import { useWorkspaces } from "../hooks";
import type { Workspace } from "../api";

const itemClass =
  "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-3 focus-visible:ring-sidebar-ring/50 motion-safe:transition-colors aria-[current=page]:bg-sidebar-accent aria-[current=page]:font-medium aria-[current=page]:text-sidebar-accent-foreground";

/**
 * Workspace navigation for the current org. The active link carries `aria-current="page"`. Below the `md`
 * breakpoint the list collapses behind a disclosure button.
 */
export function WorkspaceSidebar({ initialWorkspaces }: { initialWorkspaces: Workspace[] }) {
  const { org } = useOrgScope();
  const params = useParams<{ workspaceSlug?: string }>();
  const pathname = usePathname();
  const { data: workspaces = [], isError } = useWorkspaces(org.id, initialWorkspaces);
  const [open, setOpen] = useState(false);
  const navId = useId();
  const active = params?.workspaceSlug ? decodeURIComponent(params.workspaceSlug) : null;
  const base = `/o/${encodeURIComponent(org.slug)}`;

  // Close the mobile drawer after navigating (state adjusted during render, not in an effect).
  const [openedAt, setOpenedAt] = useState(pathname);
  if (openedAt !== pathname) {
    setOpenedAt(pathname);
    setOpen(false);
  }

  return (
    <div className="border-b border-sidebar-border bg-sidebar text-sidebar-foreground md:w-60 md:shrink-0 md:border-r md:border-b-0">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-4 py-2 text-sm font-medium outline-none focus-visible:ring-3 focus-visible:ring-sidebar-ring/50 md:hidden"
        aria-expanded={open}
        aria-controls={navId}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? <XIcon aria-hidden="true" className="size-4" /> : <MenuIcon aria-hidden="true" className="size-4" />}
        Workspaces
      </button>
      <nav id={navId} aria-label="Workspaces" className={cn("px-3 pb-4 md:block md:pt-4", open ? "block" : "hidden")}>
        <ul className="grid gap-0.5">
          <li>
            <Link href={base} className={itemClass} aria-current={active === null && pathname === base ? "page" : undefined}>
              <LayoutGridIcon aria-hidden="true" className="size-4" />
              Overview
            </Link>
          </li>
        </ul>
        <h2 className="mt-4 mb-1 px-2 text-xs font-medium text-muted-foreground">Workspaces</h2>
        {isError && <p className="px-2 text-xs text-muted-foreground">Workspaces could not be refreshed.</p>}
        {workspaces.length === 0 ? (
          <p className="px-2 text-xs text-muted-foreground">No workspaces yet.</p>
        ) : (
          <ul className="grid gap-0.5" data-testid="workspace-nav">
            {workspaces.map((w) => (
              <li key={w.id}>
                <Link
                  href={`${base}/w/${encodeURIComponent(w.slug)}`}
                  className={itemClass}
                  aria-current={active === w.slug ? "page" : undefined}
                >
                  <FolderIcon aria-hidden="true" className="size-4" />
                  <span className="truncate">{w.name}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </nav>
    </div>
  );
}
