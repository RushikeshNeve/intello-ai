import type { Version } from "../types";

/** Contract §6 inline allow-list. Everything else is a download, whatever the extension says. */
export type PreviewKind = "pdf" | "image" | "text" | "download";

const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const TEXT_TYPES = new Set(["text/plain", "text/markdown"]);

export function previewKind(contentType: string): PreviewKind {
  const base = contentType.split(";")[0].trim().toLowerCase();
  if (base === "application/pdf") return "pdf";
  if (IMAGE_TYPES.has(base)) return "image";
  if (TEXT_TYPES.has(base)) return "text";
  return "download";
}

/** At most this many bytes of a text file are read and shown. */
export const TEXT_PREVIEW_CAP_BYTES = 1024 * 1024;

export type SelectedVersion = Pick<Version, "id" | "document_id" | "original_filename" | "content_type" | "size_bytes">;
