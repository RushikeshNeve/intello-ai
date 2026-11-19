/**
 * Documents API client (contract m02 §5). Transport-agnostic like the organizations client: Server
 * Components pass the server fetcher, the browser passes the same-origin BFF fetcher.
 */
import {
  ApiError,
  invalid,
  isRecord,
  oneOf,
  requestJson,
  str,
  type ApiPath,
  type Fetcher,
} from "@/features/organizations/api/http";
import { parseSummaryLoose } from "./ingestion/api";
import {
  DISPOSITIONS,
  RETENTION_STATUSES,
  type ContentUrl,
  type DeleteResult,
  type Disposition,
  type Document,
  type DocumentList,
  type ListDocumentsParams,
  type Version,
  type VersionList,
} from "./types";

export type DocumentErrorCode =
  | "not_found"
  | "forbidden"
  | "invalid_cursor"
  | "storage_unavailable"
  | "network_error"
  | "unauthenticated"
  | "unknown";

/** Maps any thrown value to the small set of codes the UI reasons about. */
export function toDocumentErrorCode(err: unknown): DocumentErrorCode {
  if (!(err instanceof ApiError)) return "unknown";
  if (err.status === 401) return "unauthenticated";
  if (err.status === 0 || err.code === "network_error") return "network_error";
  if (err.code === "invalid_cursor") return "invalid_cursor";
  if (err.code === "storage_unavailable" || err.status === 502 || err.status === 503 || err.status === 504) {
    return "storage_unavailable";
  }
  if (err.status === 404 || err.status === 410 || err.code === "not_found") return "not_found";
  if (err.status === 403 || err.code === "forbidden") return "forbidden";
  return "unknown";
}

/** 404/410 (missing, deleted) and 403 all mean the same to the user: "not available". */
export function isNotAvailable(err: unknown): boolean {
  const code = toDocumentErrorCode(err);
  return code === "not_found" || code === "forbidden";
}

function num(o: Record<string, unknown>, key: string): number {
  const v = o[key];
  if (typeof v !== "number" || !Number.isFinite(v)) invalid();
  return v;
}
function nullableStr(o: Record<string, unknown>, key: string): string | null {
  const v = o[key];
  if (v === null || v === undefined) return null;
  if (typeof v !== "string") invalid();
  return v;
}
function bool(o: Record<string, unknown>, key: string): boolean {
  const v = o[key];
  if (typeof v !== "boolean") invalid();
  return v;
}

export function parseVersion(v: unknown): Version {
  if (!isRecord(v)) invalid();
  const retention = v.retention;
  if (!isRecord(retention)) invalid();
  return {
    id: str(v, "id"),
    document_id: str(v, "document_id"),
    version_number: num(v, "version_number"),
    original_filename: str(v, "original_filename"),
    content_type: str(v, "content_type"),
    size_bytes: num(v, "size_bytes"),
    sha256: str(v, "sha256"),
    created_by: nullableStr(v, "created_by"),
    created_at: str(v, "created_at"),
    retention: { status: oneOf(retention, "status", RETENTION_STATUSES), purge_after: nullableStr(retention, "purge_after") },
  };
}

export function parseDocument(v: unknown): Document {
  if (!isRecord(v)) invalid();
  const perms = v.permissions;
  if (!isRecord(perms)) invalid();
  return {
    id: str(v, "id"),
    org_id: str(v, "org_id"),
    workspace_id: str(v, "workspace_id"),
    title: str(v, "title"),
    created_by: nullableStr(v, "created_by"),
    created_at: str(v, "created_at"),
    updated_at: str(v, "updated_at"),
    version_count: num(v, "version_count"),
    latest_version: v.latest_version === null || v.latest_version === undefined ? null : parseVersion(v.latest_version),
    permissions: { write: bool(perms, "write"), manage: bool(perms, "manage") },
    ingestion: parseSummaryLoose(v.ingestion),
  };
}

function cursorOf(body: Record<string, unknown>): string | null {
  const c = body.next_cursor;
  if (c === undefined || c === null) return null;
  if (typeof c !== "string") invalid();
  return c;
}

