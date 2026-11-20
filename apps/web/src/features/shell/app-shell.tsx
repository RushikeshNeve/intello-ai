"use client";

/**
 * The signed-in org shell: skip link, sidebar (drawer <= 720px), top bar, and the single `<main id="main">`.
 * Must render inside `OrgScopeProvider`. Feature pages render only their own content inside `children`; they never
 * add another `<main>`, sidebar or top-level padding (the shell provides 17/28/42px content padding).
 * While an org switch is in flight the workspace-dependent chrome and the content are replaced (no previous-org data).
 */
import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { OfflineBanner } from "@/components/ui/states";
import { CommandPalette } from "@/features/palette/command-palette";
import { ScopedContent, useOrgScope } from "@/features/organizations/components/org-scope";
import { useWorkspaces } from "@/features/workspaces/hooks";
import type { Workspace } from "@/features/workspaces/api";
import { SkipLink } from "./skip-link";
import { useCurrentWorkspace } from "./current-workspace";
import { buildNav } from "./nav-config";
import { Sidebar } from "./sidebar";
import { Breadcrumb, Topbar } from "./topbar";

export function AppShell({ userLabel, initialWorkspaces, children }: { userLabel: string; initialWorkspaces: Workspace[]; children: ReactNode }) {
  const { org, displayedOrg, switching } = useOrgScope();
  const { data: workspaces = initialWorkspaces } = useWorkspaces(org.id, initialWorkspaces);
  const { current, choose, onWorkspaceRoute } = useCurrentWorkspace(org.id, workspaces);
  const pathname = usePathname();
  const sidebarId = useId();
  const [open, setOpen] = useState(false);

  // Close the drawer after navigating (state adjusted during render, not in an effect).
  const [seen, setSeen] = useState(pathname);
  if (seen !== pathname) {
    setSeen(pathname);
    setOpen(false);
  }

  // Leaving drawer mode (>720px) while open must not leave a modal trap behind.
  useEffect(() => {
    if (!open) return;
    const mq = window.matchMedia("(min-width: 721px)");
    const onChange = () => mq.matches && setOpen(false);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [open]);

  const entries = useMemo(() => buildNav(org.slug, switching ? null : (current?.slug ?? null)), [org.slug, switching, current?.slug]);

  return (
    <div className="flex min-h-dvh flex-1" data-org-scope={org.id}>
      <SkipLink />
      <Sidebar
        id={sidebarId}
        open={open}
        onClose={() => setOpen(false)}
        entries={entries}
        workspaces={switching ? [] : workspaces}
        current={switching ? null : current}
        onChoose={choose}
        onWorkspaceRoute={onWorkspaceRoute}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar
          userLabel={userLabel}
          menuControls={sidebarId}
          menuOpen={open}
          onMenu={() => setOpen(true)}
          breadcrumb={<Breadcrumb org={displayedOrg.name} workspace={switching ? null : (current?.name ?? null)} />}
          search={<CommandPalette />}
        />
        <OfflineBanner />
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 px-[17px] py-6 outline-none min-[721px]:px-7 min-[721px]:py-8 min-[1101px]:px-[42px]">
          <ScopedContent>{children}</ScopedContent>
        </main>
      </div>
    </div>
  );
}
