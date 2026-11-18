import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { DEFAULT_MAX_BYTES, FileTooLargeError, hexToBase64, sha256Hex } from "../hash";

describe("sha256Hex", () => {
  it("matches the empty-string vector", async () => {
    expect(await sha256Hex(new File([""], "e"))).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  });

  it('matches the "abc" vector (lowercase hex)', async () => {
    const h = await sha256Hex(new File(["abc"], "a"));
    expect(h).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    expect(h).toMatch(/^[0-9a-f]{64}$/);
  });

  it("matches the one-million-'a' vector", async () => {
    expect(await sha256Hex(new File(["a".repeat(1_000_000)], "m"))).toBe(
      "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0",
    );
  });

  it("matches node:crypto for a multi-chunk (3 MiB, many Blob parts) buffer", async () => {
    const bytes = new Uint8Array(3 * 1024 * 1024).map((_, i) => (i * 31 + 7) & 0xff);
    const parts = [bytes.subarray(0, 1 << 20), bytes.subarray(1 << 20, 2 << 20), bytes.subarray(2 << 20)];
    expect(await sha256Hex(new File(parts, "big"))).toBe(createHash("sha256").update(bytes).digest("hex"));
  });

  it("refuses a file above the limit before reading it", async () => {
    let read = false;
    const f = new File(["abcd"], "x");
    f.arrayBuffer = async () => ((read = true), new ArrayBuffer(0));
    await expect(sha256Hex(f, 3)).rejects.toBeInstanceOf(FileTooLargeError);
    expect(read).toBe(false);
  });

  it("defaults the limit to 100 MiB", () => {
    expect(DEFAULT_MAX_BYTES).toBe(104857600);
  });
});

describe("hexToBase64", () => {
  it("converts a hex digest to the base64 form used by x-amz-checksum-sha256", async () => {
    const hex = await sha256Hex(new File(["abc"], "a"));
    expect(hexToBase64(hex)).toBe(createHash("sha256").update("abc").digest("base64"));
  });
});
