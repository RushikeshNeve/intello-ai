// In-memory stub of the UKOS API for the M04 web e2e (search, chat with SSE, evidence). It implements the wire shapes
// the web agent was given (search POST, evidence chunk GET, chat threads/runs/cancel + scripted run-event SSE) plus the
// M01/M02 shapes the pages need (orgs, workspaces, documents, versions, content-url) and plays the object store.
// NOT the real API. Identity = the Dex JWT's email (signature verified against Dex's JWKS).
//
// Seed (Dex test users):
//   acme   alice=owner, bob=member   notes (alice admin, bob viewer): "Quarterly report" (pdf, 2 versions), "Meeting notes" (md)
//                                    papers (alice admin only): "Embedding survey" (txt)
//   globex bob=owner, alice=member   bench (alice viewer): "Bench readout"   vault (bob only): "Vault Secret Doc"
//   initech mallory=owner
//
// Test controls (no auth):
//   GET /__control/search?mode=ok|rate|down|degraded     search behaviour
//   GET /__control/evidence?chunk=ID&mode=ok|gone|down   evidence chunk behaviour
//   GET /__chat/step?run=ID&n=K   emit the next K scripted events of a run (persisted in its log, pushed to subscribers)
//   GET /__chat/drain?run=ID      emit all remaining events      GET /__chat/drop?run=ID   cut live SSE connections (network drop)
//   GET /__chat/end?run=ID&reason=timeout   send `end` and close live connections      GET /__chat/mode?mode=ok|rate
//   GET /__chat/state   GET /__log   GET /__seed
// Run scripts are chosen by the question text: default (grounded), PARTIAL, ABSTAIN, FAIL.
import { createServer } from "node:http";
import { createPublicKey, createVerify, randomUUID } from "node:crypto";

const port = Number(process.env.STUB_PORT ?? 39441);
const issuer = process.env.OIDC_ISSUER_URL ?? "http://127.0.0.1:39442/dex";
const origin = `http://127.0.0.1:${port}`;
const log = [];
const now = () => new Date().toISOString();

const orgs = new Map();
const workspaces = new Map();
const documents = new Map(); // id -> {id, workspace_id, title, deleted, versions: [{id, version_number, filename, type, bytes, chunks: [{id, text, page_start, page_end, heading_path}]}]}
const blobs = new Map();

function seedOrg(slug, name, members, wss) {
  const org = { id: randomUUID(), slug, name, created_at: now(), members: new Map(Object.entries(members)) };
  orgs.set(org.id, org);
  const out = {};
  for (const [wslug, wname, wmembers] of wss) {
    const w = { id: randomUUID(), org_id: org.id, slug: wslug, name: wname, created_at: now(), members: new Map(Object.entries(wmembers)) };
    workspaces.set(w.id, w);
    out[wslug] = w;
  }
  return { org, ws: out };
}
const acme = seedOrg("acme", "Acme Research", { "alice@ukos.test": "owner", "bob@ukos.test": "member" }, [
  ["notes", "Acme Notes", { "alice@ukos.test": "admin", "bob@ukos.test": "viewer" }],
  ["papers", "Acme Papers", { "alice@ukos.test": "admin" }],
]);
const globex = seedOrg("globex", "Globex Lab", { "bob@ukos.test": "owner", "alice@ukos.test": "member" }, [
  ["bench", "Globex Bench", { "bob@ukos.test": "admin", "alice@ukos.test": "viewer" }],
  ["vault", "Globex Vault", { "bob@ukos.test": "admin" }],
]);
seedOrg("initech", "Initech Holdings", { "mallory@ukos.test": "owner" }, [["plans", "Initech Secret Plans", { "mallory@ukos.test": "admin" }]]);

const PDF = Buffer.from("%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n");
const sha = (n) => String(n).padStart(2, "0").repeat(32).slice(0, 64);
let tick = 0;

function addDoc(wsId, title, versions) {
  const id = randomUUID();
  const doc = { id, workspace_id: wsId, title, deleted: false, created_at: now(), updated_at: now(), versions: [] };
  versions.forEach((v, i) => {
    const vid = randomUUID();
    doc.versions.push({
      id: vid,
      version_number: i + 1,
      filename: v.filename,
      type: v.type,
      bytes: v.bytes,
      sha: sha(++tick),
      chunks: v.chunks.map(([text, page_start, page_end, heading]) => ({ id: randomUUID(), text, page_start, page_end, heading_path: heading ? [heading] : [] })),
    });
    blobs.set(vid, { bytes: v.bytes, content_type: v.type });
  });
  documents.set(id, doc);
  return doc;
}

