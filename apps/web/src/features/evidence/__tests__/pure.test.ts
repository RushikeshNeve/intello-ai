import { describe, expect, it } from "vitest";
import { ApiError } from "@/features/organizations/api/http";
import { chunkPath, parseChunk, toEvidenceErrorCode } from "../api";
import { documentHref, evidenceHref, parseEvidenceParams, workspaceRefFor } from "../links";
import { locateExcerpt } from "../locate";

describe("locateExcerpt", () => {
  const text = "Heading\n\nThe quarterly revenue grew by 10 percent.\nCosts fell.\n\nOutlook is stable.";
  const slice = (r: { start: number; end: number } | null) => (r ? text.slice(r.start, r.end) : null);

  it("finds an exact excerpt", () => {
    expect(slice(locateExcerpt(text, "Costs fell."))).toBe("Costs fell.");
  });

  it("finds an excerpt whose whitespace differs (extraction normalises it)", () => {
    expect(slice(locateExcerpt(text, "The quarterly   revenue grew by 10 percent. Costs fell."))).toBe("The quarterly revenue grew by 10 percent.\nCosts fell.");
  });

  it("falls back to the leading half of a long excerpt whose tail differs", () => {
    const long = "The quarterly revenue grew by 10 percent. Costs fell. Something that is not in the file at all";
    expect(slice(locateExcerpt(text, long))).toBe("The quarterly revenue grew by 10 percent.\nCosts");
  });

  it("returns null when absent, and for empty input", () => {
    expect(locateExcerpt(text, "nothing like this anywhere in the document")).toBeNull();
    expect(locateExcerpt(text, "   ")).toBeNull();
    expect(locateExcerpt("", "x")).toBeNull();
  });

  it("a short excerpt that differs is not guessed at", () => {
    expect(locateExcerpt(text, "Costs rose.")).toBeNull();
  });
});

describe("evidence links", () => {
  it("builds a stable deep link with version, chunk and page", () => {
    expect(evidenceHref({ orgSlug: "acme", workspaceRef: "notes", documentId: "d1", versionId: "v2", chunkId: "c9", page: 4 })).toBe(
      "/o/acme/w/notes/documents/d1/evidence?v=v2&chunk=c9&page=4",
    );
    expect(evidenceHref({ orgSlug: "a b", workspaceRef: "n", documentId: "d", versionId: "v" })).toBe("/o/a%20b/w/n/documents/d/evidence?v=v");
    expect(documentHref("acme", "notes", "d1", "v2")).toBe("/o/acme/w/notes/documents/d1?v=v2");
    expect(documentHref("acme", "notes", "d1")).toBe("/o/acme/w/notes/documents/d1");
  });

  it("parses params defensively", () => {
    const get = (q: string) => {
      const sp = new URLSearchParams(q);
      return (n: string) => sp.get(n);
    };
    expect(parseEvidenceParams(get("v=v1&chunk=c1&page=3"))).toEqual({ versionId: "v1", chunkId: "c1", page: 3 });
    expect(parseEvidenceParams(get("v=../x&chunk=a b&page=-2"))).toEqual({ versionId: null, chunkId: null, page: null });
    expect(parseEvidenceParams(get(""))).toEqual({ versionId: null, chunkId: null, page: null });
    expect(parseEvidenceParams(get("page=0")).page).toBeNull();
  });

  it("resolves the workspace segment from the list, else the id", () => {
    expect(workspaceRefFor([{ id: "w1", slug: "notes" }], "w1")).toBe("notes");
    expect(workspaceRefFor([{ id: "w1", slug: "notes" }], "w9")).toBe("w9");
    expect(workspaceRefFor([], "w9", "hint")).toBe("hint");
  });
});

describe("evidence API", () => {
  it("builds the chunk path", () => {
    expect(chunkPath("o1", "w1", "d1", "v1", "c1")).toBe("/v1/orgs/o1/workspaces/w1/documents/d1/versions/v1/chunks/c1");
  });

  it("parses a chunk with neighbours", () => {
    const c = parseChunk({
      chunk_id: "c1",
      text: "hello",
      page_start: 2,
      page_end: 3,
      heading_path: ["A", "B", 3],
      locators: [],
      version_number: 2,
      title: "Doc",
      neighbors: { prev_chunk_id: null, next_chunk_id: "c2" },
    });
    expect(c).toMatchObject({ chunk_id: "c1", page_start: 2, heading_path: ["A", "B"], prev_chunk_id: null, next_chunk_id: "c2" });
    expect(parseChunk({ chunk_id: "c", text: "t" })).toMatchObject({ page_start: null, prev_chunk_id: null, next_chunk_id: null, heading_path: [] });
    expect(() => parseChunk({ text: "t" })).toThrow(ApiError);
  });

  it("maps 404, 410, 503 and the rest", () => {
    expect(toEvidenceErrorCode(new ApiError(404, "not_found"))).toBe("not_found");
    expect(toEvidenceErrorCode(new ApiError(410, "evidence_unavailable"))).toBe("gone");
    expect(toEvidenceErrorCode(new ApiError(503, "storage_unavailable"))).toBe("unavailable");
    expect(toEvidenceErrorCode(new ApiError(0, "network_error"))).toBe("network");
    expect(toEvidenceErrorCode(new ApiError(401, "x"))).toBe("unauthenticated");
    expect(toEvidenceErrorCode(new ApiError(500, "x"))).toBe("unknown");
  });
});
