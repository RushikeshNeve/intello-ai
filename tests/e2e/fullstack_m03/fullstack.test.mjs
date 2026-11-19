// M03 gate (also task M03.03.05): REAL web (BFF) + API + Dex + PostgreSQL + Redis + RustFS + RushDB + the
// `worker` and `dispatcher` containers, scripted over HTTP like a browser. API calls go through the web BFF
// (cookie session); file bytes go DIRECTLY to the object store through the presigned URL. The embedding server
// is the fault-injecting stub (tests/e2e/support/embedding-stub.mjs), owned by this file so that scenario 6d
// can stop it (connection refused) and start it again. Run through ./run.sh.
//
// Verification "under the hood" uses `docker exec ... psql` as the admin role, the RushDB HTTP protocol
// directly (same wire format as the adapter), and an aws-cli container for the artifacts bucket.
import { test as nodeTest, after } from "node:test";
import assert from "node:assert/strict";
import { execSync, spawn } from "node:child_process";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

const WEB = process.env.WEB_URL ?? "http://127.0.0.1:33600";
const PROJECT = process.env.PROJECT ?? "ukos-e2e-m03";
const STUB_PORT = process.env.STUB_PORT ?? "18334";
const STUB = process.env.STUB_URL ?? `http://127.0.0.1:${STUB_PORT}`;
const STUB_PATH = fileURLToPath(new URL("../support/embedding-stub.mjs", import.meta.url));
const RUSHDB = process.env.RUSHDB_HOST_URL ?? "http://127.0.0.1:33610";
const RUSHDB_TOKEN = process.env.RUSHDB_TOKEN ?? "change-me-rushdb-token";
const LEASE_S = Number(process.env.INGEST_LEASE_SECONDS ?? 20);
const MAX_ATTEMPTS = Number(process.env.INGEST_MAX_ATTEMPTS ?? 4);
const CONCURRENCY = Number(process.env.INGEST_CONCURRENCY ?? 2);
const S3_KEY = process.env.S3_ACCESS_KEY_ID ?? "change-me-s3-access-key";
const S3_SECRET = process.env.S3_SECRET_ACCESS_KEY ?? "change-me-s3-secret-key";
const SESSION_COOKIE = "ukos_session";
const TX_COOKIE = "ukos_auth_tx";
const JWT_RE = /eyJ[\w-]+\.[\w-]+\.[\w-]+/;
const USERS = {
  alice: { login: "alice@ukos.test", password: "ukos-dev-alice-password" },
  bob: { login: "bob@ukos.test", password: "ukos-dev-bob-password" },
  mallory: { login: "mallory@ukos.test", password: "ukos-dev-mallory-password" },
};
const transcript = []; // everything the web app returned to the "browser" (API responses + SSE)
const report = {}; // honest numbers, printed at the end