const MD = "# Meeting notes\n\nAction: ship the search page.\n\nDecision: keep chat answers grounded.\n";
const notes = acme.ws.notes.id;
const report = addDoc(notes, "Quarterly report", [
  { filename: "report-v1.pdf", type: "application/pdf", bytes: PDF, chunks: [["Revenue grew by two percent in the third quarter.", 2, 2, "Results"]] },
  {
    filename: "report-v2.pdf",
    type: "application/pdf",
    bytes: Buffer.concat([PDF, Buffer.from("% 2\n")]),
    chunks: [
      ["Revenue grew by ten percent in the third quarter.", 2, 2, "Results"],
      ["Operating costs fell because of lower travel spend.", 3, 3, "Results"],
      ["Outlook for the next quarter is stable.", 3, 4, "Outlook"],
    ],
  },
]);
const meeting = addDoc(notes, "Meeting notes", [
  { filename: "notes.md", type: "text/markdown", bytes: Buffer.from(MD), chunks: [["Action: ship the search page.", null, null, "Meeting notes"], ["Decision: keep chat answers grounded.", null, null, "Meeting notes"]] },
]);
const survey = addDoc(acme.ws.papers.id, "Embedding survey", [
  { filename: "survey.txt", type: "text/plain", bytes: Buffer.from("Embedding models map text to vectors.\n"), chunks: [["Embedding models map text to vectors.", null, null, null]] },
]);
const bench = addDoc(globex.ws.bench.id, "Bench readout", [
  { filename: "readout.txt", type: "text/plain", bytes: Buffer.from("The bench readout is nominal.\n"), chunks: [["The bench readout is nominal.", null, null, null]] },
]);
const vault = addDoc(globex.ws.vault.id, "Vault Secret Doc", [
  { filename: "secret.txt", type: "text/plain", bytes: Buffer.from("secret launch codes\n"), chunks: [["The secret launch codes are stored here.", null, null, null]] },
]);

// --- identity ---
let jwks = null;
async function verify(token) {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [h, p, s] = parts;
  try {
    const header = JSON.parse(Buffer.from(h, "base64url"));
    jwks ??= await (await fetch(`${issuer}/keys`)).json();
    let jwk = jwks.keys.find((k) => k.kid === header.kid);
    if (!jwk) {
      jwks = await (await fetch(`${issuer}/keys`)).json();
      jwk = jwks.keys.find((k) => k.kid === header.kid);
    }
    if (!jwk) return null;
    const ok = createVerify("RSA-SHA256").update(`${h}.${p}`).verify(createPublicKey({ key: jwk, format: "jwk" }), Buffer.from(s, "base64url"));
    if (!ok) return null;
    const claims = JSON.parse(Buffer.from(p, "base64url"));
    if (claims.iss !== issuer || (claims.exp && claims.exp * 1000 < Date.now())) return null;
    return claims;
  } catch {
    return null;
  }
}

const send = (res, status, body, entry, headers = {}) => {
  if (entry) entry.status = status;
  if (body === null) {
    res.writeHead(status, headers);
    return res.end();
  }
  res.writeHead(status, { "content-type": "application/json", ...headers });
  res.end(JSON.stringify(body));
};
const orgRole = (org, me) => org.members.get(me) ?? null;
function wsRole(w, org, me) {
  const r = orgRole(org, me);
  if (r === "owner" || r === "admin") return w.members.get(me) ?? "admin";
  return w.members.get(me) ?? null;
}
const wsOut = (w, org, me) => ({ id: w.id, org_id: w.org_id, slug: w.slug, name: w.name, created_at: w.created_at, role: wsRole(w, org, me) });
const perms = (role) => ({ write: role === "admin" || role === "editor", manage: role === "admin" });
const readable = (org, me) => [...workspaces.values()].filter((w) => w.org_id === org.id && wsRole(w, org, me));

