/**
 * Upload API calls (contract m02 §5). Both go through the same-origin BFF (`/api/v1/*`) as JSON; the file
 * bytes NEVER pass through here (they go straight to `upload.url`, see `put.ts`).
 */
import { ApiError, invalid, isRecord, jsonBody, requestJson, str, type Fetcher } from "@/features/organizations/api/http";

/** Stable codes the upload flow reasons about (contract §5 errors + synthetic client-side ones). */
export const UPLOAD_ERROR_CODES = [
  "file_too_large",
  "quota_exceeded",
  "too_many_uploads",
  "upload_expired",
  "object_missing",
  "size_mismatch",
  "checksum_mismatch",
  "storage_unavailable",
  "forbidden",
  "not_found",
  "invalid_request",
  "invalid_token",
  // synthetic
  "network_error", // the request (API call or PUT) never got an answer
  "upload_rejected", // the object store answered the PUT with a 4xx / unexpected status
  "unauthenticated", // 401: the BFF is already redirecting to sign-in
  "unknown",
] as const;
export type UploadErrorCode = (typeof UPLOAD_ERROR_CODES)[number];

export function toUploadErrorCode(err: unknown): UploadErrorCode {
  if (err instanceof ApiError) {
    if (err.status === 401) return "unauthenticated";
    if (err.status === 0) return "network_error";
    if ((UPLOAD_ERROR_CODES as readonly string[]).includes(err.code)) return err.code as UploadErrorCode;
    if (err.code === "validation_error") return "invalid_request";
    if (err.status === 502 || err.status === 503 || err.status === 504) return "storage_unavailable";
  }
  return "unknown";
}

export type CreateUploadBody = {
  filename: string;
  content_type: string;
  size_bytes: number;
  /** 64 lowercase hex. */
  sha256: string;
  /** Present = upload a new VERSION of this document. */
  document_id?: string;
  title?: string;
};

export type UploadTarget = { method: string; url: string; headers: Record<string, string> };
export type UploadSession = {
  upload_id: string;
  document_id: string;
  version_id: string;
  expires_at: string;
  upload: UploadTarget;
  limits: { max_bytes: number };
};

// Document/Version are typed loosely on purpose: the library feature owns their full shape (§5 Shapes).
export type UploadedVersion = {
  id: string;
  document_id: string;
  version_number: number;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  [k: string]: unknown;
};
export type UploadedDocument = { id: string; title: string; [k: string]: unknown };
export type FinalizeResult = { document: UploadedDocument; version: UploadedVersion };

function num(o: Record<string, unknown>, key: string): number {
  const v = o[key];
  if (typeof v !== "number" || !Number.isFinite(v)) invalid();
  return v;
}

export function parseUploadSession(body: unknown): UploadSession {
  if (!isRecord(body)) invalid();
  const upload = body.upload;
  const limits = body.limits;
  if (!isRecord(upload) || !isRecord(limits) || !isRecord(upload.headers)) invalid();
  const headers: Record<string, string> = {};
  for (const [k, v] of Object.entries(upload.headers)) {
    if (typeof v !== "string") invalid();
    headers[k] = v;
  }
  return {
    upload_id: str(body, "upload_id"),
    document_id: str(body, "document_id"),
    version_id: str(body, "version_id"),
    expires_at: str(body, "expires_at"),
    upload: { method: str(upload, "method"), url: str(upload, "url"), headers },
    limits: { max_bytes: num(limits, "max_bytes") },
  };
}

export function parseFinalizeResult(body: unknown): FinalizeResult {
  if (!isRecord(body) || !isRecord(body.document) || !isRecord(body.version)) invalid();
  str(body.document, "id");
  str(body.version, "id");
  return body as unknown as FinalizeResult;
}

const ws = (orgId: string, workspaceId: string) =>
  `/v1/orgs/${encodeURIComponent(orgId)}/workspaces/${encodeURIComponent(workspaceId)}` as const;

/** `POST {ws}/uploads` → 201 (413 `file_too_large`, 429 `too_many_uploads`, 409 `quota_exceeded`, 403/404, 422). */
export function createUpload(fetcher: Fetcher, orgId: string, workspaceId: string, body: CreateUploadBody): Promise<UploadSession> {
  return requestJson(fetcher, `${ws(orgId, workspaceId)}/uploads`, parseUploadSession, jsonBody(body));
}

/** `POST {ws}/uploads/{id}/finalize` → 201 first time, 200 on a repeat; both are success. */
export function finalizeUpload(fetcher: Fetcher, orgId: string, workspaceId: string, uploadId: string): Promise<FinalizeResult> {
  return requestJson(
    fetcher,
    `${ws(orgId, workspaceId)}/uploads/${encodeURIComponent(uploadId)}/finalize` as `/v1/${string}`,
    parseFinalizeResult,
    { method: "POST" },
  );
}

/** The two calls bound to a transport; this is what the engine depends on (and tests fake). */
export type UploadsApi = {
  createUpload(orgId: string, workspaceId: string, body: CreateUploadBody): Promise<UploadSession>;
  finalizeUpload(orgId: string, workspaceId: string, uploadId: string): Promise<FinalizeResult>;
};

export function createUploadsApi(fetcher: Fetcher): UploadsApi {
  return {
    createUpload: (o, w, b) => createUpload(fetcher, o, w, b),
    finalizeUpload: (o, w, u) => finalizeUpload(fetcher, o, w, u),
  };
}