// ---------------------------------------------------------------------------------------------
// generic helpers
// ---------------------------------------------------------------------------------------------
// Print a failure the moment it happens (the spec reporter only prints details at the very end).
const test = (name, fn) => nodeTest(name, async (t) => {
  try { await fn(t); } catch (e) { console.error("FAILED " + name + " :: cause=" + (e?.cause?.code ?? e?.cause?.message ?? "") + " :: " + String(e.stack ?? e).slice(0, 1500).split("\n").join(" | ")); throw e; }
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Node's fetch reuses keep-alive sockets; a server that closed an idle one yields "fetch failed" (ECONNRESET /
// UND_ERR_SOCKET) on the very next request. Retry once on exactly that, never on an HTTP-level answer.
const realFetch = globalThis.fetch;
globalThis.fetch = async (...args) => {
  try {
    return await realFetch(...args);
  } catch (e) {
    const code = e?.cause?.code ?? "";
    if (e instanceof TypeError && /ECONNRESET|UND_ERR_SOCKET|EPIPE/.test(code)) { await sleep(150); return realFetch(...args); }
    throw e;
  }
};

async function waitFor(fn, { timeout = 60_000, interval = 500, what = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  let last;
  for (;;) {
    try {
      last = await fn();
      if (last) return last;
    } catch (e) {
      last = `threw: ${e.message}`;
    }
    if (Date.now() > deadline) throw new Error(`timeout (${timeout} ms) waiting for ${what}; last = ${JSON.stringify(last)}`);
    await sleep(interval);
  }
}

function sh(cmd, opts = {}) {
  return execSync(cmd, { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"], ...opts }).trim();
}

/** Admin SQL through the postgres container (superuser: RLS does not apply, like the M02 gate). */
function sql(query) {
  return sh(`docker exec -i ${PROJECT}-postgres-1 psql -U ukos_admin -d ukos -At`, { input: query });
}

/** SQL as another role over TCP (password auth), e.g. ukos_worker, to prove what it can and cannot read. */
function sqlAs(user, password, query) {
  return sh(`docker exec -i -e PGPASSWORD=${password} ${PROJECT}-postgres-1 psql -h 127.0.0.1 -U ${user} -d ukos -At -v ON_ERROR_STOP=0 2>&1`, { input: query });
}

const docker = (args) => sh(`docker ${args}`);
const ctr = (service) => `${PROJECT}-${service}-1`;

function s3ls(prefix, bucket = "ukos-artifacts") {
  try {
    return sh(
      `docker run --rm --network ${PROJECT}_default -e AWS_ACCESS_KEY_ID=${S3_KEY} -e AWS_SECRET_ACCESS_KEY=${S3_SECRET} ` +
        `-e AWS_DEFAULT_REGION=us-east-1 amazon/aws-cli:2.40.3 --endpoint-url http://objectstore:9000 s3 ls s3://${bucket}/${prefix} --recursive`,
    );
  } catch {
    return ""; // aws s3 ls exits 1 when nothing matches
  }
}

// ---------------------------------------------------------------------------------------------
// embedding stub: owned by this file
// ---------------------------------------------------------------------------------------------
let stubProc = null;
async function stubUp() {
  if (stubProc) return;
  stubProc = spawn(process.execPath, [STUB_PATH], {
    env: { ...process.env, PORT: STUB_PORT, HOST: "0.0.0.0", DIMENSIONS: "768", MODEL: "google/embedding-gemma-300m" },
    stdio: "ignore",
  });
  stubProc.on("exit", () => { stubProc = null; });
  await waitFor(async () => (await fetch(`${STUB}/health`)).ok, { timeout: 15_000, interval: 200, what: "stub health" });
}
async function stubDown() {
  if (!stubProc) return;
  const p = stubProc;
  const exited = new Promise((r) => p.once("exit", r));
  p.kill();
  await exited;
  stubProc = null;
}
const stub = async (control) => (await fetch(`${STUB}/__control`, { method: "POST", body: JSON.stringify(control) })).json();
const stubStats = async () => (await (await fetch(`${STUB}/__stats`)).json()).stats;
const stubReset = () => stub({ reset: true });
process.on("exit", () => { stubProc?.kill(); });
after(async () => { await stubDown(); });

// ---------------------------------------------------------------------------------------------
// HTTP helpers (browser-like), copied from the M02 gate
// ---------------------------------------------------------------------------------------------
function parseSetCookies(list) {
  const out = {};
  for (const raw of list) {
    const [pair] = raw.split(";");
    const eq = pair.indexOf("=");
    out[pair.slice(0, eq).trim()] = pair.slice(eq + 1);
  }
  return out;
}

async function web(path, { session, method = "GET", json, extraCookies = {}, headers: extra = {} } = {}) {
  const cookies = { ...extraCookies, ...(session ? { [SESSION_COOKIE]: session } : {}) };
  const headers = { ...extra };
  const cookieHeader = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join("; ");
  if (cookieHeader) headers.cookie = cookieHeader;
  if (method !== "GET") Object.assign(headers, { origin: WEB, "sec-fetch-site": "same-origin" });
  if (json !== undefined) headers["content-type"] = "application/json";
  const res = await fetch(path.startsWith("http") ? path : WEB + path, {
    method, redirect: "manual", headers, body: json === undefined ? undefined : JSON.stringify(json),
  });
  const text = await res.text();
  transcript.push({ path, method, status: res.status, headers: [...res.headers.entries()], body: text });
  let body = null;
  try { body = JSON.parse(text); } catch { /* HTML */ }
  return { status: res.status, text, body, location: res.headers.get("location"), setCookies: parseSetCookies(res.headers.getSetCookie()) };
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
  assert.ok(m, "Dex login form present");
  const action = new URL(unescapeHtml(m[1]), url).href;
  let res = await fetch(action, {
    method: "POST", redirect: "manual",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(creds).toString(),
  });
  let cur = action;
  for (let i = 0; i < 5 && res.status >= 300 && res.status < 400; i++) {
    const next = new URL(res.headers.get("location"), cur).href;
    if (next.startsWith(`${WEB}/api/auth/callback`)) return next;
    cur = next;
    res = await fetch(cur, { redirect: "manual" });
  }
  throw new Error(`Dex login failed (${res.status})`);
}

async function login(who, returnTo = "/home") {
  const start = await web(`/api/auth/login?returnTo=${encodeURIComponent(returnTo)}`);
  assert.equal(start.status, 302, "login starts with a redirect to Dex");
  const callback = await dexLogin(start.location, { login: USERS[who].login, password: USERS[who].password });
  const cb = await web(callback, { extraCookies: { [TX_COOKIE]: start.setCookies[TX_COOKIE] } });
  const session = cb.setCookies[SESSION_COOKIE];
  assert.ok(session, `${who} got a session cookie`);
  return session;
}

/** Server-sent events through the BFF; resolves once `until(frames)` is true, the stream ends, or the timeout. */
async function readSse(path, session, { until = () => false, timeout = 60_000, headers = {} } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  const res = await fetch(WEB + path, {
    headers: { cookie: `${SESSION_COOKIE}=${session}`, accept: "text/event-stream", ...headers },
    signal: ctrl.signal,
  });
  if (!res.ok || !(res.headers.get("content-type") ?? "").includes("text/event-stream")) {
    const text = await res.text();
    clearTimeout(timer);
    transcript.push({ path, method: "GET", status: res.status, headers: [...res.headers.entries()], body: text });
    return { status: res.status, frames: [], text };
  }
  const frames = [];
  let buf = "";
  let raw = "";
  const decoder = new TextDecoder();
  try {
    for await (const chunk of res.body) {
      const piece = decoder.decode(chunk, { stream: true });
      buf += piece;
      raw += piece;
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const frame = { event: "message", id: null, data: null, comment: null };
        for (const line of block.split("\n")) {
          if (line.startsWith(":")) frame.comment = line.slice(1).trim();
          else if (line.startsWith("event:")) frame.event = line.slice(6).trim();
          else if (line.startsWith("id:")) frame.id = line.slice(3).trim();
          else if (line.startsWith("data:")) frame.data = (frame.data ?? "") + line.slice(5).trim();
        }
        frames.push(frame);
      }
      if (until(frames)) break;
    }
  } catch (e) {
    if (e.name !== "AbortError") throw e;
  } finally {
    clearTimeout(timer);
    ctrl.abort();
  }
  transcript.push({ path, method: "GET", status: res.status, headers: [...res.headers.entries()], body: raw });
  return { status: res.status, frames, text: raw };
}

// ---------------------------------------------------------------------------------------------
// RushDB, directly over its HTTP protocol (same wire format as app/infrastructure/rushdb/repository.py)
// ---------------------------------------------------------------------------------------------
async function rdb(orgId, operation) {
  const tenant = `org:${orgId}`;
  const res = await fetch(`${RUSHDB}/protocol/v1/execute`, {
    method: "POST",
    headers: { authorization: `Bearer ${RUSHDB_TOKEN}`, "x-rushdb-tenant": tenant, "content-type": "application/json" },
    body: JSON.stringify({ protocolVersion: 1, context: { scope: tenant }, operation }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.error) throw new Error(`rushdb ${res.status} ${JSON.stringify(body.error ?? body).slice(0, 200)}`);
  return body.response;
}
const recordsQuery = (query) => ({ kind: "query", query: { kind: "records", query: { labels: ["Chunk"], ...query } } });
async function rCount(orgId, predicate) {
  const r = await rdb(orgId, recordsQuery({ predicate, select: { n: { $count: "*" } } }));
  return r.rows?.[0]?.n ?? 0;
}
async function rChunks(orgId, predicate) {
  const out = [];
  for (let offset = 0; ; offset += 500) {
    const r = await rdb(orgId, recordsQuery({
      predicate, offset, limit: 500, order: { chunk_index: "asc" },
      select: { chunk_id: "chunk_id", revision_id: "revision_id", document_id: "document_id", published: "published", chunk_index: "chunk_index" },
    }));
    out.push(...(r.rows ?? []));
    if ((r.rows ?? []).length < 500) return out;
  }
}
const scopedPredicate = (wsIds, extra = {}) => ({ workspace_id: { $in: [...wsIds].sort() }, published: true, ...extra });
async function rKeyword(orgId, wsIds, text, limit = 10) {
  const r = await rdb(orgId, { kind: "query", query: { kind: "hybrid", query: {
    labels: ["Chunk"], limit, predicate: scopedPredicate(wsIds), text: { index: "chunk-text", query: text } } } });
  return r.records ?? [];
}
async function embedQuery(text) {
  const res = await fetch(`${STUB}/v1/embeddings`, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "google/embedding-gemma-300m", input: [`task: search result | query: ${text}`] }) });
  return (await res.json()).data[0].embedding;
}
async function rVector(orgId, wsIds, space, text, limit = 5) {
  const vector = await embedQuery(text);
  const r = await rdb(orgId, { kind: "query", query: { kind: "vector", query: {
    labels: ["Chunk"], property: "embedding", index: `chunk-emb-${space}`, vector, limit, mode: "auto",
    predicate: scopedPredicate(wsIds, { embedding_space_id: space }) } } });
  return r.records ?? [];
}
const vectorStats = async (orgId, space) =>
  (await rdb(orgId, { kind: "describe", describe: { kind: "vectorIndex", name: `chunk-emb-${space}` } })).stats;

// ---------------------------------------------------------------------------------------------
// documents, tenants, uploads
// ---------------------------------------------------------------------------------------------
const sha256hex = (bytes) => createHash("sha256").update(bytes).digest("hex");
const suffix = randomBytes(3).toString("hex");
const sessions = {};
const ws = {}; // tenants by tag

async function newTenant(tag, { owner = "alice", viewer = null } = {}) {
  const slug = `${tag}-${suffix}`;
  const org = await web("/api/v1/orgs", { session: sessions[owner], method: "POST", json: { slug, name: `Gate ${slug}` } });
  assert.equal(org.status, 201, org.text);
  const wsr = await web(`/api/v1/orgs/${org.body.id}/workspaces`, { session: sessions[owner], method: "POST", json: { slug: "lab", name: "Lab" } });
  assert.equal(wsr.status, 201, wsr.text);
  const t = { tag, orgId: org.body.id, wsId: wsr.body.id, slug, owner, wsPath: `/api/v1/orgs/${org.body.id}/workspaces/${wsr.body.id}` };
  if (viewer) {
    assert.equal((await web(`/api/v1/orgs/${t.orgId}/members/${sessions[`${viewer}Id`]}`, { session: sessions[owner], method: "PUT", json: { role: "member" } })).status, 200);
    assert.equal((await web(`${t.wsPath}/members/${sessions[`${viewer}Id`]}`, { session: sessions[owner], method: "PUT", json: { role: "viewer" } })).status, 200);
  }
  ws[tag] = t;
  return t;
}

async function uploadFile(t, spec) {
  const session = sessions[spec.as ?? t.owner];
  const bytes = spec.bytes;
  const created = await web(`${t.wsPath}/uploads`, {
    session, method: "POST",
    json: { filename: spec.filename, content_type: spec.contentType, size_bytes: bytes.length, sha256: sha256hex(bytes),
      ...(spec.documentId ? { document_id: spec.documentId } : {}), ...(spec.title ? { title: spec.title } : {}) },
  });
  assert.equal(created.status, 201, `create session: ${created.text}`);
  const put = await fetch(created.body.upload.url, { method: created.body.upload.method, headers: created.body.upload.headers, body: bytes });
  assert.equal(put.status, 200, `PUT to the object store: ${await put.text()}`);
  const startedAt = Date.now();
  const fin = await web(`${t.wsPath}/uploads/${created.body.upload_id}/finalize`, { session, method: "POST" });
  assert.equal(fin.status, 201, `finalize: ${fin.text}`);
  return { uploadId: created.body.upload_id, docId: fin.body.document.id, versionId: fin.body.version.id, fin, startedAt };
}

const status = (t, docId, who) => web(`${t.wsPath}/documents/${docId}/ingestion`, { session: sessions[who ?? t.owner] });
async function waitStatus(t, docId, states, { timeout = 90_000, who, what } = {}) {
  const want = Array.isArray(states) ? states : [states];
  return waitFor(async () => {
    const r = await status(t, docId, who);
    return r.status === 200 && want.includes(r.body.state) ? r.body : false;
  }, { timeout, interval: 400, what: what ?? `document ${docId} to reach ${want.join("|")}` });
}

// ---- document builders ------------------------------------------------------------------------
let wordCounter = 0;
const uniqueWord = (tag) => `qz${tag}${suffix}x${wordCounter++}`;
function paragraphs(n, marker, { chars = 700 } = {}) {
  const out = [];
  for (let i = 0; i < n; i++) {
    let p = `${marker} section ${i}. `;
    while (p.length < chars) p += `alpha${i} beta${(i * 7) % 13} gamma${(i * 11) % 17} delta${randomBytes(2).toString("hex")} `;
    out.push(p.trim());
  }
  return out;
}
const textDoc = (marker, n = 3) => Buffer.from(`GATE-CANARY ${marker}\n\n${paragraphs(n, marker).join("\n\n")}\n`);

function makePdf(lines) {
  const esc = (s) => s.replace(/[\\()]/g, "\\$&");
  const stream = `BT /F1 12 Tf 72 720 Td 16 TL\n${lines.map((l) => `(${esc(l)}) Tj T*`).join("\n")}\nET`;
  const objs = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R /Resources << /Font << /F1 4 0 R >> >> >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
  ];
  let out = "%PDF-1.4\n";
  const offsets = [];
  objs.forEach((o, i) => { offsets.push(Buffer.byteLength(out)); out += `${i + 1} 0 obj\n${o}\nendobj\n`; });
  const xref = Buffer.byteLength(out);
  out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n${offsets.map((o) => `${String(o).padStart(10, "0")} 00000 n \n`).join("")}`;
  out += `trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, "latin1");
}

// ---- database views ---------------------------------------------------------------------------
const jobOf = (docId) => {
  const row = sql(`SELECT id, state, attempts, coalesce(current_stage,''), coalesce(last_error_code,''), revision_id FROM ingestion_jobs WHERE document_id = '${docId}' ORDER BY created_at DESC LIMIT 1`);
  if (!row) return null;
  const [id, state, attempts, stage, error, revisionId] = row.split("|");
  return { id, state, attempts: Number(attempts), stage, error, revisionId };
};
const revisionsOf = (docId) => sql(`SELECT id || '|' || state || '|' || chunk_count || '|' || embedding_space_id FROM knowledge_revisions WHERE document_id = '${docId}' ORDER BY created_at`)
  .split("\n").filter(Boolean).map((r) => { const [id, state, chunks, space] = r.split("|"); return { id, state, chunks: Number(chunks), space }; });
const pointerOf = (docId) => sql(`SELECT revision_id FROM document_publications WHERE document_id = '${docId}'`) || null;
const eventsOf = (jobId) => sql(`SELECT event_type || ':' || coalesce(stage,'') FROM ingestion_job_events WHERE job_id = '${jobId}' ORDER BY seq`).split("\n").filter(Boolean);
const count = (q) => Number(sql(q));

/** Asserts the full "published" end state of one document against PostgreSQL, RushDB and the artifacts bucket. */
async function assertPublished(t, docId, { expectedRevisions = 1 } = {}) {
  const revs = revisionsOf(docId);
  const live = revs.filter((r) => r.state === "published");
  assert.equal(live.length, 1, `exactly one published revision (got ${JSON.stringify(revs)})`);
  const rev = live[0];
  assert.ok(rev.chunks > 0, "chunk count > 0");
  assert.equal(pointerOf(docId), rev.id, "pointer set to the published revision");
  assert.equal(revs.filter((r) => r.state !== "purged" && r.state !== "superseded").length, expectedRevisions);
  assert.equal(await rCount(t.orgId, { revision_id: rev.id, published: true }), rev.chunks, "RushDB: all chunks published");
  assert.equal(await rCount(t.orgId, { revision_id: rev.id, published: false }), 0, "RushDB: none unpublished");
  assert.equal(await rCount(t.orgId, { document_id: docId }), rev.chunks, "RushDB: no duplicate or stray chunks for the document");
  return rev;
}

// =============================================================================================
// 0. setup
// =============================================================================================
test("0. setup: stub up, users sign in, stack is healthy", async () => {
  await stubUp();
  sessions.alice = await login("alice");
  sessions.bob = await login("bob");
  sessions.mallory = await login("mallory");
  sessions.bobId = (await web("/api/v1/me", { session: sessions.bob })).body.id;
  sessions.malloryId = (await web("/api/v1/me", { session: sessions.mallory })).body.id;
  assert.ok(sessions.bobId && sessions.malloryId);
  for (const s of ["worker", "dispatcher"]) {
    assert.equal(docker(`inspect -f {{.State.Health.Status}} ${ctr(s)}`), "healthy", `${s} container healthy`);
  }
  assert.equal(sql("SELECT rolcanlogin::text || rolsuper::text || rolbypassrls::text FROM pg_roles WHERE rolname = 'ukos_worker'"), "truefalsefalse", "worker role exists (migration 0005/0006)");
});

// =============================================================================================
// 1. happy path: txt, md, html, pdf
// =============================================================================================
test("1. happy path: TXT, MD, HTML and PDF go queued -> processing -> ready, with all the evidence", async () => {
  const t = await newTenant("happy", { viewer: "bob" });
  const marker = uniqueWord("happy");
  t.marker = marker;
  const cases = [
    { key: "txt", filename: "notes.txt", contentType: "text/plain", bytes: textDoc(`${marker}txt`) },
    { key: "md", filename: "readme.md", contentType: "text/markdown", bytes: Buffer.from(`# Title ${marker}md\n\nGATE-CANARY intro paragraph.\n\n## Part one\n\n${paragraphs(2, `${marker}md`).join("\n\n")}\n\n\`\`\`js\nconsole.log("code ${marker}md")\n\`\`\`\n`) },
    { key: "html", filename: "page.html", contentType: "text/html", bytes: Buffer.from(`<html><head><title>x</title><script>alert('GATE-CANARY-script')</script></head><body><h1>Heading ${marker}html</h1><p>GATE-CANARY ${paragraphs(2, `${marker}html`).join("</p><p>")}</p></body></html>`) },
    { key: "pdf", filename: "paper.pdf", contentType: "application/pdf", bytes: makePdf([`GATE-CANARY ${marker}pdf`, ...paragraphs(1, `${marker}pdf`, { chars: 120 }), "second line of the pdf"]) },
  ];
  t.docs = {};
  const uploaded = [];
  for (const c of cases) uploaded.push({ c, up: await uploadFile(t, c) });

  // SSE for the first document: snapshot first, ids increase, reaches a terminal event, no internals.
  const sseDoc = uploaded[0].up.docId;
  const sse = await readSse(`${t.wsPath}/documents/${sseDoc}/ingestion/events`, sessions.alice, {
    timeout: 60_000, until: (fr) => fr.some((f) => f.event === "end"),
  });
  assert.equal(sse.status, 200);
  assert.equal(sse.frames.find((f) => f.event !== "message" || f.data)?.event, "snapshot", "the first event is the snapshot");
  const ids = sse.frames.filter((f) => f.event === "job").map((f) => Number(f.id));
  assert.ok(ids.every((v, i) => i === 0 || v > ids[i - 1]), "SSE ids strictly increase");
  assert.ok(sse.frames.some((f) => f.event === "end"), `SSE ended (frames: ${sse.frames.map((f) => f.event).join(",")})`);

  const seen = new Set();
  for (const { c, up } of uploaded) {
    // The ingestion summary on the finalize response is queued (or already past it).
    assert.ok(["queued", "processing", "ready"].includes(up.fin.body.document.ingestion?.state ?? up.fin.body.ingestion?.state), `finalize shows ingestion state: ${up.fin.text.slice(0, 300)}`);
    const body = await waitStatus(t, up.docId, "ready");
    seen.add(body.state);
    t.docs[c.key] = up.docId;
    assert.equal(body.can_retry, false);
    assert.ok(body.published_at, "published_at set");
    assert.equal(body.error_code, null);
    // Strict allowlist of the status payload: nothing internal leaks.
    const allowed = new Set(["state", "stage", "attempts_label", "error_code", "message", "updated_at", "can_retry", "document_id", "version_id", "job_id", "revision_id", "attempts", "max_attempts", "published_at", "events", "next_after"]);
    for (const k of Object.keys(body)) assert.ok(allowed.has(k), `unexpected status key ${k}`);
    // Timeline: queued first, a stage per processing step, finished last, seq strictly increasing.
    const types = body.events.map((e) => e.type);
    assert.equal(types[0], "queued");
    assert.ok(types.includes("started") && types.includes("stage_completed") && types.includes("published") && types.includes("succeeded"), `timeline ${types.join(",")}`);
    assert.ok(body.events.every((e, i) => i === 0 || e.id > body.events[i - 1].id), "event ids increase");
    assert.ok(types.indexOf("started") < types.indexOf("succeeded"));
    for (const e of body.events) assert.deepEqual(Object.keys(e).sort(), ["at", "attempt", "id", "label", "stage", "type"]);
    const stages = [...new Set(body.events.filter((e) => e.type === "stage_completed").map((e) => e.stage))];
    report.stages = stages;
    // Database, RushDB, artifacts, audit.
    const rev = await assertPublished(t, up.docId);
    const job = jobOf(up.docId);
    assert.equal(job.state, "succeeded");
    assert.equal(job.attempts, 1);
    assert.equal(count(`SELECT count(*) FROM ingestion_job_events WHERE job_id = '${job.id}' AND event_type = 'started'`), 1);
    const arts = s3ls(`org/${t.orgId}/rev/${rev.id}/`);
    for (const f of ["extracted.json", "chunks.json", "embeddings.f32"]) assert.ok(arts.includes(f), `artifact ${f} present for ${c.key}: ${arts}`);
    assert.equal(count(`SELECT count(*) FROM ingestion_checkpoints WHERE revision_id = '${rev.id}' AND state = 'completed'`) >= 4, true, "checkpoints completed");
    // The chunks carry the right content: a keyword search finds this document by its own word.
    const hits = await waitFor(async () => {
      const h = await rKeyword(t.orgId, [t.wsId], `${marker}${c.key}`);
      return h.length ? h : false;
    }, { timeout: 20_000, what: `keyword hit for ${c.key}` });
    assert.ok(hits.every((h) => h.properties.document_id === up.docId), `${c.key}: hits belong to the document`);
  }
  assert.equal(count(`SELECT count(*) FROM audit_events WHERE org_id = '${t.orgId}' AND event_type = 'document.version.indexed'`), 4, "one document.version.indexed audit event per document");
  // The list shows the state for every document.
  const list = await web(`${t.wsPath}/documents`, { session: sessions.alice });
  assert.ok(list.body.items.length === 4 && list.body.items.every((d) => d.ingestion.state === "ready"), "document list shows ready for all four");
  assert.ok(seen.has("ready"));
});

// =============================================================================================
// 2. search contract against RushDB: published-only, tenant isolation, vectors
// =============================================================================================
test("2. search contract: only published chunks are retrievable; another org's tenant sees none", async () => {
  const t = ws.happy;
  const orgB = await newTenant("searchb", { owner: "mallory" });
  const wordB = uniqueWord("orgb");
  const upB = await uploadFile(orgB, { filename: "b.txt", contentType: "text/plain", bytes: textDoc(wordB) });
  await waitStatus(orgB, upB.docId, "ready");

  const txt = t.docs.txt;
  const rev = revisionsOf(txt).find((r) => r.state === "published");
  const word = `${t.marker}txt`;
  const vec = await rVector(t.orgId, [t.wsId], rev.space, `${word} section 0`);
  assert.ok(vec.length > 0, "vector search finds the published chunks");
  assert.ok(vec.every((h) => h.properties.org_id === t.orgId && h.properties.published === true));
  assert.ok(vec.some((h) => h.properties.document_id === txt), "the document is among the vector hits");
  const kw = await rKeyword(t.orgId, [t.wsId], word);
  assert.ok(kw.length > 0 && kw.every((h) => h.properties.org_id === t.orgId));

  // Org B's tenant: neither keyword nor vector search ever returns org A's chunks (own doc is found).
  assert.equal((await rKeyword(orgB.orgId, [t.wsId], word)).length, 0, "org B tenant, org A workspace filter: nothing");
  assert.equal((await rKeyword(orgB.orgId, [orgB.wsId], word)).length, 0, "org B tenant, own workspace: nothing of org A");
  assert.ok((await rKeyword(orgB.orgId, [orgB.wsId], wordB)).length > 0, "org B finds its own document");
  const vecB = await rVector(orgB.orgId, [t.wsId, orgB.wsId], rev.space, `${word} section 0`);
  assert.ok(vecB.every((h) => h.properties.org_id === orgB.orgId), "vector search in org B never returns org A chunks");
  assert.equal(await rCount(orgB.orgId, { document_id: txt }), 0);
  // A workspace the caller is not allowed to see (other workspace id) returns nothing even in the right tenant.
  assert.equal((await rKeyword(t.orgId, [randomUUID()], word)).length, 0, "workspace predicate is honoured");

  // Unpublished chunks are invisible: flip the whole revision off, search, flip it back.
  const chunks = await rChunks(t.orgId, { revision_id: rev.id });
  const flip = async (flag) => { for (const c of chunks) await rdb(t.orgId, { kind: "mutate", mutation: { kind: "putRecord", mode: "merge", label: "Chunk", id: c.chunk_id, properties: { published: flag }, options: {} } }); };
  await flip(false);
  assert.equal((await rKeyword(t.orgId, [t.wsId], word)).length, 0, "unpublished chunks are not retrievable (keyword)");
  assert.equal((await rVector(t.orgId, [t.wsId], rev.space, `${word} section 0`)).filter((h) => h.properties.document_id === txt).length, 0, "...nor by vector");
  await flip(true);
  assert.ok((await rKeyword(t.orgId, [t.wsId], word)).length > 0, "published again: retrievable");
});

// =============================================================================================
// 3. new version while v1 is published
// =============================================================================================
test("3. new version: v1 stays published until v2 publishes; then exactly v2; never both at the settle point", async () => {
  const t = await newTenant("vers");
  const w1 = uniqueWord("v1"), w2 = uniqueWord("v2");
  const up1 = await uploadFile(t, { filename: "doc-v1.txt", contentType: "text/plain", bytes: textDoc(w1, 6) });
  await waitStatus(t, up1.docId, "ready");
  const rev1 = await assertPublished(t, up1.docId);

  await stub({ delay_ms: 4000 }); // hold v2 in the embed stage so v1 is observed while v2 is in flight
  const violations = [];
  let samples = 0, bothSeen = 0;
  let stop = false;
  const monitor = (async () => {
    const until = Date.now() + 300_000;
    while (!stop && Date.now() < until) {
      const revs = revisionsOf(up1.docId);
      const rev2 = revs.find((r) => r.id !== rev1.id);
      const v1pub = await rCount(t.orgId, { revision_id: rev1.id, published: true });
      const v2pub = rev2 ? await rCount(t.orgId, { revision_id: rev2.id, published: true }) : 0;
      const ptr = pointerOf(up1.docId); // read AFTER the RushDB samples: a flag is only flipped after the pointer commit
      samples++;
      if (v2pub > 0 && ptr !== rev2?.id) violations.push(`v2 chunks published (${v2pub}) while the pointer was ${ptr}`);
      if (v1pub === 0 && ptr === rev1.id) violations.push("v1 unpublished while the pointer still pointed at it");
      if (v1pub > 0 && v2pub > 0) bothSeen++;
    }
  })();
  const up2 = await uploadFile(t, { filename: "doc-v2.txt", contentType: "text/plain", bytes: textDoc(w2, 6), documentId: up1.docId });
  assert.equal(up2.fin.body.version.version_number, 2);
  // While v2 is held in flight, v1 is still the answer.
  await waitFor(async () => (await stubStats()).requests >= 1 && jobOf(up1.docId)?.state === "running" && jobOf(up1.docId).id !== undefined, { timeout: 30_000, what: "v2 job running" });
  assert.equal(pointerOf(up1.docId), rev1.id, "v1 is still the pointer while v2 is processing");
  assert.ok((await rKeyword(t.orgId, [t.wsId], w1)).length > 0, "v1 answers searches while v2 builds");
  assert.equal((await rKeyword(t.orgId, [t.wsId], w2)).length, 0, "v2 is not searchable before the barrier");
  await stub({ delay_ms: 0 });
  await waitStatus(t, up1.docId, "ready", { what: "v2 ready" });
  await waitFor(async () => (await rCount(t.orgId, { revision_id: rev1.id })) === 0, { timeout: 60_000, what: "v1 chunks purged from RushDB" });
  stop = true;
  await monitor;
  report.versionMonitor = { samples, bothFlaggedSamples: bothSeen, violations: violations.length };
  assert.deepEqual(violations, [], "publication barrier invariants held in every sample");

  const revs = revisionsOf(up1.docId);
  const rev2 = revs.find((r) => r.id !== rev1.id);
  assert.equal(rev2.state, "published");
  assert.ok(["superseded", "purged"].includes(revs.find((r) => r.id === rev1.id).state));
  assert.equal(pointerOf(up1.docId), rev2.id, "pointer shows v2");
  assert.equal(sql(`SELECT version_number FROM document_publications WHERE document_id = '${up1.docId}'`), "2");
  assert.equal(await rCount(t.orgId, { document_id: up1.docId, published: true }), rev2.chunks, "exactly v2's chunks are published");
  assert.equal(await rCount(t.orgId, { document_id: up1.docId }), rev2.chunks, "v1's chunks are gone");
  assert.equal((await rKeyword(t.orgId, [t.wsId], w1)).length, 0, "v1 text no longer retrievable");
  assert.ok((await rKeyword(t.orgId, [t.wsId], w2)).length > 0, "v2 text retrievable");
  await waitFor(() => revisionsOf(up1.docId).find((r) => r.id === rev1.id).state === "purged", { timeout: 60_000, what: "v1 revision marked purged" });
  await waitFor(() => s3ls(`org/${t.orgId}/rev/${rev1.id}/`) === "", { timeout: 30_000, interval: 2000, what: "v1 artifacts removed" });
  t.docId = up1.docId;
});

// =============================================================================================
// 4. idempotency
// =============================================================================================
test("4. idempotency: duplicate finalize, duplicate outbox deliveries, a manual re-queue -> one revision, no duplicate chunks", async () => {
  const t = await newTenant("idem");
  await stub({ delay_ms: 8000 });
  const up = await uploadFile(t, { filename: "idem.txt", contentType: "text/plain", bytes: textDoc(uniqueWord("idem"), 4) });
  // finalize again (same upload id): 200, same version, still one job
  const again = await web(`${t.wsPath}/uploads/${up.uploadId}/finalize`, { session: sessions.alice, method: "POST" });
  assert.equal(again.status, 200);
  assert.equal(again.body.version.id, up.versionId);
  assert.equal(count(`SELECT count(*) FROM ingestion_jobs WHERE version_id = '${up.versionId}'`), 1);
  assert.equal(count(`SELECT count(*) FROM outbox_events WHERE aggregate_id = (SELECT id FROM ingestion_jobs WHERE version_id = '${up.versionId}') AND event_type = 'ingestion.job.queued'`), 1);

  // While the job is RUNNING (held in embed), deliver duplicates of the queued event: they must claim nothing.
  await waitFor(() => jobOf(up.docId)?.state === "running", { timeout: 30_000, what: "job running" });
  const job = jobOf(up.docId);
  const dup = (n) => { for (let i = 0; i < n; i++) sql(`INSERT INTO outbox_events (id, org_id, aggregate_type, aggregate_id, event_type, payload) SELECT gen_random_uuid(), org_id, aggregate_type, aggregate_id, event_type, payload FROM outbox_events WHERE aggregate_id = '${job.id}' AND event_type = 'ingestion.job.queued' LIMIT 1`); };
  dup(3);
  sql(`UPDATE outbox_events SET dispatched_at = NULL WHERE aggregate_id = '${job.id}' AND event_type = 'ingestion.job.queued'`); // and re-send the original too
  await waitFor(() => count(`SELECT count(*) FROM outbox_events WHERE aggregate_id = '${job.id}' AND dispatched_at IS NULL`) === 0, { timeout: 20_000, what: "duplicates dispatched" });
  await sleep(3000);
  assert.equal(jobOf(up.docId).attempts, 1, "duplicates did not claim the running job");
  await stub({ delay_ms: 0 });
  await waitStatus(t, up.docId, "ready");
  const rev = await assertPublished(t, up.docId);
  assert.equal(jobOf(up.docId).attempts, 1);
  assert.equal(count(`SELECT count(*) FROM ingestion_job_events WHERE job_id = '${job.id}' AND event_type = 'started'`), 1);

  // Duplicate delivery AFTER success: no-op.
  dup(2);
  sql(`INSERT INTO outbox_events (id, org_id, aggregate_type, aggregate_id, event_type, payload) VALUES (gen_random_uuid(), '${t.orgId}', 'document', '${up.docId}', 'knowledge.reconcile', '{"document_id":"${up.docId}"}')`);
  await waitFor(() => count(`SELECT count(*) FROM outbox_events WHERE org_id = '${t.orgId}' AND dispatched_at IS NULL`) === 0, { timeout: 20_000, what: "post-success duplicates dispatched" });
  await sleep(3000);
  assert.equal(jobOf(up.docId).attempts, 1);
  assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE document_id = '${up.docId}'`), 1);
  await assertPublished(t, up.docId);

  // Manual re-queue of the finished job (operator mistake / replay): re-runs, skips finished stages, same revision, no duplicates.
  const cpBefore = sql(`SELECT stage || '=' || completed_at FROM ingestion_checkpoints WHERE stage IN ('extract','chunk','embed','index') AND revision_id = '${rev.id}' ORDER BY stage`);
  sql(`UPDATE ingestion_jobs SET state = 'queued', run_after = now(), finished_at = NULL WHERE id = '${job.id}'`);
  sql(`INSERT INTO outbox_events (id, org_id, aggregate_type, aggregate_id, event_type, payload) VALUES (gen_random_uuid(), '${t.orgId}', 'ingestion_job', '${job.id}', 'ingestion.job.queued', '{"job_id":"${job.id}"}')`);
  await waitFor(() => { const j = jobOf(up.docId); return j.state === "succeeded" && j.attempts === 2; }, { timeout: 60_000, what: "re-queued job finishes (attempt 2)" });
  const after = await assertPublished(t, up.docId);
  assert.equal(after.id, rev.id, "same deterministic revision");
  assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE document_id = '${up.docId}'`), 1);
  assert.equal(sql(`SELECT stage || '=' || completed_at FROM ingestion_checkpoints WHERE stage IN ('extract','chunk','embed','index') AND revision_id = '${rev.id}' ORDER BY stage`), cpBefore, "extract, chunk, embed and index were skipped on the re-run (checkpoint rows untouched; validate and publish always re-run)");
  assert.equal(count(`SELECT count(*) FROM document_publications WHERE document_id = '${up.docId}'`), 1);
});

