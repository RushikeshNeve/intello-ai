"use client";

import { useRef } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  ActivityIcon,
  BrainIcon,
  ChevronsUpDownIcon,
  FlaskConicalIcon,
  FolderIcon,
  HomeIcon,
  LibraryIcon,
  MessageSquareIcon,
  PlugIcon,
  SearchIcon,
  SettingsIcon,
  ShareIcon,
  UsersIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import { cn } from "cn";
import { BrandLockup } from "@/components/brand/brand";
import { Eyebrow } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { useFocusTrap } from "@/components/ui/overlay";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { OrgSwitcher } from "@/features/organizations/components/org-switcher";
import { useOrgScope } from "@/features/organizations/components/org-scope";
import type { Workspace } from "@/features/workspaces/api";
import { isActive, type NavEntry, type NavIcon } from "./nav-config";

const ICONS: Record<NavIcon, LucideIcon> = {
  home: HomeIcon,
  search: SearchIcon,
  ask: MessageSquareIcon,
  library: LibraryIcon,
  graph: ShareIcon,
  research: FlaskConicalIcon,
  memory: BrainIcon,
  integrations: PlugIcon,
  activity: ActivityIcon,
  people: UsersIcon,
  settings: SettingsIcon,
};

const itemBase =
  "focus-ring flex min-h-10 items-center gap-3 rounded-control px-3 py-2 text-[0.8125rem] font-medium transition-colors duration-[var(--duration-base)]";

