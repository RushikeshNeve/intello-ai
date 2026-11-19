import { describe, expect, it } from "vitest";
import { citationByMarker, citationHref, citationLabel } from "../citations";
import { parseBlocks, parseInline } from "../format";
import { verificationNote } from "../messages";
import { cit } from "./fixtures";

describe("parseInline", () => {
  it("splits text, code spans and citation markers", () => {
    expect(parseInline("Use `npm test` now [1][2].")).toEqual([
      { t: "text", text: "Use " },
      { t: "code", text: "npm test" },
      { t: "text", text: " now " },
      { t: "cite", marker: "[1]", raw: "[1]" },
      { t: "cite", marker: "[2]", raw: "[2]" },
      { t: "text", text: "." },
    ]);
  });

  it("treats an unterminated backtick and markup as literal text", () => {
    expect(parseInline("a `b")).toEqual([{ t: "text", text: "a `b" }]);
    expect(parseInline("<script>alert(1)</script> <b>x</b>")).toEqual([{ t: "text", text: "<script>alert(1)</script> <b>x</b>" }]);
  });

  it("does not treat markers inside code spans as citations, nor non-numeric brackets", () => {
    expect(parseInline("`[1]` and [a] and [1234]")).toEqual([
      { t: "code", text: "[1]" },
      { t: "text", text: " and [a] and [1234]" },
    ]);
  });
});

describe("parseBlocks", () => {
  it("builds paragraphs and lists", () => {
    const blocks = parseBlocks("Intro line\nsecond line\n\n- one [1]\n- two\n\n1. first\n2) second\n\nDone");
    expect(blocks.map((b) => b.t)).toEqual(["p", "ul", "ol", "p"]);
    expect(blocks[1]).toMatchObject({ t: "ul", items: [[{ t: "text", text: "one " }, { t: "cite", marker: "[1]" }], [{ t: "text", text: "two" }]] });
  });

  it("emits no HTML: markup stays text inside paragraphs", () => {
    const [b] = parseBlocks('<img src=x onerror="alert(1)">');
    expect(b).toEqual({ t: "p", inline: [{ t: "text", text: '<img src=x onerror="alert(1)">' }] });
  });

  it("handles empty input and CRLF", () => {
    expect(parseBlocks("")).toEqual([]);
    expect(parseBlocks("a\r\n\r\nb")).toHaveLength(2);
  });

  it("caps very long input", () => {
    const blocks = parseBlocks("x".repeat(500_000));
    expect(JSON.stringify(blocks).length).toBeLessThan(110_000);
  });
});

describe("citation links", () => {
  const workspaces = [{ id: "w1", slug: "notes" }];
  it("opens exactly the cited document, version and chunk (never 'latest')", () => {
    const href = citationHref(cit(1, { version_id: "v-old", chunk_id: "c-77", page_start: 4 }), "acme", workspaces);
    expect(href).toBe("/o/acme/w/notes/documents/d-1/evidence?v=v-old&chunk=c-77&page=4");
  });

  it("falls back to the workspace id when the slug is unknown", () => {
    expect(citationHref(cit(1, { workspace_id: "w-unknown" }), "acme", workspaces)).toContain("/w/w-unknown/documents/");
  });

  it("omits the page when the citation has none", () => {
    expect(citationHref(cit(1, { page_start: null, page_end: null }), "acme", workspaces)).not.toContain("page=");
  });

  it("labels and indexes by marker (first wins)", () => {
    expect(citationLabel(cit(2, { page_start: 3, page_end: 5 }))).toBe("Source 2: Doc 2, version 2, pages 3–5");
    const map = citationByMarker([cit(1), cit(1, { title: "dup" })]);
    expect(map.get("[1]")?.title).toBe("Doc 1");
  });
});

describe("verificationNote", () => {
  it("words abstained, partial and flagged plainly", () => {
    expect(verificationNote({ status: "abstained", withheld_count: 0, flagged_count: 0 })?.text).toBe("I couldn't find this in your documents.");
    expect(verificationNote({ status: "partial", withheld_count: 2, flagged_count: 0 })?.text).toContain("Some statements were removed because they weren't supported");
    expect(verificationNote({ status: "grounded", withheld_count: 0, flagged_count: 0 })).toBeNull();
    expect(verificationNote({ status: "grounded", withheld_count: 0, flagged_count: 1 })?.tone).toBe("flagged");
    expect(verificationNote(null)).toBeNull();
  });
});
