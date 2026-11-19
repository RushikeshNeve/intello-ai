import { AlertTriangleIcon, BanIcon, CheckCircle2Icon, CircleDashedIcon, ClockIcon, FileXIcon, Loader2Icon, type LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
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

const VARIANTS: Record<DisplayKind, "default" | "secondary" | "destructive" | "outline"> = {
  none: "outline",
  queued: "secondary",
  processing: "secondary",
  ready: "default",
  failed: "destructive",
  unsupported: "outline",
  no_text: "outline",
  cancelled: "outline",
};

/** State is always conveyed as text and an icon, never by colour alone. */
export function IngestionBadge({ summary }: { summary: IngestionSummary | null | undefined }) {
  if (!summary) return null;
  const kind = displayKind(summary);
  const Icon = ICONS[kind];
  return (
    <Badge variant={VARIANTS[kind]} data-testid="ingestion-badge" data-state={kind}>
      <Icon aria-hidden="true" className={kind === "processing" ? "motion-safe:animate-spin" : undefined} />
      {badgeText(summary)}
    </Badge>
  );
}
