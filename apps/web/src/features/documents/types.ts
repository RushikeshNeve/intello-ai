import type { IngestionSummary } from "./ingestion/api";

/** Shapes of the documents API (contract m02 §5; `ingestion` from m03 §11). */
export const RETENTION_STATUSES = ["current", "superseded"] as const;

export type Version = {
  id: string;
  document_id: string;
  version_number: number;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  created_by: string | null;
  created_at: string;
  retention: { status: "current" | "superseded"; purge_after: string | null };
};

export type DocumentPermissions = { write: boolean; manage: boolean };

export type Document = {
  id: string;
  org_id: string;
  workspace_id: string;
  title: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  version_count: number;
  latest_version: Version | null;
  permissions: DocumentPermissions;
  /** Latest version's processing summary; null when the API did not send one. */
  ingestion: IngestionSummary | null;
};

export type DocumentList = { items: Document[]; next_cursor: string | null; can_upload: boolean };
export type VersionList = { items: Version[]; next_cursor: string | null };

export const DISPOSITIONS = ["inline", "attachment"] as const;
export type Disposition = (typeof DISPOSITIONS)[number];

export type ContentUrl = {
  url: string;
  expires_at: string;
  filename: string;
  content_type: string;
  disposition: Disposition;
};

export const SORTS = ["updated_at", "created_at", "title"] as const;
export type Sort = (typeof SORTS)[number];
export const ORDERS = ["asc", "desc"] as const;
export type Order = (typeof ORDERS)[number];

/** Query parameters of `GET {ws}/documents`. */
export type ListDocumentsParams = {
  q?: string;
  content_type?: string;
  sort?: Sort;
  order?: Order;
  limit?: number;
  cursor?: string;
};

export type DeleteResult = { status: string; purge_after: string | null };