const versionOut = (doc, v) => ({
  id: v.id,
  document_id: doc.id,
  version_number: v.version_number,
  original_filename: v.filename,
  content_type: v.type,
  size_bytes: v.bytes.length,
  sha256: v.sha,
  created_by: "u-alice",
  created_at: doc.created_at,
  retention: { status: v === doc.versions.at(-1) ? "current" : "superseded", purge_after: null },
});
const docOut = (doc, w, org, role) => ({
  id: doc.id,
  org_id: org.id,
  workspace_id: w.id,
  title: doc.title,
  created_by: "u-alice",
  created_at: doc.created_at,
  updated_at: doc.updated_at,
  version_count: doc.versions.length,
  latest_version: versionOut(doc, doc.versions.at(-1)),
  permissions: perms(role),
  ingestion: { state: "ready", stage: null, attempts_label: null, error_code: null, message: null, updated_at: doc.updated_at, can_retry: false },
});
const INLINE = new Set(["application/pdf", "text/plain", "text/markdown"]);

// --- search ---
const ctl = { search: "ok", chatMode: "ok", evidence: new Map() };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function highlightRanges(text, tokens) {
  const points = Array.from(text);
  const lower = points.map((c) => c.toLowerCase()).join("");
  const out = [];
  for (const t of tokens) {
    let from = 0;
    for (;;) {
      const i = lower.indexOf(t, from);
      if (i < 0) break;
      // offsets are code points: convert the UTF-16 index
      const start = Array.from(lower.slice(0, i)).length;
      out.push([start, start + Array.from(t).length]);
      from = i + t.length;
    }
  }
  return out.sort((a, b) => a[0] - b[0]);
}

function doSearch(req, res, org, me, body, entry) {
  entry.body = body;
  if (ctl.search === "rate") return send(res, 429, { detail: "rate_limited" }, entry, { "retry-after": "7" });
  if (ctl.search === "down") return send(res, 503, { detail: "search_unavailable" }, entry);
  const q = body?.query;
  const okMode = body?.mode === undefined || ["hybrid", "keyword", "vector"].includes(body.mode);
  const okLimit = body?.limit === undefined || (Number.isInteger(body.limit) && body.limit >= 1 && body.limit <= 50);
  if (typeof q !== "string" || q.trim() === "" || q.length > 500 || !okMode || !okLimit) return send(res, 422, { detail: [{ loc: ["body"], msg: "invalid" }] }, entry);
  let scope = readable(org, me);
  if (Array.isArray(body.workspace_ids)) scope = scope.filter((w) => body.workspace_ids.includes(w.id)); // may only narrow
  const types = Array.isArray(body.content_types) ? body.content_types : null;
  const tokens = q.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const results = [];
  for (const w of scope) {
    for (const doc of documents.values()) {
      if (doc.workspace_id !== w.id || doc.deleted) continue;
      const v = doc.versions.at(-1);
      if (types && !types.includes(v.type)) continue;
      const snippets = [];
      for (const c of v.chunks) {
        if (tokens.some((t) => c.text.toLowerCase().includes(t))) {
          snippets.push({ evidence_id: `e${snippets.length + 1}`, chunk_id: c.id, text: c.text, page_start: c.page_start, page_end: c.page_end, highlights: highlightRanges(c.text, tokens) });
        }
      }
      if (snippets.length) {
        results.push({ document_id: doc.id, title: doc.title, workspace_id: w.id, workspace_slug: w.slug, content_type: v.type, version_id: v.id, version_number: v.version_number, score: 0.5, snippets: snippets.slice(0, 3) });
      }
    }
  }
  return send(res, 200, { query: q, results: results.slice(0, body.limit ?? 10), degraded: ctl.search === "degraded" ? ["embedding_unavailable"] : [], took_ms: 3, scope: { workspace_count: scope.length } }, entry);
}

// --- chat ---
const threads = new Map(); // id -> {id, owner, org_id, workspace_id, title, created_at, updated_at, deleted, messages: []}
const runs = new Map(); // id -> {id, thread_id, owner, state, key, script: [], emitted: [], subs: Set, message_id, error_code}
const runKeys = new Map(); // `${thread}:${key}` -> run id
const streams = { n: 0 };

const threadOut = (t) => ({ id: t.id, workspace_id: t.workspace_id, title: t.title, created_at: t.created_at, updated_at: t.updated_at });
const isActive = (r) => r.state === "queued" || r.state === "running";

