import type { UploadErrorCode } from "./api";

/** Human size with binary units labelled the way the product copy does ("100 MB"). */
export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 10 || Number.isInteger(v) ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** Calm, actionable copy for each error code (contract §5). */
export function errorMessage(code: UploadErrorCode, maxBytes: number): string {
  switch (code) {
    case "file_too_large":
      return `This file is larger than ${formatBytes(maxBytes)}.`;
    case "quota_exceeded":
      return "Your organization's storage is full.";
    case "too_many_uploads":
      return "Too many uploads are in progress. Wait for some to finish, then try again.";
    case "upload_expired":
      return "Upload timed out, try again.";
    case "object_missing":
    case "size_mismatch":
    case "checksum_mismatch":
      return "The file didn't arrive intact. Try again.";
    case "storage_unavailable":
      return "File storage is temporarily unavailable. Try again in a moment.";
    case "forbidden":
      return "You don't have permission to upload here.";
    case "not_found":
      return "This workspace or document is no longer available.";
    case "invalid_request":
      return "This file can't be uploaded (check its name and type).";
    case "network_error":
    case "upload_rejected":
      return "The upload was interrupted. Check your connection and try again.";
    case "unauthenticated":
    case "invalid_token":
      return "Your session ended. Sign in again to continue.";
    default:
      return "Something went wrong. Try again.";
  }
}

/** Retrying cannot help these without the user changing something else. */
export function isRetryable(code: UploadErrorCode): boolean {
  return code !== "file_too_large" && code !== "forbidden" && code !== "not_found";
}
