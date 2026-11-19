/**
 * Finds a chunk's text inside the loaded document text so the excerpt can be highlighted. Pure.
 * Extraction normalises whitespace, so an exact match is tried first, then a whitespace-insensitive one, then the
 * start of the excerpt. Returns UTF-16 offsets into `text` (usable with `String.slice`), or null.
 */
export type Range = { start: number; end: number };

const MIN_PREFIX = 40;
const PREFIX = 120;

/** Collapses whitespace runs to one space; `map[i]` is the offset in `s` of normalised char `i`. */
function normalize(s: string): { text: string; map: number[] } {
  const out: string[] = [];
  const map: number[] = [];
  let inSpace = true; // leading whitespace is dropped
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (/\s/.test(ch)) {
      if (!inSpace) {
        out.push(" ");
        map.push(i);
        inSpace = true;
      }
    } else {
      out.push(ch);
      map.push(i);
      inSpace = false;
    }
  }
  if (out.at(-1) === " ") {
    out.pop();
    map.pop();
  }
  return { text: out.join(""), map };
}

export function locateExcerpt(text: string, excerpt: string): Range | null {
  const needle = excerpt.trim();
  if (!needle || !text) return null;
  const exact = text.indexOf(needle);
  if (exact >= 0) return { start: exact, end: exact + needle.length };

  const hay = normalize(text);
  const want = normalize(needle).text;
  if (!want) return null;
  let at = hay.text.indexOf(want);
  if (at >= 0) return { start: hay.map[at], end: hay.map[at + want.length - 1] + 1 };

  // The excerpt may run past the end of what was loaded (truncated preview) or differ in its tail: match its start.
  if (want.length > MIN_PREFIX) {
    const prefix = want.slice(0, Math.min(PREFIX, Math.max(MIN_PREFIX, Math.floor(want.length / 2))));
    at = hay.text.indexOf(prefix);
    if (at >= 0) return { start: hay.map[at], end: hay.map[at + prefix.length - 1] + 1 };
  }
  return null;
}