function scriptFor(content, thread, org) {
  const c1 = report.versions.at(-1).chunks[0];
  const c2 = report.versions.at(-1).chunks[1];
  const cit = (n, c) => ({ marker: `[${n}]`, chunk_id: c.id, document_id: report.id, version_id: report.versions.at(-1).id, workspace_id: report.workspace_id, title: report.title, version_number: 2, page_start: c.page_start, page_end: c.page_end, excerpt: c.text });
  const mid = randomUUID();
  const start = [["started", {}], ["retrieval", { evidence_count: 2, dropped: 0, degraded: [] }]];
  if (content.includes("FAIL")) return { mid, events: [...start.slice(0, 1), ["failed", { error_code: "model_unavailable", message: "internal: connection refused to 127.0.0.1:1234" }]] };
  if (content.includes("ABSTAIN")) {
    return { mid, events: [["started", {}], ["retrieval", { evidence_count: 0, dropped: 0, degraded: [] }], ["verification", { status: "abstained", withheld_count: 0, flagged_count: 0 }], ["completed", { message_id: mid, answer_text: "I couldn't find this in your documents." }]], assistant: { content: "I couldn't find this in your documents.", citations: [], verification: { status: "abstained", withheld_count: 0, flagged_count: 0 } } };
  }
  const partial = content.includes("PARTIAL");
  const kept = "Revenue grew by ten percent [1]. Costs fell [2].";
  return {
    mid,
    events: [
      ...start,
      ["delta", { text: "Revenue grew by ten percent [1]. " }],
      ["delta", { text: partial ? "Costs fell [2]. Moon is made of cheese." : "Costs fell [2]." }],
      ["citations", { citations: [cit(1, c1), cit(2, c2)] }],
      ["verification", partial ? { status: "partial", withheld_count: 1, flagged_count: 0 } : { status: "grounded", withheld_count: 0, flagged_count: 0 }],
      ["completed", { message_id: mid, answer_text: kept }],
    ],
    assistant: { content: kept, citations: [cit(1, c1), cit(2, c2)], verification: partial ? { status: "partial", withheld_count: 1, flagged_count: 0 } : { status: "grounded", withheld_count: 0, flagged_count: 0 } },
  };
}

const frame = (r, ev) => `id: ${ev.seq}\nevent: ${ev.type}\ndata: ${JSON.stringify(ev.data)}\n\n`;
const TERMINAL = { completed: "completed", failed: "failed", cancelled: "cancelled" };

function emit(run, n) {
  for (let i = 0; i < n && run.script.length > 0; i++) {
    const [type, data] = run.script.shift();
    const ev = { seq: run.emitted.length + 1, type, data };
    run.emitted.push(ev);
    if (run.state === "queued") run.state = "running";
    if (TERMINAL[type]) {
      run.state = TERMINAL[type];
      if (type === "failed") run.error_code = data.error_code;
      if (type === "completed" && run.assistant) {
        const t = threads.get(run.thread_id);
        t.messages.push({ id: run.message_id, role: "assistant", content: run.assistant.content, citations: run.assistant.citations, verification: run.assistant.verification, run_id: run.id, created_at: now() });
        t.updated_at = now();
      }
    }
    for (const sub of [...run.subs]) {
      sub.res.write(frame(run, ev));
      if (TERMINAL[type]) {
        sub.res.write(`event: end\ndata: ${JSON.stringify({ reason: "terminal" })}\n\n`);
        sub.res.end();
      }
    }
    if (TERMINAL[type]) run.subs.clear();
  }
}

