export const DEFAULT_RETURN_TO = "/home";

/**
 * Open-redirect guard: only same-origin absolute paths are accepted (`/x`, never `//host`, `/\host`,
 * schemes, or control characters). Anything else falls back to the default landing page.
 */
export function safeReturnTo(value: string | null | undefined): string {
  if (!value || value.length > 2048) return DEFAULT_RETURN_TO;
  if (!value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return DEFAULT_RETURN_TO;
  if (/[\u0000-\u001f\u007f\\]/.test(value)) return DEFAULT_RETURN_TO;
  try {
    const parsed = new URL(value, "http://ukos.invalid");
    if (parsed.origin !== "http://ukos.invalid") return DEFAULT_RETURN_TO;
    if (parsed.pathname.startsWith("/api/auth/")) return DEFAULT_RETURN_TO;
    return parsed.pathname + parsed.search + parsed.hash;
  } catch {
    return DEFAULT_RETURN_TO;
  }
}