export function parseDocumentList(body: unknown): DocumentList {
  if (!isRecord(body) || !Array.isArray(body.items)) invalid();
  return { items: body.items.map(parseDocument), next_cursor: cursorOf(body), can_upload: body.can_upload === true };
}

export function parseVersionList(body: unknown): VersionList {
  if (!isRecord(body) || !Array.isArray(body.items)) invalid();
  return { items: body.items.map(parseVersion), next_cursor: cursorOf(body) };
}

export function parseContentUrl(body: unknown): ContentUrl {
  if (!isRecord(body)) invalid();
  return {
    url: str(body, "url"),
    expires_at: str(body, "expires_at"),
    filename: str(body, "filename"),
    content_type: str(body, "content_type"),
    disposition: oneOf(body, "disposition", DISPOSITIONS),
  };
}

function parseDeleteResult(body: unknown): DeleteResult {
  if (!isRecord(body)) invalid();
  return { status: str(body, "status"), purge_after: nullableStr(body, "purge_after") };
}

const ws = (orgId: string, workspaceId: string) =>
  `/v1/orgs/${encodeURIComponent(orgId)}/workspaces/${encodeURIComponent(workspaceId)}`;
const docPath = (orgId: string, workspaceId: string, documentId: string): string =>
  `${ws(orgId, workspaceId)}/documents/${encodeURIComponent(documentId)}`;

export function listDocuments(
  fetcher: Fetcher,
  orgId: string,
  workspaceId: string,
  params: ListDocumentsParams = {},
): Promise<DocumentList> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") qs.set(k, String(v));
  const suffix = qs.size ? `?${qs}` : "";
  return requestJson(fetcher, `${ws(orgId, workspaceId)}/documents${suffix}` as ApiPath, parseDocumentList);
}

export function getDocument(fetcher: Fetcher, orgId: string, workspaceId: string, documentId: string): Promise<Document> {
  return requestJson(fetcher, docPath(orgId, workspaceId, documentId) as ApiPath, parseDocument);
}

export function renameDocument(
  fetcher: Fetcher,
  orgId: string,
  workspaceId: string,
  documentId: string,
  title: string,
): Promise<Document> {
  return requestJson(fetcher, docPath(orgId, workspaceId, documentId) as ApiPath, parseDocument, {
    method: "PATCH",
    body: JSON.stringify({ title }),
    headers: { "content-type": "application/json" },
  });
}

/** 202 `{status: "deletion_requested", purge_after}`; from then on every read of it is 404. */
export function deleteDocument(fetcher: Fetcher, orgId: string, workspaceId: string, documentId: string): Promise<DeleteResult> {
  return requestJson(fetcher, docPath(orgId, workspaceId, documentId) as ApiPath, parseDeleteResult, { method: "DELETE" });
}

export function listVersions(
  fetcher: Fetcher,
  orgId: string,
  workspaceId: string,
  documentId: string,
  params: { limit?: number; cursor?: string } = {},
): Promise<VersionList> {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.cursor) qs.set("cursor", params.cursor);
  const suffix = qs.size ? `?${qs}` : "";
  return requestJson(fetcher, `${docPath(orgId, workspaceId, documentId)}/versions${suffix}` as ApiPath, parseVersionList);
}

export function getVersion(
  fetcher: Fetcher,
  orgId: string,
  workspaceId: string,
  documentId: string,
  versionId: string,
): Promise<Version> {
  return requestJson(
    fetcher,
    `${docPath(orgId, workspaceId, documentId)}/versions/${encodeURIComponent(versionId)}` as ApiPath,
    parseVersion,
  );
}

/** Presigned GET. The URL is short-lived: never keep it past `expires_at`. */
export function getContentUrl(
  fetcher: Fetcher,
  orgId: string,
  workspaceId: string,
  documentId: string,
  versionId: string,
  disposition: Disposition = "attachment",
): Promise<ContentUrl> {
  return requestJson(
    fetcher,
    `${docPath(orgId, workspaceId, documentId)}/versions/${encodeURIComponent(versionId)}/content-url?disposition=${disposition}` as ApiPath,
    parseContentUrl,
  );
}