function runStream(req, res, run, entry) {
  entry.status = 200;
  entry.last_event_id = req.headers["last-event-id"] ?? null;
  entry.accept = req.headers.accept ?? null;
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
  const last = Number(req.headers["last-event-id"] ?? 0) || 0;
  res.write("retry: 3000\n\n");
  for (const ev of run.emitted) if (ev.seq > last) res.write(frame(run, ev));
  if (!isActive(run)) {
    res.write(`event: end\ndata: ${JSON.stringify({ reason: "terminal" })}\n\n`);
    return res.end();
  }
  const sub = { res };
  run.subs.add(sub);
  streams.n += 1;
  const ka = setInterval(() => res.write(": keep-alive\n\n"), 15_000);
  res.on("close", () => {
    clearInterval(ka);
    run.subs.delete(sub);
    if (!sub.closed) {
      sub.closed = true;
      streams.n -= 1;
    }
  });
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const text = Buffer.concat(chunks).toString("utf8");
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function chatRoute(req, res, org, me, parts, url, entry) {
  // parts: v1 orgs {o} chat ...
  const rest = parts.slice(4);
  if (ctl.chatMode === "rate" && req.method === "POST" && rest[0] === "threads" && rest[2] === "runs") {
    return send(res, 429, { detail: "rate_limited" }, entry, { "retry-after": "5" });
  }
  if (rest[0] === "threads") {
    if (rest.length === 1 && req.method === "POST") {
      const body = await readBody(req);
      if (body === null || typeof body !== "object") return send(res, 422, { detail: "invalid_request" }, entry);
      let workspaceId = null;
      if (body.workspace_id !== undefined && body.workspace_id !== null) {
        const w = workspaces.get(body.workspace_id);
        if (!w || w.org_id !== org.id || !wsRole(w, org, me)) return send(res, 404, { detail: "not_found" }, entry);
        workspaceId = w.id;
      }
      const t = { id: randomUUID(), owner: me, org_id: org.id, workspace_id: workspaceId, title: typeof body.title === "string" ? body.title : "", created_at: now(), updated_at: now(), deleted: false, messages: [] };
      threads.set(t.id, t);
      return send(res, 201, threadOut(t), entry);
    }
    if (rest.length === 1 && req.method === "GET") {
      const limit = Math.min(Math.max(Number(url.searchParams.get("limit") ?? 30) || 30, 1), 100);
      const after = Number(Buffer.from(url.searchParams.get("after") ?? "", "base64url").toString() || 0) || 0;
      const mine = [...threads.values()].filter((t) => t.owner === me && t.org_id === org.id && !t.deleted).sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
      const page = mine.slice(after, after + limit);
      const next = after + limit < mine.length ? Buffer.from(String(after + limit)).toString("base64url") : null;
      return send(res, 200, { items: page.map(threadOut), next_cursor: next }, entry);
    }
    const t = threads.get(rest[1]);
    if (!t || t.owner !== me || t.org_id !== org.id || t.deleted) return send(res, 404, { detail: "not_found" }, entry);
    if (rest.length === 2 && req.method === "GET") {
      const active = [...runs.values()].find((r) => r.thread_id === t.id && isActive(r));
      return send(res, 200, { thread: threadOut(t), messages: t.messages, active_run: active ? { run_id: active.id, state: active.state } : null }, entry);
    }
    if (rest.length === 2 && req.method === "DELETE") {
      t.deleted = true;
      return send(res, 204, null, entry);
    }
    if (rest.length === 2 && req.method === "PATCH") {
      const body = await readBody(req);
      if (!body || typeof body.title !== "string") return send(res, 422, { detail: "invalid_request" }, entry);
      t.title = body.title;
      return send(res, 200, threadOut(t), entry);
    }
    if (rest.length === 3 && rest[2] === "runs" && req.method === "POST") {
      const body = await readBody(req);
      entry.body = body;
      const ok = body && typeof body.content === "string" && body.content.trim().length >= 1 && body.content.length <= 4000 && typeof body.idempotency_key === "string" && body.idempotency_key.length >= 8 && body.idempotency_key.length <= 128;
      if (!ok) return send(res, 422, { detail: [{ msg: "invalid" }] }, entry);
      const existing = runKeys.get(`${t.id}:${body.idempotency_key}`);
      if (existing) {
        const r = runs.get(existing);
        entry.idempotent_replay = true;
        return send(res, 202, { run_id: r.id, message_id: r.user_message_id, state: r.state }, entry);
      }
      if ([...runs.values()].filter((r) => r.owner === me && isActive(r)).length >= 2) return send(res, 409, { detail: "too_many_runs" }, entry);
      let scopeIds = readable(org, me).map((w) => w.id);
      if (Array.isArray(body.workspace_ids)) scopeIds = scopeIds.filter((id) => body.workspace_ids.includes(id));
      const sc = scriptFor(body.content, t, org);
      const run = { id: randomUUID(), thread_id: t.id, owner: me, state: "queued", key: body.idempotency_key, script: sc.events, emitted: [], subs: new Set(), message_id: sc.mid, assistant: sc.assistant, user_message_id: randomUUID(), scope: scopeIds, created_at: now() };
      runs.set(run.id, run);
      runKeys.set(`${t.id}:${body.idempotency_key}`, run.id);
      t.messages.push({ id: run.user_message_id, role: "user", content: body.content, citations: [], verification: null, run_id: run.id, created_at: now() });
      if (!t.title) t.title = body.content.slice(0, 40);
      t.updated_at = now();
      entry.run_id = run.id;
      return send(res, 202, { run_id: run.id, message_id: run.user_message_id, state: run.state }, entry);
    }
    return send(res, 404, { detail: "not_found" }, entry);
  }
  if (rest[0] === "runs") {
    const run = runs.get(rest[1]);
    const t = run ? threads.get(run.thread_id) : null;
    if (!run || run.owner !== me || !t || t.org_id !== org.id || t.deleted) return send(res, 404, { detail: "not_found" }, entry);
    if (rest.length === 2 && req.method === "GET") {
      return send(res, 200, { run_id: run.id, thread_id: run.thread_id, state: run.state, error_code: run.error_code ?? null, message: null, created_at: run.created_at, finished_at: isActive(run) ? null : now() }, entry);
    }
    if (rest[2] === "events" && req.method === "GET") return runStream(req, res, run, entry);
    if (rest[2] === "cancel" && req.method === "POST") {
      if (isActive(run)) {
        run.script = [["cancelled", {}]];
        emit(run, 1);
      }
      return send(res, 202, { state: run.state }, entry);
    }
  }
  return send(res, 404, { detail: "not_found" }, entry);
}

async function handle(req, res, url) {
  const auth = req.headers.authorization ?? "";
  const claims = auth.startsWith("Bearer ") ? await verify(auth.slice(7)) : null;
  const me = claims ? (claims.email ?? claims.sub) : null;
  const entry = { method: req.method, path: url.pathname + url.search, caller: me };
  log.push(entry);
  if (!me) return send(res, 401, { detail: "invalid_token" }, entry);

  const parts = url.pathname.split("/").filter(Boolean);
  if (parts[0] !== "v1") return send(res, 404, { detail: "not_found" }, entry);
  const [, a, orgId, b, wsId, c, docId, d, versionId, e, chunkId] = parts;
  if (a === "me") return send(res, 200, { id: claims.sub, email: claims.email ?? null, display_name: claims.name ?? null }, entry);
  if (a !== "orgs") return send(res, 404, { detail: "not_found" }, entry);

  if (parts.length === 2 && req.method === "GET") {
    const mine = [...orgs.values()].filter((o) => orgRole(o, me)).map((o) => ({ id: o.id, slug: o.slug, name: o.name, role: orgRole(o, me) }));
    return send(res, 200, { items: mine }, entry);
  }
  const org = orgs.get(orgId);
  if (!org || !orgRole(org, me)) return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 3) return send(res, 200, { id: org.id, slug: org.slug, name: org.name, created_at: org.created_at, role: orgRole(org, me) }, entry);
  if (b === "search" && parts.length === 4 && req.method === "POST") return doSearch(req, res, org, me, await readBody(req), entry);
  if (b === "chat") return chatRoute(req, res, org, me, parts, url, entry);
  if (b !== "workspaces") return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 4 && req.method === "GET") return send(res, 200, { items: readable(org, me).map((w) => wsOut(w, org, me)) }, entry);
  const w = workspaces.get(wsId);
  const role = w && w.org_id === org.id ? wsRole(w, org, me) : null;
  if (!role) return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 5) return send(res, 200, wsOut(w, org, me), entry);
  if (c !== "documents") return send(res, 404, { detail: "not_found" }, entry);
  const doc = documents.get(docId);
  if (!doc || doc.workspace_id !== w.id || doc.deleted) return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 7 && req.method === "GET") return send(res, 200, docOut(doc, w, org, role), entry);
  if (d !== "versions") return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 8 && req.method === "GET") return send(res, 200, { items: [...doc.versions].reverse().map((v) => versionOut(doc, v)), next_cursor: null }, entry);
  const ver = doc.versions.find((v) => v.id === versionId);
  if (!ver) return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 9 && req.method === "GET") return send(res, 200, versionOut(doc, ver), entry);
  if (parts.length === 10 && e === "content-url" && req.method === "GET") {
    const want = url.searchParams.get("disposition") === "inline" ? "inline" : "attachment";
    const disposition = want === "inline" && INLINE.has(ver.type) ? "inline" : "attachment";
    const exp = Date.now() + 5 * 60_000;
    return send(res, 200, { url: `${origin}/__store/${ver.id}?disp=${disposition}&exp=${exp}&sig=ok`, expires_at: new Date(exp).toISOString(), filename: ver.filename, content_type: ver.type, disposition }, entry);
  }
  if (parts.length === 11 && e === "chunks" && req.method === "GET") {
    const mode = ctl.evidence.get(chunkId) ?? "ok";
    const i = ver.chunks.findIndex((ch) => ch.id === chunkId);
    if (i < 0) return send(res, 404, { detail: "not_found" }, entry);
    if (mode === "gone") return send(res, 410, { detail: "evidence_unavailable" }, entry);
    if (mode === "down") return send(res, 503, { detail: "storage_unavailable" }, entry);
    const ch = ver.chunks[i];
    return send(res, 200, { chunk_id: ch.id, text: ch.text, page_start: ch.page_start, page_end: ch.page_end, heading_path: ch.heading_path, locators: [], version_number: ver.version_number, title: doc.title, neighbors: { prev_chunk_id: ver.chunks[i - 1]?.id ?? null, next_chunk_id: ver.chunks[i + 1]?.id ?? null } }, entry);
  }
  return send(res, 404, { detail: "not_found" }, entry);
}

