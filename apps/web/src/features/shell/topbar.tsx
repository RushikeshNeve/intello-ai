"use client";

import { useRef, useState } from "react";
import { ChevronRightIcon, CircleHelpIcon, LogOutIcon, MenuIcon } from "lucide-react";
import { Modal } from "@/components/ui/overlay";
import { Truncate } from "@/components/ui/truncate";
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { ThemeToggle } from "@/features/theme/theme-toggle";

/** `org / workspace` trail. The last crumb is the current location. */
export function Breadcrumb({ org, workspace }: { org: string; workspace: string | null }) {
  return (
    <nav aria-label="Breadcrumb" className="min-w-0">
      <ol className="flex min-w-0 items-center gap-1.5 text-sm text-text-secondary">
        <li className="min-w-0 max-w-[12rem]">
          <Truncate text={org} className={workspace ? "" : "font-semibold text-foreground"} />
        </li>
        {workspace && (
          <>
            <li aria-hidden="true">
              <ChevronRightIcon className="size-3.5" />
            </li>
            <li className="min-w-0 max-w-[14rem]" aria-current="page">
              <Truncate text={workspace} className="font-semibold text-foreground" />
            </li>
          </>
        )}
      </ol>
    </nav>
  );
}

/** Help: keyboard shortcuts and where to ask. No help centre exists yet, and the dialog says so. */
export function HelpButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} aria-haspopup="dialog" aria-label="Help" data-testid="help-button" className="focus-ring grid size-9 place-items-center rounded-control text-text-secondary hover:bg-muted">
        <CircleHelpIcon aria-hidden="true" className="size-[1.125rem]" />
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title="Help" description="Quick reference for working in Intello.">
        <dl className="grid gap-3 text-sm">
          <div className="flex items-center justify-between gap-4">
            <dt>Open the command palette</dt>
            <dd>
              <kbd className="rounded border border-border bg-muted px-1.5 py-0.5 text-meta">Ctrl</kbd> + <kbd className="rounded border border-border bg-muted px-1.5 py-0.5 text-meta">K</kbd>
            </dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt>Close a dialog or menu</dt>
            <dd>
              <kbd className="rounded border border-border bg-muted px-1.5 py-0.5 text-meta">Esc</kbd>
            </dd>
          </div>
        </dl>
        <p className="mt-4 text-sm text-text-secondary">A help centre is not available yet. Ask an administrator of your organization if you need access to something.</p>
      </Modal>
    </>
  );
}

/** Account menu: the signed-in user and sign-out (a real POST form, so the CSRF Origin check applies as before). */
export function UserMenu({ userLabel }: { userLabel: string }) {
  const form = useRef<HTMLFormElement>(null);
  const initial = (userLabel.trim()[0] ?? "?").toUpperCase();
  return (
    <>
      <form ref={form} method="post" action="/api/auth/logout" className="hidden" />
      <DropdownMenu>
        <DropdownMenuTrigger data-testid="user-menu" aria-label="Account menu" className="focus-ring grid size-9 place-items-center rounded-full bg-info-soft text-sm font-bold text-info hover:brightness-95">
          <span aria-hidden="true">{initial}</span>
          <span className="sr-only" data-testid="session-user">
            {userLabel}
          </span>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-64">
          <DropdownMenuGroup>
            <DropdownMenuLabel className="truncate">{userLabel}</DropdownMenuLabel>
          </DropdownMenuGroup>
          <DropdownMenuItem onClick={() => form.current?.requestSubmit()} data-testid="sign-out">
            <LogOutIcon aria-hidden="true" />
            Sign out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}

/** Top bar (69px; 61px on mobile). `menuControls` is the id of the sidebar the menu button opens. */
export function Topbar({
  breadcrumb,
  search,
  userLabel,
  menuControls,
  menuOpen,
  onMenu,
}: {
  breadcrumb: React.ReactNode;
  search: React.ReactNode;
  userLabel: string;
  menuControls: string;
  menuOpen: boolean;
  onMenu: () => void;
}) {
  return (
    <header className="sticky top-0 z-20 flex h-[61px] items-center gap-3 border-b border-border bg-background/90 px-[17px] backdrop-blur min-[721px]:h-[69px] min-[721px]:px-7 min-[1101px]:px-[42px]">
      <button
        type="button"
        onClick={onMenu}
        aria-label="Open navigation"
        aria-expanded={menuOpen}
        aria-controls={menuControls}
        data-testid="menu-button"
        className="focus-ring -ml-2 grid size-10 shrink-0 place-items-center rounded-control hover:bg-muted min-[721px]:hidden"
      >
        <MenuIcon aria-hidden="true" className="size-5" />
      </button>
      <div className="min-w-0 flex-1">{breadcrumb}</div>
      <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
        {search}
        <HelpButton />
        <ThemeToggle />
        <UserMenu userLabel={userLabel} />
      </div>
    </header>
  );
}
