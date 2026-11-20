import type { Locator } from "./api";

const MAX_ROWS = 200;
const MAX_COLS = 50;

/** 1 → A, 27 → AA. */
export function colLabel(n: number): string {
  let s = "";
  for (let x = Math.max(1, n); x > 0; x = Math.floor((x - 1) / 26)) s = String.fromCharCode(65 + ((x - 1) % 26)) + s;
  return s;
}

export function cellRangeLabel(l: Pick<Locator, "sheet" | "row_start" | "row_end" | "col_start" | "col_end">): string {
  const sheet = l.sheet ? `${l.sheet}!` : "";
  const rs = l.row_start ?? 1;
  const re = l.row_end ?? rs;
  if (l.col_start === null) return `${sheet}rows ${rs}–${re}`;
  const a = `${colLabel(l.col_start)}${rs}`;
  const b = `${colLabel(l.col_end ?? l.col_start)}${re}`;
  return a === b ? `${sheet}${a}` : `${sheet}${a}:${b}`;
}

export type GridCell = { col: number; text: string; inRange: boolean };
export type GridRow = { row: number; cells: GridCell[]; inRange: boolean };

/** Cells of a text row: tab-separated, else pipe-separated, else one cell. */
export function splitRow(line: string): string[] {
  if (line.includes("\t")) return line.split("\t");
  if (line.includes("|")) return line.split("|").map((c) => c.trim());
  return [line];
}

/**
 * The grid window to show: the chunk's text laid out from (`windowRow`, `windowCol`) (default: the cited range's start), with
 * the cells inside the cited range marked. A chunk carries exactly the cited cells, so by default every cell is in range;
 * a wider window (when the caller has more data) shows the surrounding cells unmarked.
 */
export function buildGrid(text: string, l: Pick<Locator, "row_start" | "row_end" | "col_start" | "col_end">, window?: { row: number; col: number }): GridRow[] {
  const rs = l.row_start ?? 1;
  const re = l.row_end ?? rs;
  const cs = l.col_start ?? 1;
  const ce = l.col_end ?? Number.MAX_SAFE_INTEGER;
  const r0 = window?.row ?? rs;
  const c0 = window?.col ?? cs;
  return text
    .split(/\r?\n/)
    .slice(0, MAX_ROWS)
    .map((line, i) => {
      const row = r0 + i;
      const rowIn = row >= rs && row <= re;
      const cells = splitRow(line)
        .slice(0, MAX_COLS)
        .map((t, j) => ({ col: c0 + j, text: t, inRange: rowIn && c0 + j >= cs && c0 + j <= ce }));
      return { row, cells, inRange: rowIn };
    });
}

/** Source code with line numbers; lines inside `highlight` (default: all of them, the cited range) are marked. */
export function LineView({ text, startLine, highlight }: { text: string; startLine: number; highlight?: { start: number; end: number } }) {
  const lines = text.replace(/\n$/, "").split(/\r?\n/);
  const hl = highlight ?? { start: startLine, end: startLine + lines.length - 1 };
  return (
    <div className="max-h-[65vh] overflow-auto rounded-lg border border-border bg-muted/30 font-mono text-xs" data-testid="line-view" role="region" aria-label="Source lines" tabIndex={0}>
      <ol className="min-w-max">
        {lines.map((t, i) => {
          const n = startLine + i;
          const on = n >= hl.start && n <= hl.end;
          return (
            <li key={n} value={n} className={`flex gap-3 px-2 ${on ? "bg-accent" : ""}`} data-line={n} data-highlight={on ? "true" : "false"}>
              <span className="w-10 shrink-0 text-right text-muted-foreground select-none">{n}</span>
              <span className="whitespace-pre">{t || " "}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** A sheet window: column letters, row numbers, the cited range highlighted. Falls back to the chunk text layout. */
export function CellView({ text, locator, window }: { text: string; locator: Locator; window?: { row: number; col: number } }) {
  const rows = buildGrid(text, locator, window);
  const firstCol = rows[0]?.cells[0]?.col ?? locator.col_start ?? 1;
  const width = Math.max(1, ...rows.map((r) => r.cells.length));
  return (
    <div className="grid gap-1" data-testid="cell-view">
      <p className="text-xs text-muted-foreground" data-testid="cell-range">
        {cellRangeLabel(locator)}
      </p>
      <div className="max-h-[65vh] overflow-auto rounded-lg border border-border" role="region" aria-label="Sheet cells" tabIndex={0}>
        <table className="min-w-max border-collapse text-xs">
          <thead>
            <tr>
              <th className="border border-border bg-muted px-2 py-0.5" />
              {Array.from({ length: width }, (_, j) => (
                <th key={j} className="border border-border bg-muted px-2 py-0.5 font-medium">
                  {colLabel(firstCol + j)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.row}>
                <th className="border border-border bg-muted px-2 py-0.5 font-medium">{r.row}</th>
                {r.cells.map((c) => (
                  <td key={c.col} className={`border border-border px-2 py-0.5 whitespace-pre-wrap ${c.inRange ? "bg-accent" : ""}`} data-col={c.col} data-highlight={c.inRange ? "true" : "false"}>
                    {c.text}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
