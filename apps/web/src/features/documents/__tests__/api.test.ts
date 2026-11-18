import { describe, expect, it } from "vitest";
import { ApiError, type Fetcher } from "@/features/organizations/api/http";
import {
  deleteDocument,
  getContentUrl,
  getDocument,
  getVersion,
  isNotAvailable,
  listDocuments,
  listVersions,
  renameDocument,
  toDocumentErrorCode,
} from "../api";
import { makeDoc, makeVersion, WS } from "./helpers";

function respond(status: number, body: unknown) {
  const calls: { path: string; init?: RequestInit }[] = [];
  const fetcher: Fetcher = async (path, init) => {
    calls.push({ path, init });
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  };
  return { fetcher, calls };
}

describe("documents client", () => {
  it("lists documents with only the supplied query params and parses the page shape", async () => {
    const { fetcher, calls } = respond(200, { items: [makeDoc()], next_cursor: "abc", can_upload: true });
    const page = await listDocuments(fetcher, "o1", "w1", { q: "rep", content_type: "text/", sort: "title", order: "asc", limit: 25, cursor: "abc" });
    expect(calls[0].path).toBe(`${WS}/documents?q=rep&content_type=text%2F&sort=title&order=asc&limit=25&cursor=abc`);
    expect(page.next_cursor).toBe("abc");
    expect(page.can_upload).toBe(true);
    expect(page.items[0]).toMatchObject({ id: "d-1", version_count: 1, permissions: { write: true, manage: true } });
    expect(page.items[0].latest_version?.retention.status).toBe("current");
  });

  it("omits empty params, and a missing cursor/can_upload is null/false", async () => {
    const { fetcher, calls } = respond(200, { items: [] });
    const page = await listDocuments(fetcher, "o1", "w1", { q: "" });
    expect(calls[0].path).toBe(`${WS}/documents`);
    expect(page).toEqual({ items: [], next_cursor: null, can_upload: false });
  });

  it("accepts a document without a latest version", async () => {
    const { fetcher } = respond(200, makeDoc({ latest_version: null }));
    expect((await getDocument(fetcher, "o1", "w1", "d-1")).latest_version).toBeNull();
  });

  it("rejects responses that do not match the contract", async () => {
    const bad = respond(200, { ...makeDoc(), permissions: undefined });
    await expect(getDocument(bad.fetcher, "o1", "w1", "d-1")).rejects.toMatchObject({ code: "invalid_response" });
    const badVersion = respond(200, { ...makeVersion(), retention: { status: "purged", purge_after: null } });
    await expect(getVersion(badVersion.fetcher, "o1", "w1", "d-1", "v-1")).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("renames with PATCH {title}", async () => {
    const { fetcher, calls } = respond(200, makeDoc({ title: "New" }));
    const doc = await renameDocument(fetcher, "o1", "w1", "d-1", "New");
    expect(calls[0].path).toBe(`${WS}/documents/d-1`);
    expect(calls[0].init?.method).toBe("PATCH");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ title: "New" });
    expect(doc.title).toBe("New");
  });

  it("deletes with DELETE and reads the 202 body", async () => {
    const { fetcher, calls } = respond(202, { status: "deletion_requested", purge_after: "2026-12-20T00:00:00Z" });
    const res = await deleteDocument(fetcher, "o1", "w1", "d-1");
    expect(calls[0].init?.method).toBe("DELETE");
    expect(res.status).toBe("deletion_requested");
  });

  it("lists versions and gets one", async () => {
    const list = respond(200, { items: [makeVersion({ id: "v-2", version_number: 2 })], next_cursor: null });
    expect((await listVersions(list.fetcher, "o1", "w1", "d-1", { limit: 200 })).items[0].version_number).toBe(2);
    expect(list.calls[0].path).toBe(`${WS}/documents/d-1/versions?limit=200`);
    const one = respond(200, makeVersion());
    await getVersion(one.fetcher, "o1", "w1", "d-1", "v-1");
    expect(one.calls[0].path).toBe(`${WS}/documents/d-1/versions/v-1`);
  });

  it("requests a content URL with the disposition (default attachment)", async () => {
    const body = { url: "https://store/x", expires_at: "2026-11-19T10:05:00Z", filename: "a.pdf", content_type: "application/pdf", disposition: "inline" };
    const a = respond(200, body);
    expect((await getContentUrl(a.fetcher, "o1", "w1", "d-1", "v-1", "inline")).disposition).toBe("inline");
    expect(a.calls[0].path).toBe(`${WS}/documents/d-1/versions/v-1/content-url?disposition=inline`);
    const b = respond(200, { ...body, disposition: "attachment" });
    await getContentUrl(b.fetcher, "o1", "w1", "d-1", "v-1");
    expect(b.calls[0].path).toContain("disposition=attachment");
  });

  it("encodes ids into the path", async () => {
    const { fetcher, calls } = respond(200, makeDoc());
    await getDocument(fetcher, "o/1", "w 1", "d#1");
    expect(calls[0].path).toBe("/v1/orgs/o%2F1/workspaces/w%201/documents/d%231");
  });

  it("maps errors to stable codes", async () => {
    const cases: [number, string, ReturnType<typeof toDocumentErrorCode>][] = [
      [404, "not_found", "not_found"],
      [410, "gone", "not_found"],
      [403, "forbidden", "forbidden"],
      [422, "invalid_cursor", "invalid_cursor"],
      [503, "storage_unavailable", "storage_unavailable"],
      [401, "invalid_token", "unauthenticated"],
      [500, "boom", "unknown"],
    ];
    for (const [status, detail, code] of cases) {
      const { fetcher } = respond(status, { detail });
      const err = await getDocument(fetcher, "o1", "w1", "d").catch((e) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(toDocumentErrorCode(err), `${status} ${detail}`).toBe(code);
    }
  });

  it("maps a network failure and distinguishes 'not available' from the rest", async () => {
    const fetcher: Fetcher = async () => {
      throw new TypeError("fetch failed");
    };
    const err = await getDocument(fetcher, "o1", "w1", "d").catch((e) => e);
    expect(toDocumentErrorCode(err)).toBe("network_error");
    expect(isNotAvailable(err)).toBe(false);
    expect(isNotAvailable(new ApiError(404, "not_found"))).toBe(true);
    expect(isNotAvailable(new ApiError(410, "gone"))).toBe(true);
    expect(isNotAvailable(new ApiError(403, "forbidden"))).toBe(true);
    expect(isNotAvailable(new ApiError(503, "storage_unavailable"))).toBe(false);
  });

  it("a 401 surfaces as unauthenticated (the browser fetcher redirects to sign-in)", async () => {
    const { fetcher } = respond(401, { detail: "invalid_token" });
    const err = await listDocuments(fetcher, "o1", "w1").catch((e) => e);
    expect(err.isUnauthenticated).toBe(true);
  });
});