// =============================================================================================
// 5. unsupported, no text, corrupt, dead-letter + manual retry
// =============================================================================================
test("5a. unsupported types, scanned/empty PDF and corrupt PDF end in safe terminal states", async () => {
  const t = await newTenant("terminal", { viewer: "bob" });
  const png = await uploadFile(t, { filename: "pic.png", contentType: "image/png", bytes: Buffer.from("\x89PNG\r\n\x1a\n" + "x".repeat(200), "latin1") });
  const xlsx = await uploadFile(t, { filename: "sheet.xlsx", contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", bytes: Buffer.from("PK\x03\x04" + "y".repeat(200), "latin1") });
  const empty = await uploadFile(t, { filename: "scan.pdf", contentType: "application/pdf", bytes: makePdf([]) });
  const corrupt = await uploadFile(t, { filename: "broken.pdf", contentType: "application/pdf", bytes: Buffer.from("%PDF-1.4\nthis is not a pdf GATE-CANARY \x00\x01\x02 garbage garbage") });

  for (const up of [png, xlsx]) {
    const s = await waitStatus(t, up.docId, "unsupported");
    assert.equal(s.error_code, "unsupported_type");
    assert.equal(s.message, "This file type cannot be processed yet.");
    assert.equal(s.attempts, 1, "terminal immediately, no retries");
    assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE document_id = '${up.docId}' AND state = 'published'`), 0);
    assert.equal(await rCount(t.orgId, { document_id: up.docId }), 0);
    report.unsupportedCanRetry = s.can_retry; // by design (M06 adds processors): reported, see handoff
  }
  const e = await waitStatus(t, empty.docId, "no_text");
  assert.equal(e.error_code, "no_text_extracted");
  assert.equal(e.can_retry, false, "no text: a retry button would be pointless");
  assert.equal(e.message, "No readable text was found in this document.");
  assert.equal(await rCount(t.orgId, { document_id: empty.docId }), 0);

  const c = await waitStatus(t, corrupt.docId, "failed");
  assert.equal(c.error_code, "pdf_unreadable");
  assert.match(c.message, /could not be read/);
  assert.equal(c.attempts, 1, "a permanent error is not retried");
  assert.equal(c.can_retry, true);
  assert.equal(await rCount(t.orgId, { document_id: corrupt.docId }), 0);
  assert.ok(!c.message.includes("GATE-CANARY"));
  // stored error detail is bounded and redacted
  assert.ok(count(`SELECT length(coalesce(last_error_detail,'')) FROM ingestion_jobs WHERE document_id = '${corrupt.docId}'`) <= 500);
  assert.ok(!sql(`SELECT coalesce(last_error_detail,'') FROM ingestion_jobs WHERE document_id = '${corrupt.docId}'`).includes("GATE-CANARY"), "no document text in the stored error");
  t.corruptDoc = corrupt.docId;
});

test("5b. dead-letter: attempts exhaust -> failed + can_retry; viewer cannot retry; retry after the fix reaches ready", async () => {
  const t = await newTenant("dead", { viewer: "bob" });
  await stub({ fail_next: 100000, fail_status: 503 });
  const started = Date.now();
  const up = await uploadFile(t, { filename: "dead.txt", contentType: "text/plain", bytes: textDoc(uniqueWord("dead"), 2) });
  const failed = await waitStatus(t, up.docId, "failed", { timeout: 180_000, what: "dead-lettered (failed)" });
  report.deadLetterSeconds = Math.round((Date.now() - started) / 1000);
  assert.equal(failed.attempts, MAX_ATTEMPTS, "all attempts used");
  assert.equal(failed.can_retry, true);
  assert.ok(failed.error_code && failed.message, "safe error code and message");
  assert.equal(jobOf(up.docId).state, "dead");
  const types = failed.events.map((e) => e.type);
  assert.equal(types.filter((x) => x === "started").length, MAX_ATTEMPTS);
  assert.ok(types.includes("retry_scheduled") && types.at(-1) === "dead", `timeline ${types.join(",")}`);
  assert.equal(await rCount(t.orgId, { document_id: up.docId }), 0, "nothing was indexed");
  assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE document_id = '${up.docId}' AND state = 'published'`), 0);
  report.deadLetterErrorCode = failed.error_code;

  // The viewer sees the failure but cannot retry; the owner can.
  const asBob = await status(t, up.docId, "bob");
  assert.equal(asBob.status, 200);
  assert.equal(asBob.body.can_retry, false);
  assert.equal((await web(`${t.wsPath}/documents/${up.docId}/ingestion/retry`, { session: sessions.bob, method: "POST" })).status, 403);

  // Fix the "outage", then two simultaneous clicks: exactly one wins.
  await stubReset();
  const [r1, r2] = await Promise.all([1, 2].map(() => web(`${t.wsPath}/documents/${up.docId}/ingestion/retry`, { session: sessions.alice, method: "POST" })));
  assert.deepEqual([r1.status, r2.status].sort(), [202, 409], "single winner under concurrent retry");
  const win = r1.status === 202 ? r1 : r2;
  assert.equal(win.body.job_id, failed.job_id, "the existing job is re-queued, not duplicated");
  assert.ok(["queued", "processing"].includes(win.body.ingestion.state));
  const ready = await waitStatus(t, up.docId, "ready", { timeout: 90_000 });
  assert.equal(ready.can_retry, false);
  assert.equal(count(`SELECT count(*) FROM ingestion_jobs WHERE document_id = '${up.docId}'`), 1);
  const rev = await assertPublished(t, up.docId);
  assert.equal(count(`SELECT count(*) FROM ingestion_job_events WHERE job_id = '${failed.job_id}' AND detail::text LIKE '%manual_retry%'`), 1, "audit trail of the manual retry in the job events");
  assert.equal(count(`SELECT count(*) FROM audit_events WHERE org_id = '${t.orgId}' AND event_type = 'document.ingestion.retried'`), 1);
  // Retrying a ready document is a conflict.
  assert.equal((await web(`${t.wsPath}/documents/${up.docId}/ingestion/retry`, { session: sessions.alice, method: "POST" })).status, 409);
  assert.ok(rev);
});

// =============================================================================================
// 6. crash injection with real containers
// =============================================================================================
test("6a. worker killed mid-job (embed held): lease expires, reaper requeues, restart resumes without duplicates", async () => {
  const t = await newTenant("crashw");
  await stubReset();
  await stub({ delay_ms: 15000 });
  const before = (await stubStats()).requests;
  const up = await uploadFile(t, { filename: "crash.txt", contentType: "text/plain", bytes: textDoc(uniqueWord("crashw"), 12) });
  await waitFor(async () => (await stubStats()).requests > before, { timeout: 30_000, what: "worker reached the embed stage (stub got the request)" });
  const job = jobOf(up.docId);
  assert.equal(job.state, "running");
  const cp = () => sql(`SELECT stage || '|' || state || '|' || attempts || '|' || coalesce(completed_at::text,'') FROM ingestion_checkpoints WHERE revision_id = '${job.revisionId}' ORDER BY stage`);
  const cpBefore = cp();
  const doneStages = cpBefore.split("\n").filter((l) => l.split("|")[1] === "completed").map((l) => l.split("|")[0]);
  assert.ok(doneStages.length >= 2, `earlier stages already checkpointed before the crash: ${cpBefore}`);
  assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE id = '${job.revisionId}' AND state = 'published'`), 0);
  const killedAt = Date.now();
  docker(`kill ${ctr("worker")}`);
  await stub({ delay_ms: 0 });
  // Lease expires, the reaper (dispatcher) puts the job back to queued.
  await waitFor(() => jobOf(up.docId).state === "queued", { timeout: LEASE_S * 1000 + 40_000, interval: 1000, what: "reaper requeued the job" });
  report.reaperSeconds = Math.round((Date.now() - killedAt) / 1000);
  assert.ok(Date.now() - killedAt >= (LEASE_S - 8) * 1000, "not reaped before the lease could expire");
  const mid = (await status(t, up.docId)).body;
  assert.equal(mid.state, "queued", "status endpoint shows queued, not processing, after the reap");
  assert.equal(await rCount(t.orgId, { document_id: up.docId, published: true }), 0, "nothing visible while the worker is down");
  docker(`start ${ctr("worker")}`);
  await waitStatus(t, up.docId, "ready", { timeout: 120_000 });
  const rev = await assertPublished(t, up.docId);
  const after = jobOf(up.docId);
  assert.equal(after.attempts, 2, "crash consumed exactly one extra attempt");
  assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE document_id = '${up.docId}'`), 1);
  const cpAfter = cp().split("\n");
  for (const line of cpBefore.split("\n").filter((l) => l.split("|")[1] === "completed")) {
    assert.ok(cpAfter.includes(line), `stage checkpoint untouched by the resume (skipped): ${line}`);
  }
  const events = eventsOf(job.id);
  assert.equal(events.filter((e) => e.startsWith("started")).length, 2);
  assert.ok(events.some((e) => e.startsWith("retry_scheduled") || e.startsWith("queued")), events.join(","));
  report.crashWorkerStagesSkipped = doneStages;
  await waitFor(() => docker(`inspect -f {{.State.Health.Status}} ${ctr("worker")}`) === "healthy", { timeout: 90_000, what: "worker healthy again" });
  assert.ok(rev.chunks > 1);
});

