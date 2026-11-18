import { describe, expect, it } from "vitest";
import {
  DEFAULT_STATE,
  clearFilters,
  hasFilters,
  nextPage,
  paramsKey,
  parseLibraryState,
  previousPage,
  toApiParams,
  toSearchParams,
  toggleSort,
  withFilter,
  type LibraryState,
} from "../url-state";

const parse = (qs: string) => parseLibraryState(new URLSearchParams(qs));

describe("url-state", () => {
  it("an empty URL is the default view", () => {
    expect(parse("")).toEqual(DEFAULT_STATE);
    expect(toSearchParams(DEFAULT_STATE).toString()).toBe("");
  });

  it("round-trips every filter through the URL", () => {
    const state: LibraryState = { q: "annual report", type: "pdf", sort: "title", order: "desc", cursor: "c3", prev: ["c2"] };
    const qs = toSearchParams(state).toString();
    expect(parse(qs)).toEqual(state);
    // and a second trip is stable (canonical form)
    expect(toSearchParams(parse(qs)).toString()).toBe(qs);
  });

  it("omits defaults, including the per-sort default order", () => {
    expect(toSearchParams(parse("sort=title&order=asc")).toString()).toBe("sort=title");
    expect(toSearchParams(parse("sort=title&order=desc")).toString()).toBe("sort=title&order=desc");
    expect(parse("sort=title").order).toBe("asc");
    expect(parse("").order).toBe("desc");
  });

  it("ignores invalid values instead of failing", () => {
    expect(parse("type=nope&sort=colour&order=sideways")).toEqual(DEFAULT_STATE);
    expect(parse("cursor=%3Cscript%3E").cursor).toBeNull();
    expect(parse("cursor=" + "a".repeat(600)).cursor).toBeNull();
    expect(parse("q=%20%20").q).toBe("");
  });

  it("prev only counts when there is a current cursor, and bad entries are dropped", () => {
    expect(parse("prev=a,b").prev).toEqual([]);
    expect(parse("cursor=c&prev=a,%3Cx%3E,b").prev).toEqual(["a", "b"]);
  });

  it("maps the state to API params (type filters become exact types or prefixes)", () => {
    expect(toApiParams(parse("q=x&type=text&sort=title&cursor=c"))).toEqual({
      q: "x",
      content_type: "text/",
      sort: "title",
      order: "asc",
      limit: 25,
      cursor: "c",
    });
    expect(toApiParams(parse("type=pdf")).content_type).toBe("application/pdf");
    expect(toApiParams(parse("type=image")).content_type).toBe("image/");
    expect(toApiParams(parse("type=spreadsheet")).content_type).toContain("spreadsheetml");
    expect(toApiParams(parse("type=other")).content_type).toBe("application/octet-stream");
    expect(toApiParams(parse("")).content_type).toBeUndefined();
    expect(toApiParams(parse("")).q).toBeUndefined();
  });

  it("changing any filter resets the cursor and the stack", () => {
    const deep = parse("cursor=c3&prev=c2");
    for (const patch of [{ q: "a" }, { type: "pdf" as const }, { sort: "created_at" as const }, { order: "asc" as const }]) {
      const next = withFilter(deep, patch);
      expect(next.cursor).toBeNull();
      expect(next.prev).toEqual([]);
    }
    expect(clearFilters(parse("q=a&type=pdf&cursor=c")).cursor).toBeNull();
  });

  it("switching the sort column picks that column's default order; toggling flips it", () => {
    const s = withFilter(DEFAULT_STATE, { sort: "title" });
    expect(s.order).toBe("asc");
    expect(toggleSort(s, "title").order).toBe("desc");
    expect(toggleSort(s, "updated_at")).toMatchObject({ sort: "updated_at", order: "desc" });
  });

  it("walks forward and back with a cursor stack", () => {
    let s = DEFAULT_STATE;
    s = nextPage(s, "c2");
    expect(s).toMatchObject({ cursor: "c2", prev: [] });
    s = nextPage(s, "c3");
    expect(s).toMatchObject({ cursor: "c3", prev: ["c2"] });
    s = nextPage(s, "c4");
    expect(s).toMatchObject({ cursor: "c4", prev: ["c2", "c3"] });
    s = previousPage(s);
    expect(s).toMatchObject({ cursor: "c3", prev: ["c2"] });
    s = previousPage(s);
    expect(s).toMatchObject({ cursor: "c2", prev: [] });
    s = previousPage(s);
    expect(s).toMatchObject({ cursor: null, prev: [] });
    expect(previousPage(s)).toBe(s);
  });

  it("the cache key ignores the stack but not the cursor or filters", () => {
    expect(paramsKey(parse("cursor=c&prev=a"))).toBe(paramsKey(parse("cursor=c")));
    expect(paramsKey(parse("cursor=c"))).not.toBe(paramsKey(parse("cursor=d")));
    expect(paramsKey(parse("q=a"))).not.toBe(paramsKey(parse("q=b")));
    expect(paramsKey(DEFAULT_STATE)).toBe("default");
  });

  it("reports whether a search or type filter is active (sort and paging are not filters)", () => {
    expect(hasFilters(parse("sort=title&cursor=c"))).toBe(false);
    expect(hasFilters(parse("q=a"))).toBe(true);
    expect(hasFilters(parse("type=pdf"))).toBe(true);
  });
});
