"use client";

import { useRouter } from "next/navigation";
import { Building2Icon, ChevronsUpDownIcon } from "lucide-react";
import { cn } from "cn";
import { buttonVariants } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ROLE_LABEL } from "../api/orgs";
import { useOrgScope } from "./org-scope";

/**
 * Header org switcher: a menu button (`aria-haspopup="menu"`) whose menu is a radio group of the caller's
 * orgs (`menuitemradio`, `aria-checked` on the current one). Keyboard: Enter/Space/ArrowDown opens,
 * arrows move, Enter selects, Escape closes and returns focus to the trigger (Base UI Menu).
 */
export function OrgSwitcher({ goHome }: { goHome?: () => void }) {
  const { displayedOrg, orgs, switchOrg } = useOrgScope();
  const router = useRouter();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        data-testid="org-switcher"
        className={cn(buttonVariants({ variant: "outline" }), "h-9 max-w-[16rem] gap-2 px-2.5")}
      >
        <Building2Icon aria-hidden="true" />
        <span className="sr-only">Current organization: </span>
        <span className="truncate font-medium" data-testid="current-org-name">
          {displayedOrg.name}
        </span>
        <span
          className="rounded-full bg-secondary px-1.5 py-0.5 text-xs font-normal text-secondary-foreground"
          data-testid="current-org-role"
        >
          <span className="sr-only">, role </span>
          {ROLE_LABEL[displayedOrg.role]}
        </span>
        <span className="sr-only">. Switch organization</span>
        <ChevronsUpDownIcon aria-hidden="true" className="text-muted-foreground" />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="w-64 motion-reduce:animate-none motion-reduce:transition-none"
      >
        <DropdownMenuGroup>
          <DropdownMenuLabel>Organizations</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={displayedOrg.id}
            onValueChange={(id: string) => {
              const target = orgs.find((o) => o.id === id);
              if (target) switchOrg(target);
            }}
          >
            {orgs.map((o) => (
              <DropdownMenuRadioItem key={o.id} value={o.id} data-testid={`org-option-${o.slug}`}>
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate">{o.name}</span>
                  <span className="text-xs text-muted-foreground">{ROLE_LABEL[o.role]}</span>
                </span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => (goHome ? goHome() : router.push("/home"))}>
          All organizations
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
