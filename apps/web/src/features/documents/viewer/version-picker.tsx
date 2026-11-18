import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { formatBytes } from "@/features/documents/upload/messages";
import { formatDate, shortSha } from "../format";
import type { Version } from "../types";

/** Version timeline, newest first. The selected version carries `aria-current="true"`. */
export function VersionPicker({
  versions,
  selectedId,
  documentPath,
}: {
  versions: Version[];
  selectedId: string | null;
  /** `/o/{org}/w/{ws}/documents/{id}` */
  documentPath: string;
}) {
  return (
    <nav aria-labelledby="versions-heading" data-testid="version-picker">
      <h2 id="versions-heading" className="mb-2 text-sm font-medium">
        Versions
      </h2>
      <ol className="grid gap-2">
        {versions.map((v) => {
          const selected = v.id === selectedId;
          return (
            <li key={v.id}>
              <Link
                href={`${documentPath}?v=${encodeURIComponent(v.id)}`}
                aria-current={selected ? "true" : undefined}
                className="grid gap-0.5 rounded-lg border border-border px-3 py-2 text-sm outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 aria-[current=true]:border-ring aria-[current=true]:bg-muted motion-safe:transition-colors"
                data-testid="version-item"
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="font-medium">Version {v.version_number}</span>
                  <Badge variant={v.retention.status === "current" ? "default" : "secondary"}>
                    {v.retention.status === "current" ? "Current" : "Superseded"}
                  </Badge>
                </span>
                <span className="truncate text-muted-foreground">{v.original_filename}</span>
                <span className="text-xs text-muted-foreground">
                  {formatBytes(v.size_bytes)} · <time dateTime={v.created_at}>{formatDate(v.created_at)}</time> · sha256{" "}
                  <code>{shortSha(v.sha256)}</code>
                </span>
              </Link>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