test("6b. dispatcher killed after the outbox row exists but before dispatch: restart dispatches, job runs once", async () => {
  const t = await newTenant("crashd");
  await stubReset();
  docker(`kill ${ctr("dispatcher")}`);
  const up = await uploadFile(t, { filename: "d.txt", contentType: "text/plain", bytes: textDoc(uniqueWord("crashd"), 3) });
  await sleep(6000);
  assert.equal(jobOf(up.docId).state, "queued", "nothing runs while the dispatcher is down");
  assert.equal(count(`SELECT count(*) FROM outbox_events WHERE aggregate_id = '${jobOf(up.docId).id}' AND dispatched_at IS NULL`), 1, "the outbox row waits");
  assert.equal((await status(t, up.docId)).body.state, "queued");
  docker(`start ${ctr("dispatcher")}`);
  await waitStatus(t, up.docId, "ready", { timeout: 90_000 });
  await assertPublished(t, up.docId);
  const job = jobOf(up.docId);
  assert.equal(job.attempts, 1);
  assert.equal(count(`SELECT count(*) FROM ingestion_job_events WHERE job_id = '${job.id}' AND event_type = 'started'`), 1);
  assert.equal(count(`SELECT count(*) FROM outbox_events WHERE aggregate_id = '${job.id}' AND dispatched_at IS NOT NULL AND event_type = 'ingestion.job.queued'`), 1);
  await waitFor(() => docker(`inspect -f {{.State.Health.Status}} ${ctr("dispatcher")}`) === "healthy", { timeout: 60_000, what: "dispatcher healthy again" });
});

