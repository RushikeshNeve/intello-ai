import { describe, expect, it } from "vitest";
import { ApiError } from "@/features/organizations/api/http";
import { degradedMessage, parseHighlights, parseSearchResponse, parseSnippet, toSearchErrorCode } from "../api";
import { normalizeRanges, segmentText } from "../highlight";
import { DEFAULT_SEARCH_STATE, loadScope, parseSearchState, requestKey, saveScope, toSearchParams, toSearchRequest } from "../url-state";

const result = (over: Record<string, unknown> = {}) => ({
  document_id: "d1",
  title: "Report",
  workspace_id: "w1",
  workspace_slug: "notes",
  content_type: "application/pdf",
  version_id: "v1",
  version_number: 2,
  score: 0.5,
  snippets: [{ evidence_id: "e1", text: "alpha beta", page_start: 3, page_end: 4, highlights: [[0, 5]] }],
  ...over,
});

describe("search parsers", () => {
  it("parses a full response", () => {
    const r = parseSearchResponse({ query: "alpha", results: [result()], degraded: ["embedding_unavailable"], took_ms: 12, scope: { workspace_count: 2 } });
    expect(r.results[0]).toMatchObject({ document_id: "d1", workspace_slug: "notes", version_number: 2 });
    expect(r.results[0].snippets[0]).toMatchObject({ page_start: 3, page_end: 4, highlights: [[0, 5]], chunk_id: null });
    expect(r).toMatchObject({ degraded: ["embedding_unavailable"], took_ms: 12, workspace_count: 2 });
  });

  it("an empty result list is valid; a missing list is not", () => {
    expect(parseSearchResponse({ results: [] }).results).toEqual([]);
    expect(() => parseSearchResponse({})).toThrow(ApiError);
    expect(() => parseSearchResponse(null)).toThrow(ApiError);
  });

  it("drops malformed results and snippets instead of failing everything", () => {
    const r = parseSearchResponse({ results: [result(), { title: "no ids" }, result({ document_id: "d2", snippets: [{ text: 3 }, { text: "ok" }] })] });
    expect(r.results.map((x) => x.document_id)).toEqual(["d1", "d2"]);
    expect(r.results[1].snippets).toHaveLength(1);
  });

  it("uses chunk_id when sent, or an evidence_id that is itself a uuid", () => {
    expect(parseSnippet({ text: "t", chunk_id: "c-1", evidence_id: "e1" }).chunk_id).toBe("c-1");
    expect(parseSnippet({ text: "t", evidence_id: "0b3b2f55-2d0a-4f9a-9d6a-1d1b3b9f5e11" }).chunk_id).toBe("0b3b2f55-2d0a-4f9a-9d6a-1d1b3b9f5e11");
    expect(parseSnippet({ text: "t", evidence_id: "e3" }).chunk_id).toBeNull();
  });

  it("sanitises highlights", () => {
    expect(parseHighlights([[1, 2], [3], "x", [NaN, 4], [5.9, 7.2], null])).toEqual([[1, 2], [5, 7]]);
    expect(parseHighlights("no")).toEqual([]);
  });

  it("maps errors", () => {
    expect(toSearchErrorCode(new ApiError(429, "rate_limited"))).toBe("rate_limited");
    expect(toSearchErrorCode(new ApiError(503, "search_unavailable"))).toBe("unavailable");
    expect(toSearchErrorCode(new ApiError(422, "validation_error"))).toBe("invalid");
    expect(toSearchErrorCode(new ApiError(404, "not_found"))).toBe("not_found");
    expect(toSearchErrorCode(new ApiError(0, "network_error"))).toBe("network");
    expect(toSearchErrorCode(new ApiError(401, "x"))).toBe("unauthenticated");
  });

  it("words degraded modes", () => {
    expect(degradedMessage([])).toBeNull();
    expect(degradedMessage(["embedding_unavailable"])).toContain("keyword");
    expect(degradedMessage(["other"])).toContain("less complete");
  });
});