/** Grouped primary navigation. Active link carries `aria-current="page"`; unresolved entries render disabled. */
export function SidebarNav({ entries }: { entries: NavEntry[] }) {
  const pathname = usePathname();
  const groups: { id: NavEntry["group"]; label: string }[] = [
    { id: "knowledge", label: "Knowledge" },
    { id: "manage", label: "Manage" },
  ];
  return (
    <nav aria-label="Primary" className="grid gap-4" data-testid="sidebar-nav">
      {groups.map((g) => {
        const items = entries.filter((e) => e.group === g.id);
        if (items.length === 0) return null;
        return (
          <div key={g.id}>
            <Eyebrow className="px-3 pb-1.5 text-[0.625rem]" >{g.label}</Eyebrow>
            <ul className="grid gap-0.5">
              {items.map((e) => {
                const Icon = ICONS[e.icon];
                const active = isActive(e, pathname);
                return (
                  <li key={e.id}>
                    {e.href ? (
                      <Link
                        href={e.href}
                        aria-current={active ? "page" : undefined}
                        data-testid={`nav-${e.id}`}
                        className={cn(itemBase, "text-sidebar-foreground hover:bg-muted", active && "bg-sidebar-accent font-semibold text-sidebar-accent-foreground hover:bg-sidebar-accent")}
                      >
                        <Icon aria-hidden="true" className="size-[1.0625rem] shrink-0" />
                        <span className="truncate">{e.label}</span>
                      </Link>
                    ) : (
                      <span aria-disabled="true" data-testid={`nav-${e.id}`} title="Create a workspace to use this section" className={cn(itemBase, "cursor-not-allowed text-text-secondary opacity-60")}>
                        <Icon aria-hidden="true" className="size-[1.0625rem] shrink-0" />
                        <span className="truncate">{e.label}</span>
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}

/**
 * Picks the workspace that Library/Graph/management links point to. Choice is remembered per org (see
 * current-workspace.ts). On a workspace route it also moves to the same section of the chosen workspace.
 */
export function WorkspaceSwitcher({
  workspaces,
  current,
  onChoose,
  onWorkspaceRoute,
}: {
  workspaces: readonly Workspace[];
  current: Workspace | null;
  onChoose: (slug: string) => void;
  onWorkspaceRoute: boolean;
}) {
  const { org, switching } = useOrgScope();
  const router = useRouter();
  const pathname = usePathname();
  if (switching) return <Skeleton className="mx-3 mb-4 h-11" />;
  if (workspaces.length === 0 || !current) {
    return (
      <p className="mx-3 mb-4 rounded-control border border-dashed border-border px-3 py-2 text-meta text-text-secondary" data-testid="workspace-switcher-empty">
        No workspaces yet.
      </p>
    );
  }

  function choose(slug: string) {
    if (slug === current?.slug) return;
    onChoose(slug);
    if (onWorkspaceRoute) {
      // Keep the section (documents, graph, ...) but never a document id that belongs to the other workspace.
      const base = `/o/${encodeURIComponent(org.slug)}/w/`;
      const rest = pathname.startsWith(base) ? pathname.slice(base.length).split("/").slice(1, 2).join("/") : "";
      router.push(`${base}${encodeURIComponent(slug)}${rest ? `/${rest}` : ""}`);
    }
  }

  return (
    <div className="mx-3 mb-4">
      <DropdownMenu>
        <DropdownMenuTrigger
          data-testid="workspace-switcher"
          className="focus-ring flex h-11 w-full items-center gap-2.5 rounded-control border border-border bg-background px-2.5 text-left hover:bg-muted"
        >
          <FolderIcon aria-hidden="true" className="size-4 shrink-0 text-brand-violet" />
          <span className="min-w-0 flex-1">
            <span className="block text-[0.625rem] leading-none font-semibold tracking-wide text-text-secondary uppercase">Workspace</span>
            <span className="block truncate text-[0.8125rem] font-semibold" data-testid="current-workspace-name">
              {current.name}
            </span>
          </span>
          <ChevronsUpDownIcon aria-hidden="true" className="size-4 shrink-0 text-text-secondary" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-64">
          <DropdownMenuGroup>
            <DropdownMenuLabel>Workspaces</DropdownMenuLabel>
            <DropdownMenuRadioGroup value={current.slug} onValueChange={(slug: string) => choose(slug)}>
              {workspaces.map((w) => (
                <DropdownMenuRadioItem key={w.id} value={w.slug} data-testid={`workspace-option-${w.slug}`}>
                  <span className="truncate">{w.name}</span>
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

/** Usage/plan block. Disabled placeholder: there is no usage endpoint yet, so it shows no numbers. */
export function UsageBlock() {
  return (
    <div className="rounded-card border border-border bg-muted/60 p-3" aria-disabled="true" data-testid="usage-block">
      <Eyebrow className="text-[0.625rem]">Usage &amp; plan</Eyebrow>
      <p className="mt-1 text-meta text-text-secondary">Usage and plan details will appear here once they are available.</p>
      <button type="button" disabled className="mt-2 h-8 w-full cursor-not-allowed rounded-control border border-border bg-background text-meta font-semibold text-text-secondary opacity-60">
        Not available yet
      </button>
    </div>
  );
}

/**
 * The sidebar landmark. >= 721px it is a sticky column (254px, 218px up to 1100px); <= 720px it is a true drawer:
 * off-screen and not focusable until `open`, then modal (focus trapped, Escape closes, focus returns to the menu button).
 */
export function Sidebar({
  id,
  open,
  onClose,
  entries,
  workspaces,
  current,
  onChoose,
  onWorkspaceRoute,
}: {
  id: string;
  open: boolean;
  onClose: () => void;
  entries: NavEntry[];
  workspaces: readonly Workspace[];
  current: Workspace | null;
  onChoose: (slug: string) => void;
  onWorkspaceRoute: boolean;
}) {
  const ref = useRef<HTMLElement>(null);
  useFocusTrap(ref, open, onClose);

  return (
    <>
      {open && <div className="fixed inset-0 z-30 bg-brand-indigo/40 min-[721px]:hidden" aria-hidden="true" onClick={onClose} data-testid="sidebar-backdrop" />}
      <aside
        ref={ref}
        id={id}
        data-open={open}
        aria-label={open ? "Navigation" : "Sidebar"}
        {...(open ? { role: "dialog", "aria-modal": true } : {})}
        tabIndex={-1}
        className={cn(
          "flex flex-col overflow-y-auto border-r border-sidebar-border bg-sidebar text-sidebar-foreground outline-none",
          "fixed inset-y-0 left-0 z-40 w-[min(86vw,19rem)] transition-[transform,visibility] duration-[var(--duration-panel)] ease-standard",
          "max-[720px]:data-[open=false]:invisible max-[720px]:data-[open=false]:-translate-x-full",
          "min-[721px]:sticky min-[721px]:top-0 min-[721px]:h-dvh min-[721px]:w-[218px] min-[721px]:shrink-0 min-[1101px]:w-[254px]",
        )}
      >
        <div className="flex items-center justify-between gap-2 px-5 pt-5 pb-5">
          <Link href="/home" className="focus-ring rounded-md" aria-label="Intello home">
            <BrandLockup className="h-9" />
          </Link>
          <button type="button" onClick={onClose} aria-label="Close navigation" className="focus-ring grid size-9 place-items-center rounded-control text-text-secondary hover:bg-muted min-[721px]:hidden">
            <XIcon aria-hidden="true" className="size-5" />
          </button>
        </div>
        <div className="mx-3 mb-3">
          <OrgSwitcher className="h-auto min-h-11 w-full max-w-none justify-between py-1.5" />
        </div>
        <WorkspaceSwitcher workspaces={workspaces} current={current} onChoose={onChoose} onWorkspaceRoute={onWorkspaceRoute} />
        <div className="flex-1 px-2">
          <SidebarNav entries={entries} />
        </div>
        <div className="p-3">
          <UsageBlock />
        </div>
      </aside>
    </>
  );
}
