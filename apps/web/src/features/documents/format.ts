/** Deterministic display helpers (no locale/timezone dependence, so server and browser render identically). */

export function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 10);
}

export function shortSha(sha: string): string {
  return sha.slice(0, 8);
}

const LABELS: Record<string, string> = {
  "application/pdf": "PDF",
  "text/plain": "Text",
  "text/markdown": "Markdown",
  "text/csv": "CSV",
  "text/html": "HTML",
  "application/json": "JSON",
  "application/msword": "Word",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
  "application/vnd.ms-excel": "Spreadsheet",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Spreadsheet",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": "Slides",
};

export function typeLabel(contentType: string | undefined): string {
  if (!contentType) return "-";
  const base = contentType.split(";")[0].trim().toLowerCase();
  if (LABELS[base]) return LABELS[base];
  if (base.startsWith("image/")) return "Image";
  if (base.startsWith("text/")) return "Text";
  return "File";
}
