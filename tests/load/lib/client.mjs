// Browser-like HTTP client for the ops scripts (login through Dex, BFF cookie session, uploads, polling, percentiles).
// Dependency-free Node 20+. Condensed from tests/e2e/fullstack_m03/fullstack.test.mjs.
import { createHash, randomBytes } from "node:crypto";
import { execSync } from "node:child_process";

export const WEB = process.env.WEB_URL ?? "http://127.0.0.1:33682";
export const PROJECT = process.env.PROJECT ?? "ukos-m08-b-load";
const SESSION_COOKIE = "ukos_session";
const TX_COOKIE = "ukos_auth_tx";
const USERS = {
  alice: { login: "alice@ukos.test", password: "ukos-dev-alice-password" },
  bob: { login: "bob@ukos.test", password: "ukos-dev-bob-password" },
};
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const sha256hex = (b) => createHash("sha256").update(b).digest("hex");
export const suffix = randomBytes(3).toString("hex");

const realFetch = globalThis.fetch;
globalThis.fetch = async (...args) => {
  try { return await realFetch(...args); } catch (e) {
    if (e instanceof TypeError && /ECONNRESET|UND_ERR_SOCKET|EPIPE/.test(e?.cause?.code ?? "")) { await sleep(150); return realFetch(...args); }
    throw e;
  }
};

export async function waitFor(fn, { timeout = 60_000, interval = 500, what = "condition" } = {}) {
  const end = Date.now() + timeout;
  let last;
  for (;;) {
    try { last = await fn(); if (last) return last; } catch (e) { last = `threw: ${e.message}`; }
    if (Date.now() > end) throw new Error(`timeout (${timeout} ms) waiting for ${what}; last = ${JSON.stringify(last)}`);
    await sleep(interval);
  }
}

