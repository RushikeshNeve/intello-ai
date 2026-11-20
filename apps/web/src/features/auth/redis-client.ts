import net from "node:net";
import tls from "node:tls";

/**
 * Minimal Redis (RESP2) client: just enough for the session store (AUTH, SELECT, GET, SET, DEL).
 * Written instead of adding `ioredis`/`redis`: the store needs five commands, the dependency tree and
 * the standalone Next.js bundle stay unchanged, and the store is tested against the narrow
 * `RedisCommandClient` interface below. One socket, commands are pipelined in order, reconnect on demand.
 * Not verified against a live Redis in this repo's CI (see docs/runbooks/deploy-free-vm.md).
 */
export interface RedisCommandClient {
  command(args: readonly string[]): Promise<unknown>;
}

export function encodeCommand(args: readonly string[]): Buffer {
  const parts: Buffer[] = [Buffer.from(`*${args.length}\r\n`)];
  for (const a of args) {
    const b = Buffer.from(a, "utf8");
    parts.push(Buffer.from(`$${b.length}\r\n`), b, Buffer.from("\r\n"));
  }
  return Buffer.concat(parts);
}

export class RedisError extends Error {}

type Parsed = { value: unknown; next: number } | null;

/** Parses one RESP2 reply at `offset`; null when the buffer holds only part of it. */
export function parseReply(buf: Buffer, offset = 0): Parsed {
  if (offset >= buf.length) return null;
  const end = buf.indexOf("\r\n", offset);
  if (end < 0) return null;
  const type = String.fromCharCode(buf[offset]!);
  const line = buf.toString("utf8", offset + 1, end);
  const after = end + 2;
  switch (type) {
    case "+":
      return { value: line, next: after };
    case "-":
      return { value: new RedisError(line), next: after };
    case ":":
      return { value: Number(line), next: after };
    case "$": {
      const len = Number(line);
      if (len < 0) return { value: null, next: after };
      if (buf.length < after + len + 2) return null;
      return { value: buf.toString("utf8", after, after + len), next: after + len + 2 };
    }
    case "*": {
      const n = Number(line);
      if (n < 0) return { value: null, next: after };
      const items: unknown[] = [];
      let at = after;
      for (let i = 0; i < n; i++) {
        const r = parseReply(buf, at);
        if (!r) return null;
        items.push(r.value);
        at = r.next;
      }
      return { value: items, next: at };
    }
    default:
      throw new RedisError(`unexpected RESP type ${type}`);
  }
}

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void };

export class RespClient implements RedisCommandClient {
  private socket: net.Socket | null = null;
  private connecting: Promise<net.Socket> | null = null;
  private buffer: Buffer = Buffer.alloc(0);
  private pending: Pending[] = [];

  constructor(
    private readonly url: URL,
    private readonly timeoutMs = 3000,
  ) {}

  async command(args: readonly string[]): Promise<unknown> {
    const socket = await this.connect();
    return this.send(socket, args);
  }

  private send(socket: net.Socket, args: readonly string[]): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => socket.destroy(new Error("redis command timed out")), this.timeoutMs);
      this.pending.push({
        resolve: (v) => (clearTimeout(timer), resolve(v)),
        reject: (e) => (clearTimeout(timer), reject(e)),
      });
      socket.write(encodeCommand(args));
    });
  }

  private connect(): Promise<net.Socket> {
    if (this.socket && !this.socket.destroyed) return Promise.resolve(this.socket);
    this.connecting ??= this.open().finally(() => {
      this.connecting = null;
    });
    return this.connecting;
  }

  private async open(): Promise<net.Socket> {
    const secure = this.url.protocol === "rediss:";
    const port = Number(this.url.port || 6379);
    const host = this.url.hostname;
    const socket = await new Promise<net.Socket>((resolve, reject) => {
      const s = secure ? tls.connect({ host, port, servername: host }) : net.connect({ host, port });
      const timer = setTimeout(() => s.destroy(new Error("redis connect timed out")), this.timeoutMs);
      s.once(secure ? "secureConnect" : "connect", () => (clearTimeout(timer), resolve(s)));
      s.once("error", (e) => (clearTimeout(timer), reject(e)));
    });
    this.buffer = Buffer.alloc(0);
    socket.on("data", (d) => this.onData(d));
    const fail = (e: Error) => {
      const waiting = this.pending;
      this.pending = [];
      for (const p of waiting) p.reject(e);
    };
    socket.on("error", fail);
    socket.on("close", () => fail(new Error("redis connection closed")));
    this.socket = socket;
    try {
      const password = decodeURIComponent(this.url.password);
      if (password) {
        const user = decodeURIComponent(this.url.username);
        await this.send(socket, user ? ["AUTH", user, password] : ["AUTH", password]);
      }
      const db = this.url.pathname.replace(/^\//, "");
      if (db) await this.send(socket, ["SELECT", db]);
    } catch (e) {
      socket.destroy();
      throw e;
    }
    return socket;
  }

  private onData(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      let r: Parsed;
      try {
        r = parseReply(this.buffer);
      } catch (e) {
        this.socket?.destroy(e as Error);
        return;
      }
      if (!r) return;
      this.buffer = this.buffer.subarray(r.next);
      const p = this.pending.shift();
      if (!p) continue;
      if (r.value instanceof RedisError) p.reject(r.value);
      else p.resolve(r.value);
    }
  }
}