const server = createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const json = (body) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (url.pathname === "/__log") return json(log);
  if (url.pathname === "/__control/search") {
    ctl.search = url.searchParams.get("mode") ?? "ok";
    return json({ search: ctl.search });
  }
  if (url.pathname === "/__control/evidence") {
    ctl.evidence.set(url.searchParams.get("chunk"), url.searchParams.get("mode") ?? "ok");
    return json({ ok: true });
  }
  if (url.pathname === "/__chat/mode") {
    ctl.chatMode = url.searchParams.get("mode") ?? "ok";
    return json({ chat: ctl.chatMode });
  }
  if (url.pathname === "/__chat/state") {
    return json({ streams: streams.n, runs: Object.fromEntries([...runs].map(([id, r]) => [id, { state: r.state, emitted: r.emitted.length, remaining: r.script.length, subs: r.subs.size, key: r.key, thread: r.thread_id }])), threads: threads.size });
  }
  if (["/__chat/step", "/__chat/drain", "/__chat/drop", "/__chat/end"].includes(url.pathname)) {
    const run = runs.get(url.searchParams.get("run"));
    if (!run) {
      res.writeHead(404);
      return res.end();
    }
    if (url.pathname === "/__chat/step") emit(run, Number(url.searchParams.get("n") ?? 1));
    if (url.pathname === "/__chat/drain") emit(run, 1000);
    if (url.pathname === "/__chat/drop") {
      for (const sub of [...run.subs]) {
        run.subs.delete(sub);
        sub.res.destroy();
      }
    }
    if (url.pathname === "/__chat/end") {
      for (const sub of [...run.subs]) {
        run.subs.delete(sub);
        sub.res.write(`event: end\ndata: ${JSON.stringify({ reason: url.searchParams.get("reason") ?? "timeout" })}\n\n`);
        sub.res.end();
      }
    }
    return json({ state: run.state, emitted: run.emitted.length, remaining: run.script.length });
  }
  if (url.pathname === "/__seed") {
    const docInfo = (d) => ({ id: d.id, workspace_id: d.workspace_id, versions: d.versions.map((v) => v.id), chunks: Object.fromEntries(d.versions.map((v) => [v.id, v.chunks.map((c) => c.id)])) });
    return json({
      orgs: { acme: acme.org.id, globex: globex.org.id },
      workspaces: { notes, papers: acme.ws.papers.id, bench: globex.ws.bench.id, vault: globex.ws.vault.id },
      docs: { report: docInfo(report), meeting: docInfo(meeting), survey: docInfo(survey), bench: docInfo(bench), vault: docInfo(vault) },
    });
  }
  if (url.pathname.startsWith("/__store/")) {
    const vid = url.pathname.slice("/__store/".length);
    const blob = blobs.get(vid);
    const cors = { "access-control-allow-origin": "*" };
    if (!blob || url.searchParams.get("sig") !== "ok" || Number(url.searchParams.get("exp")) < Date.now()) {
      res.writeHead(blob ? 403 : 404, cors);
      return res.end();
    }
    res.writeHead(200, { ...cors, "content-type": blob.content_type, "x-content-type-options": "nosniff" });
    return res.end(blob.bytes);
  }
  handle(req, res, url).catch((err) => {
    console.error(err);
    res.writeHead(500, { "content-type": "application/json" });
    res.end(JSON.stringify({ detail: "stub_error" }));
  });
});

server.listen(port, "127.0.0.1", () => console.log(`web_search stub-api listening on 127.0.0.1:${port}`));
