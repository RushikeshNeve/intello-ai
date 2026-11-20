// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { parseChunk, parseLocators, type Locator } from "../api";
import { buildGrid, CellView, cellRangeLabel, colLabel, LineView } from "../locator-views";

afterEach(cleanup);

const loc = (o: Partial<Locator>): Locator => ({ kind: "cell", line_start: null, line_end: null, sheet: null, row_start: null, row_end: null, col_start: null, col_end: null, ...o });

describe("locators", () => {
  it("parses line and cell locators from the chunk endpoint and ignores junk", () => {
    const c = parseChunk({
      chunk_id: "c1", text: "t", heading_path: [], neighbors: {},
      locators: [{ kind: "line", char_start: 0, char_end: 5, line_start: 10, line_end: 12 }, { kind: "cell", sheet: "S", row_start: 2, row_end: 3, col_start: 2, col_end: 4 }, 7],
    });
    expect(c.locators.map((l) => l.kind)).toEqual(["line", "cell"]);
    expect(c.locators[1]).toMatchObject({ sheet: "S", row_start: 2, col_end: 4 });
    expect(parseLocators(undefined)).toEqual([]);
  });
  it("labels columns and ranges", () => {
    expect([colLabel(1), colLabel(26), colLabel(27)]).toEqual(["A", "Z", "AA"]);
    expect(cellRangeLabel(loc({ sheet: "Q3", row_start: 2, row_end: 5, col_start: 2, col_end: 4 }))).toBe("Q3!B2:D5");
    expect(cellRangeLabel(loc({ row_start: 3, row_end: 3, col_start: 1, col_end: 1 }))).toBe("A3");
  });
});

describe("LineView", () => {
  it("numbers lines from the cited start and highlights exactly the cited range", () => {
    render(<LineView text={"a\nb\nc\nd"} startLine={10} highlight={{ start: 11, end: 12 }} />);
    const rows = screen.getByTestId("line-view").querySelectorAll("li");
    expect([...rows].map((r) => [r.getAttribute("data-line"), r.getAttribute("data-highlight")])).toEqual([
      ["10", "false"], ["11", "true"], ["12", "true"], ["13", "false"],
    ]);
  });
  it("highlights the whole chunk by default", () => {
    render(<LineView text={"x\ny"} startLine={5} />);
    expect([...screen.getByTestId("line-view").querySelectorAll("li")].every((r) => r.getAttribute("data-highlight") === "true")).toBe(true);
  });
});

describe("CellView", () => {
  it("lays chunk text out as a grid at the cited coordinates, with the range noted", () => {
    render(<CellView text={"1\t2\n3\t4"} locator={loc({ sheet: "S", row_start: 2, row_end: 3, col_start: 2, col_end: 3 })} />);
    expect(screen.getByTestId("cell-range").textContent).toBe("S!B2:C3");
    const heads = [...screen.getByTestId("cell-view").querySelectorAll("thead th")].map((h) => h.textContent);
    expect(heads).toEqual(["", "B", "C"]);
    expect(screen.getByTestId("cell-view").querySelectorAll("td[data-highlight=true]")).toHaveLength(4);
  });
  it("marks only cells inside the cited range when the window is wider", () => {
    const g = buildGrid("a\tb\tc\nd\te\tf", { row_start: 1, row_end: 1, col_start: 2, col_end: 2 }, { row: 1, col: 1 });
    expect(g.flatMap((r) => r.cells.filter((c) => c.inRange).map((c) => c.text))).toEqual(["b"]);
  });
});