describe("highlight rendering", () => {
  const joined = (text: string, ranges: number[][]) => segmentText(text, ranges).map((s) => (s.mark ? `[${s.text}]` : s.text)).join("");

  it("marks ranges", () => {
    expect(joined("the quick brown fox", [[4, 9], [16, 19]])).toBe("the [quick] brown [fox]");
  });
  it("merges overlapping and touching ranges", () => {
    expect(joined("abcdefgh", [[1, 4], [3, 6], [6, 7]])).toBe("a[bcdefg]h");
    expect(normalizeRanges([[5, 8], [1, 3], [2, 4]], 10)).toEqual([[1, 4], [5, 8]]);
  });
  it("clamps out-of-range and drops invalid ranges", () => {
    expect(joined("abc", [[-5, 2], [2, 99]])).toBe("[abc]");
    expect(joined("abc", [[5, 9], [2, 2], [3, 1], [NaN, 1]])).toBe("abc");
    expect(joined("abc", [[1.7, 2.9]])).toBe("a[b]c");
  });
  it("uses code-point offsets, so astral characters line up", () => {
    // "😀" is one code point (two UTF-16 units): the server counts it as 1
    expect(joined("😀 smile 😀", [[2, 7]])).toBe("😀 [smile] 😀");
  });
  it("leaves markup as text (segments are plain strings)", () => {
    const segs = segmentText("<script>alert(1)</script> hit", [[26, 29]]);
    expect(segs.map((s) => s.text).join("")).toBe("<script>alert(1)</script> hit");
    expect(segs.every((s) => typeof s.text === "string")).toBe(true);
  });
  it("handles empty text and no ranges", () => {
    expect(segmentText("", [[0, 1]])).toEqual([]);
    expect(segmentText("abc", [])).toEqual([{ text: "abc", mark: false }]);
  });
});

describe("search URL state", () => {
  it("round-trips and omits defaults", () => {
    const s = parseSearchState(new URLSearchParams("q=  hello world &ws=notes&type=pdf&mode=keyword"));
    expect(s).toEqual({ q: "hello world", ws: "notes", type: "pdf", mode: "keyword" });
    expect(toSearchParams(s).toString()).toBe("q=hello+world&ws=notes&type=pdf&mode=keyword");
    expect(toSearchParams(DEFAULT_SEARCH_STATE).toString()).toBe("");
    expect(toSearchParams({ ...DEFAULT_SEARCH_STATE, q: "x" }).toString()).toBe("q=x");
  });

  it("falls back to defaults for junk values", () => {
    expect(parseSearchState(new URLSearchParams("type=exe&mode=magic&ws=../etc&q="))).toEqual(DEFAULT_SEARCH_STATE);
  });

  it("caps the query at 500 characters", () => {
    expect(parseSearchState(new URLSearchParams(`q=${"a".repeat(900)}`)).q).toHaveLength(500);
  });

  it("builds the request: scope, type and mode map to the API body", () => {
    const state = { q: "alpha", ws: "notes", type: "pdf", mode: "vector" } as const;
    expect(toSearchRequest(state, "w1")).toEqual({ query: "alpha", limit: 20, workspace_ids: ["w1"], content_types: ["application/pdf"], mode: "vector" });
    expect(toSearchRequest({ ...DEFAULT_SEARCH_STATE, q: "alpha" }, undefined)).toEqual({ query: "alpha", limit: 20 });
    expect(toSearchRequest(DEFAULT_SEARCH_STATE, undefined)).toBeNull();
  });

  it("different scopes/filters have different request keys", () => {
    const base = { ...DEFAULT_SEARCH_STATE, q: "a" };
    expect(requestKey(base, undefined)).not.toBe(requestKey(base, "w1"));
    expect(requestKey(base, undefined)).not.toBe(requestKey({ ...base, mode: "keyword" }, undefined));
  });
});

describe("remembered scope", () => {
  const store = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
  };
  it("is saved per org and read back", () => {
    const s = store();
    saveScope("o1", "notes", s);
    saveScope("o2", null, s);
    expect(loadScope("o1", s)).toBe("notes");
    expect(loadScope("o2", s)).toBe("all");
    expect(loadScope("o3", s)).toBeNull();
  });
  it("ignores junk and survives a throwing storage", () => {
    const s = store();
    s.setItem("ukos.search.scope.o1", "../../x");
    expect(loadScope("o1", s)).toBeNull();
    const boom = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(loadScope("o1", boom)).toBeNull();
    expect(() => saveScope("o1", "x", boom)).not.toThrow();
  });
});