test("6c. RushDB stopped mid-index: retryable, never partially visible, completes after it returns, no duplicate chunks", async () => {
  const t = await newTenant("crashr");
  await stubReset();
  const big = Buffer.from(`GATE-CANARY ${uniqueWord("crashr")}\n\n${paragraphs(420, "bulk", { chars: 900 }).join("\n\n")}\n`);
  const up = await uploadFile(t, { filename: "big.txt", contentType: "text/plain", bytes: big });
  const rev = (await waitFor(() => { const j = jobOf(up.docId); return j?.revisionId ? j : false; }, { timeout: 20_000 })).revisionId;
  const violations = [];
  let stop = false, maxUnpublished = 0;
  const monitor = (async () => {
    const until = Date.now() + 400_000;
    while (!stop && Date.now() < until) {
      let pub = 0, unpub = 0;
      try { pub = await rCount(t.orgId, { revision_id: rev, published: true }); unpub = await rCount(t.orgId, { revision_id: rev, published: false }); } catch { /* RushDB down */ continue; }
      const ptr = pointerOf(up.docId);
      maxUnpublished = Math.max(maxUnpublished, unpub);
      if (pub > 0 && ptr !== rev) violations.push(`published ${pub} while pointer ${ptr}`);
    }
  })();
  // Wait until the index stage is under way (some chunks written, not all), then pull the plug.
  const total = await waitFor(() => { const r = revisionsOf(up.docId)[0]; return r ? r : false; }, { timeout: 30_000 });
  await waitFor(async () => (await rCount(t.orgId, { revision_id: rev })) > 0, { timeout: 90_000, interval: 100, what: "first chunks written to RushDB" });
  const written = await rCount(t.orgId, { revision_id: rev }).catch(() => -1);
  docker(`stop -t 3 ${ctr("rushdb")}`);
  report.rushdbStoppedAfterChunks = written;
  const failedOnce = await waitFor(() => { const j = jobOf(up.docId); return (j.attempts >= 1 && (j.state === "queued" || j.state === "dead") && j.error) ? j : false; }, { timeout: 60_000, what: "job noticed RushDB is down" });
  assert.equal(failedOnce.state, "queued", `an outage is retried, not dead-lettered (error ${failedOnce.error})`);
  assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE id = '${rev}' AND state = 'published'`), 0);
  assert.equal(pointerOf(up.docId), null);
  docker(`start ${ctr("rushdb")}`);
  await waitFor(() => docker(`inspect -f {{.State.Health.Status}} ${ctr("rushdb")}`) === "healthy", { timeout: 90_000, what: "RushDB healthy" });
  const ready = await waitStatus(t, up.docId, "ready", { timeout: 180_000, what: "job completes after RushDB returns" });
  stop = true; await monitor;
  assert.deepEqual(violations, [], "a partial index was never visible");
  const r = await assertPublished(t, up.docId);
  assert.ok(r.chunks >= 100, `many chunks (${r.chunks})`);
  assert.ok(total);
  report.rushdbOutage = { chunksWrittenAtStop: written, totalChunks: r.chunks, attempts: jobOf(up.docId).attempts, maxUnpublishedSeen: maxUnpublished };
  assert.ok(ready.attempts >= 2);
  assert.equal(await rCount(t.orgId, { revision_id: rev }), r.chunks, "re-written chunks merged, none duplicated");
});

test("6d. embedding server down (connection refused): retries, then recovers when it is back", async () => {
  const t = await newTenant("crashe");
  await stubReset();
  await stubDown();
  const up = await uploadFile(t, { filename: "e.txt", contentType: "text/plain", bytes: textDoc(uniqueWord("crashe"), 3) });
  const j = await waitFor(() => { const x = jobOf(up.docId); return x.attempts >= 1 && x.state === "queued" && x.error ? x : false; }, { timeout: 90_000, what: "first failed attempt scheduled for retry" });
  report.embedderDownErrorCode = j.error;
  assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE document_id = '${up.docId}' AND state = 'published'`), 0);
  const mid = (await status(t, up.docId)).body;
  assert.equal(mid.state, "queued");
  assert.match(mid.attempts_label ?? "", /Retrying: attempt \d of \d/);
  await stubUp();
  await waitStatus(t, up.docId, "ready", { timeout: 120_000 });
  await assertPublished(t, up.docId);
  assert.ok(jobOf(up.docId).attempts >= 2);
});

