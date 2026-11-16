import { describe, expect, it, vi } from "vitest";

// `connection()` needs a live Next.js request scope; outside the server it is a no-op for this test.
vi.mock("next/server", () => ({ connection: vi.fn(async () => undefined) }));

import { GET } from "./route";

describe("GET /api/health", () => {
  it("returns 200 with status ok and no caching", async () => {
    const res = await GET();
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("application/json");
    expect(res.headers.get("cache-control")).toBe("no-store");
    await expect(res.json()).resolves.toEqual({ status: "ok" });
  });
});
