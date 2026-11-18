import { describe, expect, it } from "vitest";
import { ApiError } from "@/features/organizations/api/http";
import { createUpload, finalizeUpload, toUploadErrorCode } from "../api";
import { fakeFetcher, jsonResponse } from "@/features/organizations/__tests__/helpers";
import { FILE_RESULT, session } from "./fakes";

const BODY = { filename: "a.txt", content_type: "text/plain", size_bytes: 3, sha256: "a".repeat(64) };
const WS = "/v1/orgs/o1/workspaces/w1";

describe("createUpload", () => {
  it("POSTs JSON to {ws}/uploads and parses the 201 shape", async () => {
    const { fetcher, calls } = fakeFetcher({ [`POST ${WS}/uploads`]: () => jsonResponse(201, session(1)) });
    const s = await createUpload(fetcher, "o1", "w1", BODY);
    expect(s.upload.headers["x-amz-checksum-sha256"]).toBe("sum-1");
    expect(s.limits.max_bytes).toBe(104857600);
    expect(calls[0]).toMatchObject({ method: "POST", path: `${WS}/uploads` });
    expect(JSON.parse(calls[0].body!)).toEqual(BODY);
  });

  it("rejects a response that does not match the contract", async () => {
    const { fetcher } = fakeFetcher({ [`POST ${WS}/uploads`]: () => jsonResponse(201, { upload_id: "x" }) });
    await expect(createUpload(fetcher, "o1", "w1", BODY)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it.each([
    [413, "file_too_large"],
    [409, "quota_exceeded"],
    [429, "too_many_uploads"],
    [403, "forbidden"],
    [404, "not_found"],
  ])("maps %i %s", async (status, code) => {
    const { fetcher } = fakeFetcher({ [`POST ${WS}/uploads`]: () => jsonResponse(status, { detail: code }) });
    const err = await createUpload(fetcher, "o1", "w1", BODY).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(toUploadErrorCode(err)).toBe(code);
  });

  it("maps a 422 validation list to invalid_request and a thrown fetch to network_error", async () => {
    const { fetcher } = fakeFetcher({ [`POST ${WS}/uploads`]: () => jsonResponse(422, { detail: [{ msg: "x" }] }) });
    expect(toUploadErrorCode(await createUpload(fetcher, "o1", "w1", BODY).catch((e) => e))).toBe("invalid_request");
    const boom = async () => {
      throw new TypeError("fail");
    };
    expect(toUploadErrorCode(await createUpload(boom, "o1", "w1", BODY).catch((e) => e))).toBe("network_error");
  });

  it("maps 401 to unauthenticated", () => {
    expect(toUploadErrorCode(new ApiError(401, "invalid_token"))).toBe("unauthenticated");
  });
});

describe("finalizeUpload", () => {
  it.each([201, 200])("accepts %i as success", async (status) => {
    const { fetcher, calls } = fakeFetcher({ [`POST ${WS}/uploads/up-1/finalize`]: () => jsonResponse(status, FILE_RESULT()) });
    const r = await finalizeUpload(fetcher, "o1", "w1", "up-1");
    expect(r.version.id).toBe("ver-1");
    expect(calls[0].body).toBeUndefined();
  });

  it.each([
    [409, "object_missing"],
    [409, "size_mismatch"],
    [409, "checksum_mismatch"],
    [410, "upload_expired"],
    [503, "storage_unavailable"],
  ])("maps %i %s", async (status, code) => {
    const { fetcher } = fakeFetcher({ [`POST ${WS}/uploads/up-1/finalize`]: () => jsonResponse(status, { detail: code }) });
    expect(toUploadErrorCode(await finalizeUpload(fetcher, "o1", "w1", "up-1").catch((e) => e))).toBe(code);
  });
});
