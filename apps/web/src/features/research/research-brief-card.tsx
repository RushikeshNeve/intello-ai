"use client";

import Link from "next/link";
import { ArrowRightIcon } from "lucide-react";
import { StatusBadge, type StatusTone } from "@/components/ui/badges";
import { SurfaceCard } from "@/components/ui/surface";
import { isTerminal, type ResearchListItem, type ResearchState } from "./api";

export const STATE_LABEL: Record<ResearchState, string> = { planned: "Queued", running: "Researching", completed: "Completed", failed: "Failed", cancelled: "Cancelled" };
export const STATE_TONE: Record<ResearchState, StatusTone> = { planned: "neutral", running: "info", completed: "success", failed: "danger", cancelled: "neutral" };

/** One saved investigation. Terminal sessions are labelled as such; in-flight ones say they keep updating. */
export function ResearchBriefCard({ item, href }: { item: ResearchListItem; href: string }) {
  const date = /^\d{4}-\d{2}-\d{2}/.test(item.created_at) ? item.created_at.slice(0, 10) : null;
  return (
    <SurfaceCard as="li" className="grid gap-2 p-4" data-testid="research-card" data-state={item.state}>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge tone={STATE_TONE[item.state]}>{STATE_LABEL[item.state]}</StatusBadge>
        {date && <span className="text-meta text-text-secondary">{date}</span>}
      </div>
      <Link href={href} className="focus-ring group flex items-start justify-between gap-3 rounded-control" data-testid="research-link">
        <span className="min-w-0 text-card-title font-semibold break-words text-brand-indigo dark:text-foreground">{item.question}</span>
        <ArrowRightIcon aria-hidden="true" className="mt-1 size-4 shrink-0 text-text-secondary group-hover:text-brand-blue" />
      </Link>
      <p className="text-meta text-text-secondary">{isTerminal(item.state) ? "Open the report and sources." : "Still running. Open to follow progress."}</p>
    </SurfaceCard>
  );
}
