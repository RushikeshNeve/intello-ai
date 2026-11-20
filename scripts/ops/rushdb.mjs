#!/usr/bin/env node
// RushDB per-tenant snapshot for backup (before the cold copy) and restore verification (after it).
// Speaks the same HTTP protocol as the API adapter, with the operator token (never printed, never stored).
//   node rushdb.mjs snapshot <url> <token> <orgId,orgId,...> [--probe-from <snapshot.json>]
// Output (stdout JSON): { "<orgId>": { chunks, published, indexes: [{name,state}], probe: {term, hits} } }
// With --probe-from, the probe term of each org is taken from an earlier snapshot (the backup's) instead of chosen.
import { readFile } from "node:fs/promises";

const [cmd, url, token, orgList, ...rest] = process.argv.slice(2);
const probeFrom = rest.indexOf("--probe-from") >= 0 ? rest[rest.indexOf("--probe-from") + 1] : null;

async function rdb(orgId, operation) {
  const tenant = `org:${orgId}`;
  const res = await fetch(`${url}/protocol/v1/execute`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "x-rushdb-tenant": tenant, "content-type": "application/json" },
    body: JSON.stringify({ protocolVersion: 1, context: { scope: tenant }, operation }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.error) throw new Error(`rushdb ${res.status} ${JSON.stringify(body.error ?? body).slice(0, 160)}`);
  return body.response;
}
const records = (orgId, query) => rdb(orgId, { kind: "query", query: { kind: "records", query: { labels: ["Chunk"], ...query } } });

async function snapshotOrg(orgId, previous) {
  const total = (await records(orgId, { select: { n: { $count: "*" } } })).rows?.[0]?.n ?? 0;
  const published = (await records(orgId, { predicate: { published: true }, select: { n: { $count: "*" } } })).rows?.[0]?.n ?? 0;
  const described = await rdb(orgId, { kind: "describe", describe: { kind: "indexes" } });
  const indexes = (described.indexes ?? []).map((i) => ({ name: i.name, state: i.state })).sort((a, b) => a.name.localeCompare(b.name));
  let term = previous?.probe?.term ?? null;
  if (!term && total > 0) {
    const r = await records(orgId, { limit: 1, select: { text: "text" } });
    const words = String(r.rows?.[0]?.text ?? "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length >= 6);
    term = words.sort((a, b) => b.length - a.length)[0] ?? null;
  }
  let hits = null;
  if (term) {
    const h = await rdb(orgId, { kind: "query", query: { kind: "hybrid", query: { labels: ["Chunk"], limit: 50, predicate: { published: true }, text: { index: "chunk-text", query: term } } } });
    hits = (h.records ?? []).length;
  }
  return { chunks: total, published, indexes, probe: { term, hits } };
}

if (cmd === "snapshot") {
  const prev = probeFrom ? JSON.parse(await readFile(probeFrom, "utf8")) : {};
  const out = {};
  for (const org of orgList.split(",").filter(Boolean)) out[org] = await snapshotOrg(org, prev[org]);
  console.log(JSON.stringify(out));
} else {
  console.error("usage: rushdb.mjs snapshot <url> <token> <orgIds> [--probe-from file]");
  process.exit(2);
}