test("6e. reconcile sweep repairs induced drift (a published chunk flipped to unpublished behind PostgreSQL's back)", async () => {
  const t = await newTenant("recon");
  await stubReset();
  const up = await uploadFile(t, { filename: "r.txt", contentType: "text/plain", bytes: textDoc(uniqueWord("recon"), 12) });
  await waitStatus(t, up.docId, "ready");
  const rev = await assertPublished(t, up.docId);
  const chunks = await rChunks(t.orgId, { revision_id: rev.id });
  const flip = (id, flag) => rdb(t.orgId, { kind: "mutate", mutation: { kind: "putRecord", mode: "merge", label: "Chunk", id, properties: { published: flag }, options: {} } });
  await flip(chunks[0].chunk_id, false);
  await flip(chunks.at(-1).chunk_id, false);
  assert.equal(await rCount(t.orgId, { revision_id: rev.id, published: true }), rev.chunks - 2, "drift induced");
  const started = Date.now();
  await waitFor(async () => (await rCount(t.orgId, { revision_id: rev.id, published: true })) === rev.chunks, { timeout: 120_000, interval: 1000, what: "reconcile sweep repaired the drift" });
  report.reconcileRepairSeconds = Math.round((Date.now() - started) / 1000);
  assert.equal(await rCount(t.orgId, { document_id: up.docId }), rev.chunks);
  assert.equal(pointerOf(up.docId), rev.id);
});

