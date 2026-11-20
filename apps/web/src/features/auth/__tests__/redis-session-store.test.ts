import { describe, expect, it } from "vitest";
import { encodeCommand, parseReply, type RedisCommandClient } from "../redis-client";
import { RedisSessionStore } from "../redis-session-store";
import { createSessionStore, MemorySessionStore, type SessionRecord } from "../session-store";

const SECRET = "s".repeat(40);

/** In-memory fake implementing the subset of commands the store uses (GET, SET [NX] [PX], DEL). */
class FakeRedis implements RedisCommandClient {
  readonly data = new Map<string, { v: string; until: number }>();
  constructor(private now: () => number) {}
  async command(args: readonly string[]): Promise<unknown> {
    const [cmd, key, val, ...opts] = args as string[];
    const live = this.data.get(key!);
    const alive = live && live.until > this.now() ? live : undefined;
    if (cmd === "GET") return alive?.v ?? null;
    if (cmd === "DEL") return this.data.delete(key!) ? 1 : 0;
    if (cmd === "SET") {
      const nx = opts.includes("NX");
      const px = Number(opts[opts.indexOf("PX") + 1]);
      if (nx && alive) return null;
      this.data.set(key!, { v: val!, until: this.now() + px });
      return "OK";
    }
    throw new Error(`unsupported ${cmd}`);
  }
}

const record = (expiresAt: number): SessionRecord => ({
  id: "sid-1",
  createdAt: 0,
  expiresAt,
  user: { issuer: "https://x/dex", subject: "u1", email: "a@b.c", name: "A" },
  apiToken: "api-token-secret",
  apiTokenExpiresAt: 500,
  refreshToken: "refresh-secret",
});

describe("RedisSessionStore", () => {
  it("round-trips a record, encrypts it at rest, and deletes it", async () => {
    const t = 1000;
    const redis = new FakeRedis(() => t);
    const store = new RedisSessionStore(redis, SECRET, () => t);
    await store.set(record(t + 60_000));
    const stored = [...redis.data.values()][0]!.v;
    expect(stored).not.toContain("api-token-secret");
    expect(stored).not.toContain("refresh-secret");
    expect((await store.get("sid-1"))?.refreshToken).toBe("refresh-secret");
    await store.delete("sid-1");
    expect(await store.get("sid-1")).toBeNull();
  });

  it("expires records and rejects a value sealed with another key", async () => {
    let t = 1000;
    const redis = new FakeRedis(() => t);
    const store = new RedisSessionStore(redis, SECRET, () => t);
    await store.set(record(t + 1000));
    t += 2000;
    expect(await store.get("sid-1")).toBeNull();
    await new RedisSessionStore(redis, "z".repeat(40), () => t).set(record(t + 1000));
    expect(await store.get("sid-1")).toBeNull();
  });

  it("consumes a login state exactly once until it expires", async () => {
    let t = 0;
    const store = new RedisSessionStore(new FakeRedis(() => t), SECRET, () => t);
    expect(await store.consumeState("st", 100)).toBe(true);
    expect(await store.consumeState("st", 100)).toBe(false);
    t += 200;
    expect(await store.consumeState("st", 100)).toBe(true);
  });
});

describe("createSessionStore", () => {
  it("defaults to memory and needs a strong secret for Redis", () => {
    expect(createSessionStore({})).toBeInstanceOf(MemorySessionStore);
    expect(createSessionStore({ REDIS_URL: "redis://:pw@redis:6379/2", WEB_SESSION_SECRET: SECRET })).toBeInstanceOf(RedisSessionStore);
    expect(() => createSessionStore({ REDIS_URL: "redis://redis:6379", WEB_SESSION_SECRET: "short" })).toThrow();
  });
});

describe("RESP codec", () => {
  it("encodes commands and parses replies, including partial input", () => {
    expect(encodeCommand(["GET", "k"]).toString()).toBe("*2\r\n$3\r\nGET\r\n$1\r\nk\r\n");
    expect(parseReply(Buffer.from("+OK\r\n"))).toEqual({ value: "OK", next: 5 });
    expect(parseReply(Buffer.from("$-1\r\n"))?.value).toBeNull();
    expect(parseReply(Buffer.from("$5\r\nab"))).toBeNull();
    expect(parseReply(Buffer.from("*2\r\n:1\r\n$2\r\nhi\r\n"))?.value).toEqual([1, "hi"]);
  });
});
