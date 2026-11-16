import { sealData, unsealData } from "iron-session";
import type { AuthConfig } from "./config";
import { TX_TTL_SECONDS } from "./cookies";

/**
 * Cookie payloads are sealed with iron-session's `sealData` (AES-256-CBC + HMAC-SHA256 via
 * iron-webcrypto, with an embedded expiry). The session cookie carries only `{ sid }`, an opaque
 * 256-bit id of a server-side record; the transaction cookie carries the per-login PKCE verifier,
 * state and nonce, which must stay confidential until the callback.
 */
export type SessionRef = { v: 1; sid: string };
export type LoginTransaction = {
  v: 1;
  state: string;
  nonce: string;
  codeVerifier: string;
  returnTo: string;
  createdAt: number;
};

export async function sealSessionRef(config: AuthConfig, sid: string): Promise<string> {
  return sealData({ v: 1, sid } satisfies SessionRef, {
    password: config.sessionSecret,
    ttl: config.sessionTtlSeconds,
  });
}

export async function unsealSessionRef(config: AuthConfig, seal: string | undefined): Promise<string | null> {
  if (!seal) return null;
  try {
    const data = await unsealData<Partial<SessionRef>>(seal, {
      password: config.sessionSecret,
      ttl: config.sessionTtlSeconds,
    });
    return data?.v === 1 && typeof data.sid === "string" && data.sid.length >= 32 ? data.sid : null;
  } catch {
    return null;
  }
}

export async function sealTransaction(config: AuthConfig, tx: LoginTransaction): Promise<string> {
  return sealData(tx, { password: config.sessionSecret, ttl: TX_TTL_SECONDS });
}

export async function unsealTransaction(
  config: AuthConfig,
  seal: string | undefined,
): Promise<LoginTransaction | null> {
  if (!seal) return null;
  try {
    const tx = await unsealData<Partial<LoginTransaction>>(seal, {
      password: config.sessionSecret,
      ttl: TX_TTL_SECONDS,
    });
    if (
      tx?.v === 1 &&
      typeof tx.state === "string" &&
      typeof tx.nonce === "string" &&
      typeof tx.codeVerifier === "string" &&
      typeof tx.returnTo === "string" &&
      typeof tx.createdAt === "number"
    ) {
      return tx as LoginTransaction;
    }
    return null;
  } catch {
    return null;
  }
}
