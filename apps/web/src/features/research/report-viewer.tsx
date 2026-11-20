"use client";

import Link from "next/link";
import { InfoIcon } from "lucide-react";
import { SurfaceCard } from "@/components/ui/surface";
import { citationHref, citationLabel } from "@/features/chat/citations";
import type { Workspace } from "@/features/workspaces/api";
import type { Report } from "./api";

/** Sectioned report. Every statement carries citation chips that open the evidence viewer; withheld statements are disclosed. */
export function ReportViewer({ report, orgSlug, workspaces }: { report: Report; orgSlug: string; workspaces: readonly Workspace[] }) {
  return (
    <SurfaceCard as="section" aria-label="Report" className="grid gap-5" data-testid="research-report">
      <h2 className="text-section font-semibold text-brand-indigo dark:text-foreground">Report</h2>
      {report.withheld > 0 && (
        <p role="note" className="flex items-start gap-2 rounded-control bg-info-soft px-3 py-2 text-sm text-info" data-testid="report-withheld">
          <InfoIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          <span>
            {report.withheld} statement{report.withheld === 1 ? " was" : "s were"} withheld because the sources didn&apos;t support {report.withheld === 1 ? "it" : "them"}.
          </span>
        </p>
      )}
      {report.sections.map((sec, i) => (
        <div key={i} className="grid gap-2" data-testid="report-section">
          <h3 className="text-card-title font-semibold break-words text-brand-indigo dark:text-foreground">{sec.heading}</h3>
          {sec.statements.map((st, j) => (
            <p key={j} className="text-sm leading-relaxed break-words" data-testid="report-statement">
              {st.text}{" "}
              {st.citations.map((c) => (
                <Link
                  key={c.marker + c.chunk_id}
                  href={citationHref(c, orgSlug, workspaces)}
                  aria-label={citationLabel(c)}
                  title={citationLabel(c)}
                  className="focus-ring mx-0.5 inline-flex min-w-5 items-center justify-center rounded-md bg-info-soft px-1 text-xs font-semibold text-brand-blue no-underline hover:bg-accent"
                  data-testid="citation-chip"
                  data-chunk-id={c.chunk_id}
                >
                  {c.marker}
                </Link>
              ))}
            </p>
          ))}
        </div>
      ))}
    </SurfaceCard>
  );
}
