import { AlertTriangleIcon, BanIcon, CheckCircle2Icon, CircleDashedIcon, ClockIcon, FileXIcon, Loader2Icon, type LucideIcon } from "lucide-react";
import { StatusBadge, type StatusTone } from "@/components/ui/badges";
import type { IngestionSummary } from "./api";
import { badgeText, displayKind, type DisplayKind } from "./messages";

const ICONS: Record<DisplayKind, LucideIcon> = {
  none: CircleDashedIcon,
  queued: ClockIcon,
  processing: Loader2Icon,
  ready: CheckCircle2Icon,
  failed: AlertTriangleIcon,
  unsupported: BanIcon,
  no_text: FileXIcon,
  cancelled: BanIcon,
};

const TONES: Record<DisplayKind, StatusTone> = {
  none: "neutral",
  queued: "info",
  processing: "info",
  ready: "success",
  failed: "danger",
  unsupported: "neutral",
  no_text: "warning",
  cancelled: "neutral",
};

/** State is always conveyed as text and an icon, never by colour alone. */
export function IngestionBadge({ summary }: { summary: IngestionSummary | null | undefined }) {
  if (!summary) return null;
  const kind = displayKind(summary);
  const Icon = ICONS[kind];
  return (
    <span data-testid="ingestion-badge" data-state={kind} className="inline-flex">
      <StatusBadge tone={TONES[kind]}>
        <Icon aria-hidden="true" className={kind === "processing" ? "size-3 motion-safe:animate-spin" : "size-3"} />
        {badgeText(summary)}
      </StatusBadge>
    </span>
  );
}
