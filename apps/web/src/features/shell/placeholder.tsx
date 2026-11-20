import { notFound } from "next/navigation";
import { ConstructionIcon } from "lucide-react";
import { EmptyState } from "@/components/ui/states";
import { PageHeader } from "@/components/ui/page-header";
import { SHOW_REDESIGN_PLACEHOLDERS } from "./nav-config";

/**
 * Honest placeholder for a management screen that a later redesign track will build (Integrations, Activity, People,
 * Settings). Shows no data. The owning track REPLACES the route's `page.tsx`; this file and the flag are then unused
 * for that route. With `SHOW_REDESIGN_PLACEHOLDERS = false` the route 404s.
 */
export function PlaceholderScreen({ title, description }: { title: string; description: string }) {
  if (!SHOW_REDESIGN_PLACEHOLDERS) notFound();
  return (
    <div className="mx-auto grid max-w-4xl gap-6" data-testid="placeholder-screen">
      <PageHeader title={title} description={description} />
      <EmptyState
        icon={<ConstructionIcon className="size-5" />}
        title="Coming in this redesign"
        description="This screen is being built and is not available yet. Nothing here is saved or changed."
      />
    </div>
  );
}
