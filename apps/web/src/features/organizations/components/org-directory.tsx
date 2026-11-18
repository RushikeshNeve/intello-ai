"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRightIcon } from "lucide-react";
import { queryKeys } from "@/lib/query/keys";
import { useApiFetcher } from "@/lib/query/provider";
import { SlugNameForm } from "@/features/shell/slug-name-form";
import { createOrg, listOrgs, ROLE_LABEL, type OrgSummary } from "../api/orgs";

/** `/home`: the caller's organizations plus a create form. Seeded with the server-rendered list. */
export function OrgDirectory({ initialOrgs, navigate }: { initialOrgs: OrgSummary[]; navigate?: (href: string) => void }) {
  const fetcher = useApiFetcher();
  const queryClient = useQueryClient();
  const router = useRouter();
  const { data: orgs = initialOrgs } = useQuery({
    queryKey: queryKeys.orgs(),
    queryFn: () => listOrgs(fetcher),
    initialData: initialOrgs,
  });

  return (
    <div className="grid gap-10">
      <section aria-labelledby="orgs-heading" className="grid gap-3">
        <h2 id="orgs-heading" className="text-lg font-semibold">
          Your organizations
        </h2>
        {orgs.length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid="orgs-empty">
            You are not a member of any organization yet. Create one below to get started.
          </p>
        ) : (
          <ul className="grid gap-2" data-testid="org-list">
            {orgs.map((o) => (
              <li key={o.id}>
                <Link
                  href={`/o/${encodeURIComponent(o.slug)}`}
                  className="flex items-center justify-between gap-3 rounded-lg border border-border bg-card px-4 py-3 text-card-foreground outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 motion-safe:transition-colors"
                >
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{o.name}</span>
                    <span className="block text-xs text-muted-foreground">
                      {o.slug} · {ROLE_LABEL[o.role]}
                    </span>
                  </span>
                  <ChevronRightIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="create-org-heading" className="grid gap-3">
        <h2 id="create-org-heading" className="text-lg font-semibold">
          Create an organization
        </h2>
        <SlugNameForm
          noun="organization"
          submitLabel="Create organization"
          onCreate={async (input) => {
            const org = await createOrg(fetcher, input);
            queryClient.setQueryData<OrgSummary[]>(queryKeys.orgs(), (prev = []) => [
              ...prev,
              { id: org.id, slug: org.slug, name: org.name, role: org.role },
            ]);
            (navigate ?? router.push)(`/o/${encodeURIComponent(org.slug)}`);
          }}
        />
      </section>
    </div>
  );
}
