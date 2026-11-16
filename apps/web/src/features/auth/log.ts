/**
 * Structured auth logging. Callers pass event names and stable reason codes only; as a second line of
 * defence any field whose name looks sensitive is replaced, so tokens, codes, secrets and cookies
 * never reach the logs.
 */
const SENSITIVE_KEY = /(pass|secret|token|key|authorization|cookie|code|verifier|nonce|state|seal)/i;

export type LogLevel = "info" | "warn" | "error";

export function redactFields(fields: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(fields)) {
    if (SENSITIVE_KEY.test(key)) out[key] = "[REDACTED]";
    else if (typeof value === "string") out[key] = value.slice(0, 256);
    else out[key] = value;
  }
  return out;
}

/**
 * A loggable reason for an error: OAuth error code, library error code, or a short message with
 * anything JWT-shaped removed. Bundling minifies class names, so `name` alone is useless.
 */
export function errorReason(err: unknown): string {
  if (err && typeof err === "object") {
    const e = err as { error?: unknown; code?: unknown; message?: unknown; cause?: unknown };
    if (typeof e.error === "string") return `oauth:${e.error}`.slice(0, 96);
    const parts = [typeof e.code === "string" ? e.code : null, typeof e.message === "string" ? e.message : null];
    const text = parts.filter(Boolean).join(": ") || "unknown";
    return text.replace(/[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}/g, "[jwt]").slice(0, 160);
  }
  return "unknown";
}

export function authLog(level: LogLevel, event: string, fields: Record<string, unknown> = {}): void {
  const line = JSON.stringify({ level, event, ts: new Date().toISOString(), ...redactFields(fields) });
  if (level === "error") console.error(line);
  else if (level === "warn") console.warn(line);
  else console.info(line);
}
