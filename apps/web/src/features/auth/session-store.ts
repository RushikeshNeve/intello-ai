/**
 * Server-side session records. The browser only ever holds a sealed reference (`sid`) to one of these;
 * tokens live here and nowhere else.
 *
 * The default store is in process memory. It is correct for one web process (local dev, the single
 * `web` container in compose) and makes logout/revocation immediate: deleting the record kills every
 * copy of the cookie. A multi-instance deployment needs a shared implementation of `SessionStore`
 * (for example Postgres or Redis with the record encrypted at rest); nothing else changes.
 */
import { RespClient } from "./redis-client";
import { RedisSessionStore } from "./redis-session-store";

export type SessionUser = {
  readonly issuer: string;
  readonly subject: string;
  readonly email: string | null;
  readonly name: string | null;
};

export type SessionRecord = {
  readonly id: string;
  readonly createdAt: number;
  /** Absolute expiry (epoch ms). */
  readonly expiresAt: number;
  readonly user: SessionUser;
  /** Bearer token forwarded to the API; verified to carry the API audience. */
  apiToken: string;
  /** Epoch ms. */
  apiTokenExpiresAt: number;
  refreshToken: string | null;
};

export interface SessionStore {
  get(id: string): Promise<SessionRecord | null>;
  set(record: SessionRecord): Promise<void>;
  delete(id: string): Promise<void>;
  /** Marks a login `state` as used; returns false when it was already used (replay). */
  consumeState(state: string, ttlMs: number): Promise<boolean>;
}

export class MemorySessionStore implements SessionStore {
  private readonly sessions = new Map<string, SessionRecord>();
  private readonly usedStates = new Map<string, number>();
  private ops = 0;

  constructor(private readonly now: () => number = Date.now) {}

  async get(id: string): Promise<SessionRecord | null> {
    const record = this.sessions.get(id);
    if (!record) return null;
    if (record.expiresAt <= this.now()) {
      this.sessions.delete(id);
      return null;
    }
    return record;
  }

  async set(record: SessionRecord): Promise<void> {
    this.sessions.set(record.id, record);
    this.maybeSweep();
  }

  async delete(id: string): Promise<void> {
    this.sessions.delete(id);
  }

  async consumeState(state: string, ttlMs: number): Promise<boolean> {
    this.maybeSweep();
    const now = this.now();
    const until = this.usedStates.get(state);
    if (until !== undefined && until > now) return false;
    this.usedStates.set(state, now + ttlMs);
    return true;
  }

  get size(): number {
    return this.sessions.size;
  }

  private maybeSweep(): void {
    if (++this.ops % 100 !== 0) return;
    const now = this.now();
    for (const [id, r] of this.sessions) if (r.expiresAt <= now) this.sessions.delete(id);
    for (const [s, until] of this.usedStates) if (until <= now) this.usedStates.delete(s);
  }
}

/**
 * REDIS_URL set (production compose) -> Redis-backed store shared by every web instance and surviving
 * restarts; unset (dev, tests) -> in-process memory. WEB_SESSION_SECRET keys the at-rest encryption.
 */
export function createSessionStore(env: Record<string, string | undefined>): SessionStore {
  const url = env.REDIS_URL?.trim();
  if (!url) return new MemorySessionStore();
  const secret = env.WEB_SESSION_SECRET ?? "";
  if (secret.length < 32) throw new Error("REDIS_URL requires WEB_SESSION_SECRET (>= 32 chars) to encrypt stored sessions");
  return new RedisSessionStore(new RespClient(new URL(url)), secret);
}

const STORE_KEY = Symbol.for("ukos.web.sessionStore");
type GlobalWithStore = typeof globalThis & { [STORE_KEY]?: SessionStore };

/**
 * One store per process. Kept on `globalThis` because Next.js bundles the proxy and route handlers
 * separately; a module-level variable would give each bundle its own empty store.
 */
export function getSessionStore(): SessionStore {
  const g = globalThis as GlobalWithStore;
  g[STORE_KEY] ??= createSessionStore(process.env);
  return g[STORE_KEY];
}

/** Test hook. */
export function setSessionStore(store: SessionStore): void {
  (globalThis as GlobalWithStore)[STORE_KEY] = store;
}
