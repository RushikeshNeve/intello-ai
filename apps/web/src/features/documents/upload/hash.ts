/** Client-side limit until the server tells us otherwise (`limits.max_bytes` from the create-session response). */
export const DEFAULT_MAX_BYTES = 104857600; // 100 MiB

export class FileTooLargeError extends Error {
  constructor(readonly size: number, readonly limit: number) {
    super(`file is ${size} bytes, limit is ${limit}`);
    this.name = "FileTooLargeError";
  }
}

export function toHex(buf: ArrayBuffer): string {
  let out = "";
  for (const b of new Uint8Array(buf)) out += b.toString(16).padStart(2, "0");
  return out;
}

/** Lowercase hex SHA-256 digest -> base64 (the `x-amz-checksum-sha256` form), for callers that need it. */
export function hexToBase64(hex: string): string {
  let bin = "";
  for (let i = 0; i < hex.length; i += 2) bin += String.fromCharCode(parseInt(hex.slice(i, i + 2), 16));
  return btoa(bin);
}

/**
 * SHA-256 of a file as lowercase hex (WebCrypto).
 *
 * Memory trade-off: `crypto.subtle.digest` has no streaming mode, so the whole file is held in one
 * ArrayBuffer (at most `limit`, default 100 MiB) while it is hashed. The digest itself runs off the main
 * thread in browsers, so the UI stays responsive. Files above `limit` are refused BEFORE they are read.
 */
export async function sha256Hex(file: Blob, limit: number = DEFAULT_MAX_BYTES): Promise<string> {
  if (file.size > limit) throw new FileTooLargeError(file.size, limit);
  const bytes = await file.arrayBuffer();
  return toHex(await crypto.subtle.digest("SHA-256", bytes));
}
