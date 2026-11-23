/** Quarantine admin API (M14.02): workspace admins list, release or delete scanned-out documents. */
import {
  invalid,
  isRecord,
  parsePage,
  requestJson,
  str,
  type ApiPath,
  type Fetcher,
} from "@/features/organizations/api/http";

export type QuarantineItem = {
  document_id: string;
  version_id: string;
  title: string;
  findings: number;
  max_severity: string;
  quarantined_at: string;
};

const base = (orgId: string, workspaceId: string) => `/v1/orgs/${orgId}/workspaces/${workspaceId}/quarantine`;

function parseItem(v: unknown): QuarantineItem {
  if (!isRecord(v) || typeof v.findings !== "number") invalid();
  return {
    document_id: str(v, "document_id"),
    version_id: str(v, "version_id"),
    title: str(v, "title"),
    findings: v.findings,
    max_severity: str(v, "max_severity"),
    quarantined_at: str(v, "quarantined_at"),
  };
}

export function listQuarantine(fetcher: Fetcher, orgId: string, workspaceId: string): Promise<QuarantineItem[]> {
  return requestJson(fetcher, base(orgId, workspaceId) as ApiPath, (b) => {
    if (!Array.isArray(b)) invalid();
    return parsePage({ items: b }, parseItem).items;
  });
}

export function releaseQuarantined(fetcher: Fetcher, orgId: string, workspaceId: string, documentId: string) {
  return requestJson(fetcher, `${base(orgId, workspaceId)}/${documentId}/release` as ApiPath, () => true, {
    method: "POST",
  });
}

export function deleteQuarantined(fetcher: Fetcher, orgId: string, workspaceId: string, documentId: string) {
  return requestJson(fetcher, `${base(orgId, workspaceId)}/${documentId}` as ApiPath, () => true, {
    method: "DELETE",
  });
}
