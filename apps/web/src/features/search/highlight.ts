/**
 * Turns `text` + highlight ranges into plain segments for rendering (`<mark>` for the marked ones). Ranges are
 * CODE-POINT offsets (as the API computes them), `[start, end)`. Pure; never produces HTML. Invalid input is
 * repaired rather than rejected: non-integers and empty/negative ranges are dropped, out-of-range ends are clamped,
 * and overlapping or touching ranges are merged.
 */
export type Segment = { text: string; mark: boolean };

export function normalizeRanges(ranges: readonly (readonly number[])[], length: number): [number, number][] {
  const clean: [number, number][] = [];
  for (const r of ranges) {
    if (!Array.isArray(r) || r.length < 2) continue;
    const [a, b] = r;
    if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
    const start = Math.max(0, Math.trunc(a));
    const end = Math.min(length, Math.trunc(b));
    if (start < end) clean.push([start, end]);
  }
  clean.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
  const merged: [number, number][] = [];
  for (const [s, e] of clean) {
    const last = merged.at(-1);
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  }
  return merged;
}

export function segmentText(text: string, ranges: readonly (readonly number[])[]): Segment[] {
  if (text === "") return [];
  const points = Array.from(text); // code points: matches the server's offsets even with astral characters
  const merged = normalizeRanges(ranges, points.length);
  if (merged.length === 0) return [{ text, mark: false }];
  const out: Segment[] = [];
  let at = 0;
  for (const [s, e] of merged) {
    if (s > at) out.push({ text: points.slice(at, s).join(""), mark: false });
    out.push({ text: points.slice(s, e).join(""), mark: true });
    at = e;
  }
  if (at < points.length) out.push({ text: points.slice(at).join(""), mark: false });
  return out;
}
