"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApiFetcher } from "@/lib/query/provider";
import { deleteQuarantined, listQuarantine, releaseQuarantined } from "./api";

/** Admin "Quarantine" section: documents held back by the ingestion scan (never searchable). */
export function QuarantinePanel({ orgId, workspaceId }: { orgId: string; workspaceId: string }) {
  const fetcher = useApiFetcher();
  const client = useQueryClient();
  const key = ["quarantine", orgId, workspaceId];
  const query = useQuery({ queryKey: key, queryFn: () => listQuarantine(fetcher, orgId, workspaceId) });
  const done = { onSuccess: () => client.invalidateQueries({ queryKey: key }) };
  const release = useMutation({ mutationFn: (id: string) => releaseQuarantined(fetcher, orgId, workspaceId, id), ...done });
  const remove = useMutation({ mutationFn: (id: string) => deleteQuarantined(fetcher, orgId, workspaceId, id), ...done });

  if (query.isError) return <p role="alert">Quarantine is not available to you.</p>;
  const items = query.data ?? [];
  return (
    <section aria-label="Quarantine">
      <h2>Quarantine</h2>
      {query.isSuccess && items.length === 0 ? <p>No documents are in quarantine.</p> : null}
      <ul>
        {items.map((item) => (
          <li key={item.document_id}>
            <span>{item.title}</span>{" "}
            <span>
              {item.findings} findings, highest severity {item.max_severity}
            </span>{" "}
            <button type="button" onClick={() => release.mutate(item.document_id)}>
              Release {item.title}
            </button>{" "}
            <button type="button" onClick={() => remove.mutate(item.document_id)}>
              Delete {item.title}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