// =============================================================================================
// 7. deletion
// =============================================================================================
test("7. deletion: denied at once; revisions purged, jobs cancelled, chunks and artifacts removed; index stats reported", async () => {
  const t = await newTenant("del", { viewer: "bob" });
  await stubReset();
  const word = uniqueWord("del");
  const a = await uploadFile(t, { filename: "a.txt", contentType: "text/plain", bytes: textDoc(word, 12) });
  await waitStatus(t, a.docId, "ready");
  const revA = await assertPublished(t, a.docId);
  assert.equal((await rKeyword(t.orgId, [t.wsId], word)).length > 0, true);
  const statsBefore = await vectorStats(t.orgId, revA.space);
  assert.ok(s3ls(`org/${t.orgId}/rev/${revA.id}/`).length > 0, "artifacts exist before the delete");

  // Document B is deleted while its job is still running.
  await stub({ delay_ms: 9000 });
  const reqs = (await stubStats()).requests;
  const b = await uploadFile(t, { filename: "b.txt", contentType: "text/plain", bytes: textDoc(uniqueWord("delb"), 4) });
  await waitFor(async () => (await stubStats()).requests > reqs, { timeout: 30_000, what: "B held in embed" });

  for (const d of [a, b]) {
    const del = await web(`${t.wsPath}/documents/${d.docId}`, { session: sessions.alice, method: "DELETE" });
    assert.equal(del.status, 202, del.text);
    // immediate denial for everybody, including the status endpoints
    for (const who of ["alice", "bob"]) {
      assert.equal((await web(`${t.wsPath}/documents/${d.docId}`, { session: sessions[who] })).status, 404);
      assert.equal((await status(t, d.docId, who)).status, 404, `${who}: status of a deleted document is 404`);
    }
    assert.equal((await web(`${t.wsPath}/documents/${d.docId}/ingestion/retry`, { session: sessions.alice, method: "POST" })).status, 404);
  }
  await stub({ delay_ms: 0 });

  // A: revisions purged, chunks gone, artifacts removed
  await waitFor(() => revisionsOf(a.docId).every((r) => r.state === "purged"), { timeout: 60_000, what: "A revisions purged" });
  await waitFor(async () => (await rCount(t.orgId, { document_id: a.docId })) === 0, { timeout: 60_000, what: "A chunks purged from RushDB" });
  await waitFor(() => s3ls(`org/${t.orgId}/rev/${revA.id}/`) === "", { timeout: 60_000, interval: 2000, what: "A artifacts removed" });
  assert.equal((await rKeyword(t.orgId, [t.wsId], word)).length, 0, "searches never return the deleted document's chunks");
  assert.equal((await rVector(t.orgId, [t.wsId], revA.space, word)).filter((h) => h.properties.document_id === a.docId).length, 0);
  const statsAfter = await vectorStats(t.orgId, revA.space);
  report.vectorIndexStats = { before: statsBefore, after: statsAfter, chunksDeleted: revA.chunks };

  // B: job cancelled, nothing published, nothing left in the index once the in-flight work settled
  await waitFor(() => jobOf(b.docId).state === "cancelled", { timeout: 30_000, what: "B job cancelled" });
  await sleep(12_000);
  assert.equal(jobOf(b.docId).state, "cancelled");
  assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE document_id = '${b.docId}' AND state = 'published'`), 0);
  assert.equal(await rCount(t.orgId, { document_id: b.docId }), 0, "a deleted document is never (re)indexed");
  assert.equal(pointerOf(b.docId), null);
  assert.equal(count(`SELECT count(*) FROM ingestion_job_events WHERE job_id = '${jobOf(b.docId).id}' AND event_type = 'cancelled'`), 1);
});

// =============================================================================================
// 8. security
// =============================================================================================
test("8a. outsiders get a concealed 404 on status, retry and SSE; the viewer can read but not retry", async () => {
  const t = ws.terminal; // has a failed (corrupt PDF) document and a viewer (bob)
  assert.ok(t?.corruptDoc, "scenario 5a ran");
  const doc = t.corruptDoc;
  const orgB = ws.searchb;
  assert.ok(orgB, "mallory has an org of her own (scenario 2)");
  for (const [method, path] of [["GET", `/documents/${doc}/ingestion`], ["POST", `/documents/${doc}/ingestion/retry`]]) {
    assert.equal((await web(`${t.wsPath}${path}`, { session: sessions.mallory, method })).status, 404, `outsider ${method} ${path}`);
  }
  for (const path of [`/documents/${doc}/ingestion/events`, `/ingestion/events`]) {
    const r = await readSse(`${t.wsPath}${path}`, sessions.mallory, { timeout: 10_000 });
    assert.equal(r.status, 404, `outsider SSE ${path}`);
  }
  // The viewer (workspace role holder): reads status and the workspace feed; cannot retry.
  const bob = await status(t, doc, "bob");
  assert.equal(bob.status, 200);
  assert.equal(bob.body.can_retry, false);
  assert.equal(bob.body.state, "failed");
  assert.equal((await web(`${t.wsPath}/documents/${doc}/ingestion/retry`, { session: sessions.bob, method: "POST" })).status, 403);
  const feed = await readSse(`${t.wsPath}/ingestion/events`, sessions.bob, { timeout: 15_000, until: (fr) => fr.some((f) => f.event === "snapshot") });
  assert.equal(feed.status, 200);
  assert.ok(feed.frames.some((f) => f.event === "snapshot"));
  // Resume with Last-Event-ID does not leak either: mallory's attempt against org A stays 404.
  const resume = await readSse(`${t.wsPath}/documents/${doc}/ingestion/events`, sessions.mallory, { timeout: 10_000, headers: { "last-event-id": "1" } });
  assert.equal(resume.status, 404);
  // Mallory's own org: she cannot reach org A's ids through her own workspace path either.
  assert.equal((await web(`${orgB.wsPath}/documents/${doc}/ingestion`, { session: sessions.mallory })).status, 404);
  // An unauthenticated caller is not served.
  const anon = await web(`${t.wsPath}/documents/${doc}/ingestion`);
  assert.ok([401, 403, 404, 302, 307].includes(anon.status), `anonymous: ${anon.status}`);
});

test("8b. containers: no host ports for worker/dispatcher; no RushDB token in web; worker role is RLS-bound", async () => {
  for (const s of ["worker", "dispatcher"]) assert.equal(docker(`port ${ctr(s)}`), "", `${s} publishes no host ports`);
  for (const s of ["worker", "dispatcher"]) {
    const ports = JSON.parse(docker(`inspect -f "{{json .NetworkSettings.Ports}}" ${ctr(s)}`));
    assert.ok(Object.values(ports).every((v) => !v || v.length === 0), `${s}: no port bound to the host (${JSON.stringify(ports)})`);
  }
  for (const s of ["web"]) {
    const env = docker(`exec ${ctr(s)} env`);
    assert.ok(!/RUSHDB|DATABASE_WORKER|POSTGRES_WORKER|CELERY|S3_SECRET/.test(env), `${s} container has no RushDB/worker/broker/storage secrets`);
  }
  // M04: search runs inside the API, so the API now holds the RushDB token too (web never does).

  const t = ws.happy;
  const W = "change-me-postgres-worker";
  const noCtx = sqlAs("ukos_worker", W, "SELECT (SELECT count(*) FROM ingestion_jobs) || ',' || (SELECT count(*) FROM documents) || ',' || (SELECT count(*) FROM knowledge_revisions) || ',' || (SELECT count(*) FROM document_publications)");
  assert.equal(noCtx, "0,0,0,0", "no org context: the worker role sees no rows (fail closed)");
  const own = sqlAs("ukos_worker", W, `BEGIN; SELECT set_config('app.current_org_id', '${t.orgId}', true); SELECT count(*) > 0 FROM ingestion_jobs; SELECT count(*) FROM ingestion_jobs WHERE org_id <> '${t.orgId}'; COMMIT;`);
  assert.match(own, /\bt\b[\s\S]*\b0\b/, `with org context: own rows only (${own})`);
  const other = ws.searchb;
  const cross = sqlAs("ukos_worker", W, `BEGIN; SELECT set_config('app.current_org_id', '${other.orgId}', true); SELECT count(*) FROM ingestion_jobs WHERE org_id = '${t.orgId}'; SELECT count(*) FROM documents WHERE org_id = '${t.orgId}'; COMMIT;`);
  assert.ok(cross.split("\n").filter((l) => /^\d+$/.test(l)).every((l) => l === "0"), `org B context cannot see org A (${cross})`);
  const denied = sqlAs("ukos_worker", W, "DELETE FROM ingestion_jobs; UPDATE documents SET title = 'x'; DELETE FROM document_publications; SELECT 1 FROM organizations LIMIT 1;");
  assert.ok((denied.match(/permission denied/g) ?? []).length >= 2, `worker has no DELETE/UPDATE where not granted: ${denied}`);
  assert.equal(sqlAs("ukos_worker", W, "SELECT count(*) FROM ingest_claim_job(gen_random_uuid(), 'x', 5)"), "0", "claim function: unknown job claims nothing");
  assert.match(sqlAs("ukos_worker", W, "SELECT count(*) FROM audit_events"), /^(0|ERROR.*)$/s);
  // The app role cannot call the worker-only definer functions, and the worker role cannot call the app-only requeue.
  assert.match(sqlAs("ukos_app", "change-me-postgres-app", "SELECT * FROM ingest_claim_job(gen_random_uuid(), 'x', 5)"), /permission denied/);
  assert.match(sqlAs("ukos_worker", W, "SELECT ingest_requeue_job(gen_random_uuid())"), /permission denied/);
});

// =============================================================================================
// 9. throughput smoke
// =============================================================================================
test("9. throughput: 20 small documents at once all reach ready within a bounded time", async () => {
  const t = await newTenant("load");
  await stubReset();
  const N = 20;
  const t0 = Date.now();
  const ups = await Promise.all(Array.from({ length: N }, (_, i) => uploadFile(t, { filename: `n${i}.txt`, contentType: "text/plain", bytes: textDoc(uniqueWord(`load${i}`), 2) })));
  const uploadDone = Date.now();
  const latencies = new Array(N).fill(null);
  await waitFor(async () => {
    await Promise.all(ups.map(async (u, i) => {
      if (latencies[i] !== null) return;
      const r = await status(t, u.docId);
      if (r.body?.state === "ready") latencies[i] = Date.now() - u.startedAt;
      else assert.ok(["queued", "processing"].includes(r.body?.state), `unexpected state ${r.body?.state}`);
    }));
    return latencies.every((x) => x !== null);
  }, { timeout: 180_000, interval: 300, what: "all 20 documents ready" });
  const total = Date.now() - t0;
  const sorted = [...latencies].sort((a, b) => a - b);
  const pct = (p) => sorted[Math.min(N - 1, Math.ceil((p / 100) * N) - 1)];
  report.throughput = { docs: N, workerConcurrency: CONCURRENCY, uploadPhaseMs: uploadDone - t0, totalMs: total, p50Ms: pct(50), p95Ms: pct(95), maxMs: sorted.at(-1), docsPerSecond: Number((N / (total / 1000)).toFixed(2)) };
  assert.equal(count(`SELECT count(*) FROM knowledge_revisions WHERE org_id = '${t.orgId}' AND state = 'published'`), N);
  assert.equal(count(`SELECT count(*) FROM ingestion_jobs WHERE org_id = '${t.orgId}' AND state = 'succeeded' AND attempts = 1`), N);
  assert.equal(await rCount(t.orgId, { published: true }), count(`SELECT sum(chunk_count) FROM knowledge_revisions WHERE org_id = '${t.orgId}'`));
  assert.ok(total < 150_000, `bounded time (${total} ms)`);
});

// =============================================================================================
// 99. cross-cutting
// =============================================================================================
test("99. no raw internals in any API/SSE response, no token ever reached the browser; report", () => {
  const INTERNALS = /lease_owner|lease_epoch|run_after|heartbeat|artifact_key|artifact_sha|ukos-artifacts|embedding_space|pipeline_version|Traceback|psycopg|sqlalchemy|rushdb|celery|redis|change-me|\borg\/[0-9a-f-]{36}\/rev\//i;
  let checked = 0;
  for (const t of transcript) {
    assert.ok(!JWT_RE.test(JSON.stringify(t.headers) + t.body), `token-shaped value in response to ${t.path}`);
    const isApiDoc = /\/documents|\/ingestion/.test(t.path) && !/content-url/.test(t.path);
    if (isApiDoc) { checked++; assert.ok(!INTERNALS.test(t.body), `internal detail in response to ${t.method} ${t.path}: ${(t.body.match(INTERNALS) ?? [])[0]}`); }
    assert.ok(!/GATE-CANARY/.test(t.body), `document text echoed by ${t.path}`);
  }
  assert.ok(checked > 100, `scanned ${checked} document/ingestion responses`);
  console.log(`GATE-REPORT ${JSON.stringify({ ...report, scannedResponses: checked })}`);
});
