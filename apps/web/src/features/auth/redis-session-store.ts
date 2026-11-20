import { createCipheriv, createDecipheriv, hkdfSync, randomBytes } from "node:crypto";
import type { RedisCommandClient } from "./redis-client";
import type { SessionRecord, SessionStore } from "./session-store";

const SESSION_PREFIX = "ukos:web:sess:";
const STATE_PREFIX = "ukos:web:state:";

/**
 * Redis-backed `SessionStore` for multi-instance / restart-surviving sessions (REDIS_URL set).
 * Records hold bearer and refresh tokens, so each is AES-256-GCM encrypted with a key derived
 * (HKDF) from WEB_SESSION_SECRET before it is written: Redis persistence (AOF) never sees a token.
 * Expiry is enforced by Redis (PX) and re-checked on read.
 */
export class RedisSessionStore implements SessionStore {
  private readonly key: Buffer;

  constructor(
    private readonly client: RedisCommandClient,
    secret: string,
    private readonly now: () => number = Date.now,
  ) {
    this.key = Buffer.from(hkdfSync("sha256", secret, "ukos-web-session-store", "record-encryption-v1", 32));
  }

  async get(id: string): Promise<SessionRecord | null> {
    const raw = await this.client.command(["GET", SESSION_PREFIX + id]);
    if (typeof raw !== "string") return null;
    let record: SessionRecord;
    try {
      record = JSON.parse(this.open(raw)) as SessionRecord;
    } catch {
      return null; // wrong key, tampered or foreign value: treated as no session
    }
    if (record.expiresAt <= this.now()) {
      await this.delete(id);
      return null;
    }
    return record;
  }

  async set(record: SessionRecord): Promise<void> {
    const ttl = record.expiresAt - this.now();
    if (ttl <= 0) return this.delete(record.id);
    await this.client.command(["SET", SESSION_PREFIX + record.id, this.seal(JSON.stringify(record)), "PX", String(Math.ceil(ttl))]);
  }

  async delete(id: string): Promise<void> {
    await this.client.command(["DEL", SESSION_PREFIX + id]);
  }

  async consumeState(state: string, ttlMs: number): Promise<boolean> {
    const r = await this.client.command(["SET", STATE_PREFIX + state, "1", "NX", "PX", String(Math.max(1, Math.ceil(ttlMs)))]);
    return r === "OK";
  }

  private seal(plain: string): string {
    const iv = randomBytes(12);
    const c = createCipheriv("aes-256-gcm", this.key, iv);
    const enc = Buffer.concat([c.update(plain, "utf8"), c.final()]);
    return Buffer.concat([iv, c.getAuthTag(), enc]).toString("base64url");
  }

  private open(sealed: string): string {
    const b = Buffer.from(sealed, "base64url");
    const d = createDecipheriv("aes-256-gcm", this.key, b.subarray(0, 12));
    d.setAuthTag(b.subarray(12, 28));
    return Buffer.concat([d.update(b.subarray(28)), d.final()]).toString("utf8");
  }
}