export const sh = (cmd, opts = {}) => execSync(cmd, { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"], ...opts }).trim();
export const sql = (query) => sh(`docker exec -i ${PROJECT}-postgres-1 psql -U ukos_admin -d ukos -At`, { input: query });

function parseSetCookies(list) {
  const out = {};
  for (const raw of list) { const [pair] = raw.split(";"); const eq = pair.indexOf("="); out[pair.slice(0, eq).trim()] = pair.slice(eq + 1); }
  return out;
}

/** One request through the web BFF. Returns { status, body, text, ms, headers }. */
export async function web(path, { session, method = "GET", json, headers: extra = {}, extraCookies = {} } = {}) {
  const headers = { ...extra };
  const cookies = { ...extraCookies, ...(session ? { [SESSION_COOKIE]: session } : {}) };
  const cookie = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join("; ");
  if (cookie) headers.cookie = cookie;
  if (method !== "GET") Object.assign(headers, { origin: WEB, "sec-fetch-site": "same-origin" });
  if (json !== undefined) headers["content-type"] = "application/json";
  const t0 = performance.now();
  const res = await fetch(path.startsWith("http") ? path : WEB + path, { method, redirect: "manual", headers, body: json === undefined ? undefined : JSON.stringify(json) });
  const text = await res.text();
  const ms = performance.now() - t0;
  let body = null;
  try { body = JSON.parse(text); } catch { /* not JSON */ }
  return { status: res.status, body, text, ms, headers: res.headers, location: res.headers.get("location"), setCookies: parseSetCookies(res.headers.getSetCookie()) };
}

const unescapeHtml = (s) => s.replace(/&amp;/g, "&").replace(/&#43;/g, "+").replace(/&quot;/g, '"');

async function dexLogin(authorizationUrl, creds) {
  let url = authorizationUrl;
  let html = "";
  for (let i = 0; i < 5; i++) {
    const res = await fetch(url, { redirect: "manual" });
    if (res.status >= 300 && res.status < 400) { url = new URL(res.headers.get("location"), url).href; continue; }
    html = await res.text();
    break;
  }
  const m = html.match(/<form method="post" action="([^"]+)"/);
  if (!m) throw new Error("Dex login form not found");
  const action = new URL(unescapeHtml(m[1]), url).href;
  let res = await fetch(action, { method: "POST", redirect: "manual", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(creds).toString() });
  let cur = action;
  for (let i = 0; i < 5 && res.status >= 300 && res.status < 400; i++) {
    const next = new URL(res.headers.get("location"), cur).href;
    if (next.startsWith(`${WEB}/api/auth/callback`)) return next;
    cur = next;
    res = await fetch(cur, { redirect: "manual" });
  }
  throw new Error(`Dex login failed (${res.status})`);
}

export async function login(who = "alice") {
  const start = await web(`/api/auth/login?returnTo=${encodeURIComponent("/home")}`);
  if (start.status !== 302) throw new Error(`login start: ${start.status}`);
  const callback = await dexLogin(start.location, { login: USERS[who].login, password: USERS[who].password });
  const cb = await web(callback, { extraCookies: { [TX_COOKIE]: start.setCookies[TX_COOKIE] } });
  if (!cb.setCookies[SESSION_COOKIE]) throw new Error("no session cookie");
  return cb.setCookies[SESSION_COOKIE];
}

export async function newTenant(session, tag) {
  const slug = `${tag}-${suffix}`;
  const org = await web("/api/v1/orgs", { session, method: "POST", json: { slug, name: `Ops ${slug}` } });
  if (org.status !== 201) throw new Error(`org: ${org.status} ${org.text}`);
  const w = await web(`/api/v1/orgs/${org.body.id}/workspaces`, { session, method: "POST", json: { slug: "lab", name: "Lab" } });
  if (w.status !== 201) throw new Error(`workspace: ${w.status} ${w.text}`);
  return { orgId: org.body.id, wsId: w.body.id, wsPath: `/api/v1/orgs/${org.body.id}/workspaces/${w.body.id}`, session };
}

/** create session -> PUT to the object store -> finalize. Returns ids and the finalize latency. */
export async function uploadFile(t, { filename, contentType = "text/plain", bytes, title }) {
  const created = await web(`${t.wsPath}/uploads`, { session: t.session, method: "POST",
    json: { filename, content_type: contentType, size_bytes: bytes.length, sha256: sha256hex(bytes), ...(title ? { title } : {}) } });
  if (created.status !== 201) throw new Error(`upload session: ${created.status} ${created.text.slice(0, 200)}`);
  const put = await fetch(created.body.upload.url, { method: created.body.upload.method, headers: created.body.upload.headers, body: bytes });
  if (put.status !== 200) throw new Error(`object store PUT: ${put.status}`);
  const fin = await web(`${t.wsPath}/uploads/${created.body.upload_id}/finalize`, { session: t.session, method: "POST" });
  if (fin.status !== 201) throw new Error(`finalize: ${fin.status} ${fin.text.slice(0, 200)}`);
  return { docId: fin.body.document.id, sha256: sha256hex(bytes), bytes: bytes.length };
}

export const ingestionState = (t, docId) => web(`${t.wsPath}/documents/${docId}/ingestion`, { session: t.session });
export async function waitReady(t, docId, timeout = 180_000) {
  return waitFor(async () => { const r = await ingestionState(t, docId); return r.status === 200 && ["ready", "failed", "unsupported", "no_text", "cancelled"].includes(r.body.state) ? r.body : false; },
    { timeout, interval: 400, what: `document ${docId} to finish ingestion` });
}

export function paragraphs(n, marker, chars = 700) {
  const out = [];
  for (let i = 0; i < n; i++) {
    let p = `${marker} section ${i}. `;
    while (p.length < chars) p += `alpha${i} beta${(i * 7) % 13} gamma${(i * 11) % 17} delta${randomBytes(2).toString("hex")} `;
    out.push(p.trim());
  }
  return out;
}
export const textDoc = (marker, n = 3) => Buffer.from(`${marker}\n\n${paragraphs(n, marker).join("\n\n")}\n`);

export const search = (t, query, limit = 5) => web(`/api/v1/orgs/${t.orgId}/search`, { session: t.session, method: "POST", json: { query, limit } });

/** Chat run: thread, run, poll until terminal. Returns the terminal state (or "timeout") and the latency in ms. */
export async function chatRun(t, content, timeout = 60_000) {
  const t0 = performance.now();
  const th = await web(`/api/v1/orgs/${t.orgId}/chat/threads`, { session: t.session, method: "POST", json: { workspace_id: t.wsId, title: "load" } });
  if (th.status !== 201) return { state: `thread_${th.status}`, ms: performance.now() - t0 };
  const run = await web(`/api/v1/orgs/${t.orgId}/chat/threads/${th.body.id}/runs`, { session: t.session, method: "POST",
    json: { content, idempotency_key: `k-${randomBytes(8).toString("hex")}`, workspace_ids: [t.wsId] } });
  if (run.status !== 202) return { state: `run_${run.status}`, ms: performance.now() - t0 };
  const end = Date.now() + timeout;
  for (;;) {
    const r = await web(`/api/v1/orgs/${t.orgId}/chat/runs/${run.body.run_id}`, { session: t.session });
    const st = r.body?.state;
    if (["succeeded", "completed", "failed", "cancelled"].includes(st)) return { state: st, ms: performance.now() - t0 };
    if (Date.now() > end) return { state: "timeout", ms: performance.now() - t0 };
    await sleep(250);
  }
}

// ---- statistics ----------------------------------------------------------------------------------------
export function pct(values, p) {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.ceil((p / 100) * s.length) - 1)];
}
export const summarize = (values) => ({ n: values.length, p50: pct(values, 50), p95: pct(values, 95), p99: pct(values, 99), max: values.length ? Math.max(...values) : null });
export const fmt = (n) => (n == null ? "-" : n.toFixed(0));

/** Run `total` tasks with at most `concurrency` in flight. Each task returns {ok, ms, ...}. */
export async function runPool(total, concurrency, task) {
  const results = [];
  let next = 0;
  const t0 = performance.now();
  await Promise.all(Array.from({ length: concurrency }, async () => {
    for (;;) { const i = next++; if (i >= total) return; try { results.push(await task(i)); } catch (e) { results.push({ ok: false, ms: 0, error: String(e.message).slice(0, 120) }); } }
  }));
  const wall = (performance.now() - t0) / 1000;
  const ok = results.filter((r) => r.ok);
  return { total, concurrency, wallSeconds: wall, errors: results.length - ok.length, errorRate: (results.length - ok.length) / results.length,
    throughput: results.length / wall, latency: summarize(ok.map((r) => r.ms)), failures: results.filter((r) => !r.ok).slice(0, 5) };
}
