// M04.03 end-to-end over HTTP (NO browser): real Dex login, the built web app (pages + BFF), and an in-memory stub
// of the M04 search / chat / evidence API shapes. Run through ./run.sh. Browser behaviour (hydration, clicks) is
// covered by the vitest suites, not here.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";

const WEB = process.env.WEB_URL ?? "http://127.0.0.1:39440";
const STUB = process.env.STUB_URL ?? "http://127.0.0.1:39441";
const USERS = { alice: { login: "alice@ukos.test", password: "ukos-dev-alice-password" }, bob: { login: "bob@ukos.test", password: "ukos-dev-bob-password" } };
const SESSION_COOKIE = "ukos_session";
const TX_COOKIE = "ukos_auth_tx";
const JWT_RE = /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\./;

const bodies = [];
const sessions = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function setCookies(res) {
  const out = {};
  for (const raw of res.headers.getSetCookie()) {
    const pair = raw.split(";")[0];
    const eq = pair.indexOf("=");
    out[pair.slice(0, eq)] = pair.slice(eq + 1);
  }
  return out;
}
const unescapeHtml = (s) => s.replace(/&amp;/g, "&").replace(/&#43;/g, "+").replace(/&quot;/g, '"');

async function login(user) {
  const start = await fetch(new URL("/api/auth/login?returnTo=%2Fhome", WEB), { redirect: "manual" });
  assert.equal(start.status, 302);
  const tx = setCookies(start)[TX_COOKIE];
  let url = start.headers.get("location");
  let formAction;
  for (let i = 0; i < 5 && !formAction; i++) {
    const r = await fetch(url, { redirect: "manual" });
    if (r.status >= 300 && r.status < 400) {
      url = new URL(r.headers.get("location"), url).href;
      continue;
    }
    const m = (await r.text()).match(/<form method="post" action="([^"]+)"/);
    assert.ok(m, "Dex login form");
    formAction = new URL(unescapeHtml(m[1]), url).href;
  }
  let res = await fetch(formAction, { method: "POST", redirect: "manual", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(user).toString() });
  url = formAction;
  let callback;
  for (let i = 0; i < 5 && res.status >= 300 && res.status < 400; i++) {
    const next = new URL(res.headers.get("location"), url).href;
    if (next.startsWith(`${WEB}/api/auth/callback`)) {
      callback = next;
      break;
    }
    url = next;
    res = await fetch(url, { redirect: "manual" });
  }
  assert.ok(callback, "Dex redirected back to the callback");
  const cb = await fetch(callback, { redirect: "manual", headers: { cookie: `${TX_COOKIE}=${tx}` } });
  const session = setCookies(cb)[SESSION_COOKIE];
  assert.ok(session, "session cookie set");
  return session;
}

async function web(path, { cookie, method = "GET", headers = {} } = {}) {
  const res = await fetch(path.startsWith("http") ? path : WEB + path, { method, redirect: "manual", headers: { ...(cookie ? { cookie: `${SESSION_COOKIE}=${cookie}` } : {}), ...headers } });
  const text = await res.text();
  bodies.push(text);
  return { res, text, html: text.replace(/<!-- -->/g, "") };
}
const stubJson = async (p) => (await fetch(STUB + p)).json();
const control = (p) => stubJson(p);

/** A same-origin BFF call, like the browser's fetch (cookie + Origin on unsafe methods). */
async function api(who, method, path, { body, origin = WEB, headers = {} } = {}) {
  const res = await fetch(WEB + "/api" + path, {
    method,
    headers: { cookie: `${SESSION_COOKIE}=${sessions[who]}`, ...(method !== "GET" && origin ? { origin } : {}), ...(body !== undefined ? { "content-type": "application/json" } : {}), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  bodies.push(text);
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    /* not json */
  }
  return { res, status: res.status, json, text };
}

/** Reads SSE frames from a streaming response. */
function sseReader(res) {
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  async function next(ms = 5000) {
    const deadline = Date.now() + ms;
    for (;;) {
      const i = buf.indexOf("\n\n");
      if (i >= 0) {
        const raw = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const f = { event: "message", id: null, data: "", comment: false };
        for (const line of raw.split("\n")) {
          if (line.startsWith(":")) f.comment = true;
          else if (line.startsWith("event: ")) f.event = line.slice(7);
          else if (line.startsWith("id: ")) f.id = line.slice(4);
          else if (line.startsWith("data: ")) f.data += line.slice(6);
        }
        return f;
      }
      const left = deadline - Date.now();
      if (left <= 0) throw new Error("timed out waiting for an SSE frame");
      const chunk = await Promise.race([reader.read().catch(() => ({ done: true })), sleep(left).then(() => ({ timeout: true }))]);
      if (chunk.timeout) throw new Error("timed out waiting for an SSE frame");
      if (chunk.done) return null; // closed
      buf += dec.decode(chunk.value, { stream: true });
    }
  }
  return { next, cancel: () => reader.cancel().catch(() => undefined) };
}

async function openRunStream(who, orgId, runId, headers = {}, signal) {
  const res = await fetch(`${WEB}/api/v1/orgs/${orgId}/chat/runs/${runId}/events`, { headers: { cookie: `${SESSION_COOKIE}=${sessions[who]}`, accept: "text/event-stream", ...headers }, signal });
  return { res, sse: res.ok ? sseReader(res) : null };
}
async function untilStreamsClosed() {
  for (let i = 0; i < 30 && (await stubJson("/__chat/state")).streams !== 0; i++) await sleep(100);
  return (await stubJson("/__chat/state")).streams;
}

let seed;
let ACME;
let GLOBEX;
before(async () => {
  seed = await stubJson("/__seed");
  sessions.alice = await login(USERS.alice);
  sessions.bob = await login(USERS.bob);
  ACME = seed.orgs.acme;
  GLOBEX = seed.orgs.globex;
});
after(() => void 0);

const newKey = () => `key-${Math.random().toString(36).slice(2)}-${Date.now()}`;
async function newThread(who = "alice", org = ACME, body = {}) {
  const r = await api(who, "POST", `/v1/orgs/${org}/chat/threads`, { body });
  assert.equal(r.status, 201, r.text);
  return r.json;
}
async function newRun(who, thread, content, { key = newKey(), org = ACME, workspace_ids } = {}) {
  const r = await api(who, "POST", `/v1/orgs/${org}/chat/threads/${thread}/runs`, { body: { content, idempotency_key: key, ...(workspace_ids ? { workspace_ids } : {}) } });
  assert.equal(r.status, 202, r.text);
  return r.json;
}
const step = (run, n = 1) => stubJson(`/__chat/step?run=${run}&n=${n}`);

// ---------------------------------------------------------------------------------------------- pages and auth

test("signed out: search, chat and evidence pages go to login; the BFF refuses search, chat and SSE (401)", async () => {
  const report = seed.docs.report;
  for (const [path, returnTo] of [
    ["/o/acme/search", "%2Fo%2Facme%2Fsearch"],
    ["/o/acme/chat", "%2Fo%2Facme%2Fchat"],
    [`/o/acme/w/notes/documents/${report.id}/evidence?v=${report.versions[1]}`, null],
  ]) {
    const r = await web(path);
    assert.equal(r.res.status, 307, path);
    assert.match(r.res.headers.get("location"), /\/api\/auth\/login\?returnTo=/);
    if (returnTo) assert.ok(r.res.headers.get("location").endsWith(returnTo), path);
  }
  const post = await fetch(`${WEB}/api/v1/orgs/${ACME}/search`, { method: "POST", headers: { origin: WEB, "content-type": "application/json" }, body: JSON.stringify({ query: "x" }) });
  assert.equal(post.status, 401);
  const sse = await fetch(`${WEB}/api/v1/orgs/${ACME}/chat/runs/${report.id}/events`, { headers: { accept: "text/event-stream" } });
  assert.equal(sse.status, 401);
  await sse.text();
});

test("search page: shell, nav entries, scope/filter controls reflect the URL, and the server makes no search call", async () => {
  const before = (await stubJson("/__log")).length;
  const plain = await web("/o/acme/search", { cookie: sessions.alice });
  assert.equal(plain.res.status, 200);
  assert.match(plain.html, /data-testid="search-page"/);
  assert.match(plain.html, /href="\/o\/acme\/search"/, "Search nav entry");
  assert.match(plain.html, /href="\/o\/acme\/chat"/, "Chat nav entry");
  assert.match(plain.html, /All my workspaces/);
  assert.match(plain.html, /Acme Notes/);
  assert.match(plain.html, /Acme Papers/);
  assert.match(plain.html, /data-testid="search-idle"/);

  const state = await web("/o/acme/search?q=revenue&ws=papers&type=pdf&mode=keyword", { cookie: sessions.alice });
  assert.match(state.html, /id="search-q"[^>]*value="revenue"|value="revenue"[^>]*id="search-q"/);
  assert.match(state.html, /<option value="papers" selected="">/);
  assert.match(state.html, /<button[^>]*aria-pressed="true"[^>]*>PDF<\/button>/); // type filter is a pill group since M09
  assert.match(state.html, /<option value="keyword" selected="">/);
  const calls = (await stubJson("/__log")).slice(before);
  assert.ok(!calls.some((e) => e.path.endsWith("/search")), "results are fetched by the browser, never by the server render");

  const junk = await web("/o/acme/search?type=exe&mode=magic&ws=..%2F..&q=", { cookie: sessions.alice });
  assert.match(junk.html, /<option value="all" selected="">|<option value="all">/);
});

test("search page: unknown or foreign orgs are the calm not-found page, nothing leaks", async () => {
  for (const path of ["/o/initech/search", "/o/no-such-org/search", "/o/initech/chat", "/o/no-such-org/chat/abc"]) {
    const r = await web(path, { cookie: sessions.alice });
    assert.match(r.html, /data-testid="scope-not-found"/, path);
    for (const leak of ["Initech", "Secret Plans", "data-testid=\"search-page\"", "data-testid=\"chat-app\""]) assert.ok(!r.html.includes(leak), `${path} leaks ${leak}`);
  }
});

// ---------------------------------------------------------------------------------------------------------- search

test("search through the BFF: results grouped per document, highlights and chunk ids; the body reaches the API unchanged", async () => {
  const body = { query: "revenue", workspace_ids: [seed.workspaces.notes], content_types: ["application/pdf"], mode: "keyword", limit: 20 };
  const r = await api("alice", "POST", `/v1/orgs/${ACME}/search`, { body });
  assert.equal(r.status, 200);
  assert.equal(r.res.headers.get("cache-control"), "no-store");
  assert.equal(r.res.headers.get("set-cookie"), null);
  assert.equal(r.json.results.length, 1);
  const hit = r.json.results[0];
  assert.equal(hit.title, "Quarterly report");
  assert.equal(hit.workspace_slug, "notes");
  assert.equal(hit.version_id, seed.docs.report.versions[1], "the current version");
  assert.equal(hit.snippets[0].chunk_id, seed.docs.report.chunks[hit.version_id][0]);
  assert.deepEqual(hit.snippets[0].highlights, [[0, 7]]);
  assert.equal(r.json.scope.workspace_count, 1);
  const logged = (await stubJson("/__log")).filter((e) => e.method === "POST" && e.path === `/v1/orgs/${ACME}/search`).at(-1);
  assert.deepEqual(logged.body, body);
  assert.equal(logged.caller, "alice@ukos.test");
});

test("search is scoped on the server: a forged workspace list can only narrow; other tenants' documents never appear", async () => {
  // alice is a MEMBER of globex with only `bench`: asking for the vault yields nothing, not an error
  const forged = await api("alice", "POST", `/v1/orgs/${GLOBEX}/search`, { body: { query: "secret", workspace_ids: [seed.workspaces.vault] } });
  assert.equal(forged.status, 200);
  assert.deepEqual(forged.json.results, []);
  assert.equal(forged.json.scope.workspace_count, 0);
  const all = await api("alice", "POST", `/v1/orgs/${GLOBEX}/search`, { body: { query: "secret launch codes" } });
  assert.deepEqual(all.json.results, []);
  assert.ok(!all.text.includes("Vault Secret Doc"));
  // bob is an acme member with a viewer role in notes only: the papers document is invisible to him
  const bob = await api("bob", "POST", `/v1/orgs/${ACME}/search`, { body: { query: "embedding" } });
  assert.deepEqual(bob.json.results, []);
  const bobRev = await api("bob", "POST", `/v1/orgs/${ACME}/search`, { body: { query: "revenue" } });
  assert.equal(bobRev.json.results.length, 1);
  // an org the caller is not in: concealed 404
  const initech = Object.keys({}).length; // (no id known to alice: use a random uuid)
  const other = await api("alice", "POST", `/v1/orgs/00000000-0000-4000-8000-000000000009/search`, { body: { query: "plans" } });
  assert.equal(other.status, 404);
  void initech;
});

test("search degraded (embeddings unavailable) is reported; 429 keeps Retry-After; 503 passes through; recovery works", async () => {
  await control("/__control/search?mode=degraded");
  const d = await api("alice", "POST", `/v1/orgs/${ACME}/search`, { body: { query: "revenue" } });
  assert.equal(d.status, 200);
  assert.deepEqual(d.json.degraded, ["embedding_unavailable"]);
  assert.equal(d.json.results.length, 1, "keyword results are still returned");
  await control("/__control/search?mode=rate");
  const rl = await api("alice", "POST", `/v1/orgs/${ACME}/search`, { body: { query: "revenue" } });
  assert.equal(rl.status, 429);
  assert.equal(rl.res.headers.get("retry-after"), "7");
  assert.equal(rl.json.detail, "rate_limited");
  await control("/__control/search?mode=down");
  const down = await api("alice", "POST", `/v1/orgs/${ACME}/search`, { body: { query: "revenue" } });
  assert.equal(down.status, 503);
  assert.equal(down.json.detail, "search_unavailable");
  await control("/__control/search?mode=ok");
  const ok = await api("alice", "POST", `/v1/orgs/${ACME}/search`, { body: { query: "revenue" } });
  assert.equal(ok.status, 200);
  assert.deepEqual(ok.json.degraded, []);
});

test("search validation: blank, too long and bad mode/limit are 422 (no results leaked)", async () => {
  for (const body of [{ query: "   " }, { query: "x".repeat(501) }, { query: "ok", mode: "magic" }, { query: "ok", limit: 51 }, {}]) {
    const r = await api("alice", "POST", `/v1/orgs/${ACME}/search`, { body });
    assert.equal(r.status, 422, JSON.stringify(body).slice(0, 40));
  }
});

test("BFF CSRF on POSTs: no Origin, a foreign Origin and a cross-site Sec-Fetch-Site are refused (403) and never reach the API", async () => {
  const before = (await stubJson("/__log")).length;
  for (const [label, init] of [
    ["no origin", { origin: null }],
    ["evil origin", { origin: "http://evil.invalid" }],
    ["cross-site", { headers: { "sec-fetch-site": "cross-site" } }],
  ]) {
    const s = await api("alice", "POST", `/v1/orgs/${ACME}/search`, { body: { query: "revenue" }, ...init });
    assert.equal(s.status, 403, `search ${label}`);
    const t = await api("alice", "POST", `/v1/orgs/${ACME}/chat/threads`, { body: {}, ...init });
    assert.equal(t.status, 403, `thread ${label}`);
  }
  const after = (await stubJson("/__log")).slice(before);
  assert.equal(after.length, 0, "refused before reaching the API");
});

// -------------------------------------------------------------------------------------------------------- evidence

test("evidence API through the BFF: exact chunk, neighbours, 404 for another version/tenant, 410 and 503 pass through", async () => {
  const report = seed.docs.report;
  const [v1, v2] = report.versions;
  const [c1, c2, c3] = report.chunks[v2];
  const path = (ws, doc, v, c) => `/v1/orgs/${ACME}/workspaces/${ws}/documents/${doc}/versions/${v}/chunks/${c}`;
  const ok = await api("alice", "GET", path(seed.workspaces.notes, report.id, v2, c2));
  assert.equal(ok.status, 200);
  assert.equal(ok.json.text, "Operating costs fell because of lower travel spend.");
  assert.deepEqual(ok.json.neighbors, { prev_chunk_id: c1, next_chunk_id: c3 });
  assert.equal(ok.json.version_number, 2);
  assert.equal(ok.json.page_start, 3);
  // never another version's chunk
  assert.equal((await api("alice", "GET", path(seed.workspaces.notes, report.id, v1, c2))).status, 404);
  // another tenant's / workspace's chunk is concealed
  const vault = seed.docs.vault;
  const vaultVersion = vault.versions[0];
  const vaultChunk = vault.chunks[vaultVersion][0];
  const lost = await api("alice", "GET", `/v1/orgs/${GLOBEX}/workspaces/${seed.workspaces.vault}/documents/${vault.id}/versions/${vaultVersion}/chunks/${vaultChunk}`);
  assert.equal(lost.status, 404);
  assert.ok(!lost.text.includes("secret"));
  await control(`/__control/evidence?chunk=${c3}&mode=gone`);
  const gone = await api("alice", "GET", path(seed.workspaces.notes, report.id, v2, c3));
  assert.equal(gone.status, 410);
  assert.equal(gone.json.detail, "evidence_unavailable");
  await control(`/__control/evidence?chunk=${c3}&mode=down`);
  assert.equal((await api("alice", "GET", path(seed.workspaces.notes, report.id, v2, c3))).status, 503);
  await control(`/__control/evidence?chunk=${c3}&mode=ok`);
});

test("evidence page: shell for the exact version; an older version carries the newer-version note; deep link by slug or id", async () => {
  const report = seed.docs.report;
  const [v1, v2] = report.versions;
  const c = report.chunks[v1][0];
  const old = await web(`/o/acme/w/notes/documents/${report.id}/evidence?v=${v1}&chunk=${c}`, { cookie: sessions.alice });
  assert.equal(old.res.status, 200);
  assert.match(old.html, /data-testid="evidence-viewer"/);
  assert.match(old.html, /data-testid="evidence-title"[^>]*>Quarterly report</);
  assert.match(old.html, /Version 1/);
  assert.match(old.html, /data-testid="newer-version-note"/);
  assert.match(old.html, /A newer version \(version 2\)/);
  assert.match(old.html, new RegExp(`documents/${report.id}\\?v=${v2}`), "link to the newest version");

  const cur = await web(`/o/acme/w/notes/documents/${report.id}/evidence?v=${v2}&chunk=${report.chunks[v2][0]}`, { cookie: sessions.alice });
  assert.doesNotMatch(cur.html, /data-testid="newer-version-note"/);
  // the workspace segment may be the id (chat citations carry ids)
  const byId = await web(`/o/acme/w/${seed.workspaces.notes}/documents/${report.id}/evidence?v=${v2}&chunk=${report.chunks[v2][0]}`, { cookie: sessions.alice });
  assert.match(byId.html, /data-testid="evidence-viewer"/);
  // no ?v= means the latest version; a page-only link has no chunk
  const pageOnly = await web(`/o/acme/w/notes/documents/${report.id}/evidence?v=${v2}&page=3`, { cookie: sessions.alice });
  assert.match(pageOnly.html, /data-testid="evidence-no-chunk"/);
  assert.match(pageOnly.html, /Showing the document at page 3/);
});

test("evidence page: unknown version, another document's version, foreign workspace or document are calm 'not available'; nothing leaks", async () => {
  const report = seed.docs.report;
  const meeting = seed.docs.meeting;
  const bad = [
    `/o/acme/w/notes/documents/${report.id}/evidence?v=00000000-0000-4000-8000-000000000000`,
    `/o/acme/w/notes/documents/${report.id}/evidence?v=${meeting.versions[0]}`,
    `/o/acme/w/notes/documents/00000000-0000-4000-8000-000000000001/evidence?v=${report.versions[0]}`,
  ];
  for (const path of bad) {
    const r = await web(path, { cookie: sessions.alice });
    assert.match(r.html, /data-testid="document-unavailable"/, path);
    assert.ok(!r.html.includes("data-testid=\"evidence-viewer\""), path);
  }
  const vault = seed.docs.vault;
  for (const path of [
    `/o/globex/w/vault/documents/${vault.id}/evidence?v=${vault.versions[0]}`,
    `/o/globex/w/${seed.workspaces.vault}/documents/${vault.id}/evidence?v=${vault.versions[0]}`,
    `/o/initech/w/plans/documents/${vault.id}/evidence`,
  ]) {
    const r = await web(path, { cookie: sessions.alice });
    assert.match(r.html, /data-testid="scope-not-found"/, path);
    assert.ok(!r.html.includes("Vault Secret Doc") && !r.html.includes("secret launch"), path);
  }
  // bob may read notes but not papers
  const survey = seed.docs.survey;
  const b = await web(`/o/acme/w/papers/documents/${survey.id}/evidence?v=${survey.versions[0]}`, { cookie: sessions.bob });
  assert.match(b.html, /data-testid="scope-not-found"/);
  assert.ok(!b.html.includes("Embedding survey"));
});

// ------------------------------------------------------------------------------------------------------------ chat

test("chat pages: shell for a new chat and for a thread; a foreign thread's page shell holds no data and the API says 404", async () => {
  const mine = await newThread("alice", ACME, { workspace_id: seed.workspaces.notes });
  const page = await web(`/o/acme/chat/${mine.id}`, { cookie: sessions.alice });
  assert.equal(page.res.status, 200);
  assert.match(page.html, /data-testid="chat-app"/);
  assert.match(page.html, /data-testid="thread-list"/);
  const fresh = await web("/o/acme/chat", { cookie: sessions.alice });
  assert.match(fresh.html, /data-testid="chat-app"/);
  // bob asks for alice's thread: the API conceals it
  const foreign = await api("bob", "GET", `/v1/orgs/${ACME}/chat/threads/${mine.id}`);
  assert.equal(foreign.status, 404);
  const shell = await web(`/o/acme/chat/${mine.id}`, { cookie: sessions.bob });
  assert.ok(!shell.html.includes(mine.title || "zzzz-no-title") || mine.title === "");
});

test("threads: create, list (own only), get, delete; another user and another org see 404", async () => {
  const t = await newThread("alice");
  assert.deepEqual(Object.keys(t).sort(), ["created_at", "id", "title", "updated_at", "workspace_id"]);
  const list = await api("alice", "GET", `/v1/orgs/${ACME}/chat/threads?limit=100`);
  assert.equal(list.status, 200);
  assert.ok(list.json.items.some((x) => x.id === t.id));
  const bobList = await api("bob", "GET", `/v1/orgs/${ACME}/chat/threads?limit=100`);
  assert.ok(!bobList.json.items.some((x) => x.id === t.id), "threads are private to their creator");
  assert.equal((await api("bob", "GET", `/v1/orgs/${ACME}/chat/threads/${t.id}`)).status, 404);
  assert.equal((await api("bob", "DELETE", `/v1/orgs/${ACME}/chat/threads/${t.id}`)).status, 404);
  assert.equal((await api("alice", "GET", `/v1/orgs/${GLOBEX}/chat/threads/${t.id}`)).status, 404, "a thread id under another org");
  // workspace-bound thread: a workspace the caller cannot read is concealed
  const bad = await api("bob", "POST", `/v1/orgs/${ACME}/chat/threads`, { body: { workspace_id: seed.workspaces.papers } });
  assert.equal(bad.status, 404);
  const detail = await api("alice", "GET", `/v1/orgs/${ACME}/chat/threads/${t.id}`);
  assert.deepEqual(detail.json, { thread: t, messages: [], active_run: null });
  assert.equal((await api("alice", "DELETE", `/v1/orgs/${ACME}/chat/threads/${t.id}`)).status, 204);
  assert.equal((await api("alice", "GET", `/v1/orgs/${ACME}/chat/threads/${t.id}`)).status, 404);
});

test("a run streams through the BFF unbuffered, in order, with ids; completed carries the kept answer; the end frame stops it", async () => {
  const t = await newThread();
  const run = await newRun("alice", t.id, "What happened to revenue?");
  assert.equal(run.state, "queued");
  const { res, sse } = await openRunStream("alice", ACME, run.run_id);
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type"), /^text\/event-stream/);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.equal(res.headers.get("set-cookie"), null);
  assert.equal((await sse.next()).data, "", "first frame is `retry: 3000`");
  const seen = [];
  for (let i = 0; i < 7; i++) {
    await step(run.run_id); // each event must arrive before the next one is triggered: nothing is buffered
    const f = await sse.next();
    assert.match(f.id, /^\d+$/);
    seen.push({ id: Number(f.id), event: f.event, data: JSON.parse(f.data) });
  }
  assert.deepEqual(seen.map((e) => e.event), ["started", "retrieval", "delta", "delta", "citations", "verification", "completed"]);
  assert.deepEqual(seen.map((e) => e.id), [1, 2, 3, 4, 5, 6, 7], "monotonic ids");
  assert.equal(seen[4].data.citations[0].marker, "[1]");
  assert.equal(seen[4].data.citations[0].chunk_id, seed.docs.report.chunks[seed.docs.report.versions[1]][0]);
  assert.equal(seen[6].data.answer_text, "Revenue grew by ten percent [1]. Costs fell [2].");
  const end = await sse.next();
  assert.equal(end.event, "end");
  assert.equal(JSON.parse(end.data).reason, "terminal");
  await sse.cancel();
  assert.equal(await untilStreamsClosed(), 0, "the upstream stream is closed after `end`");
  // the persisted thread now holds the assistant message with citations and verification
  const detail = await api("alice", "GET", `/v1/orgs/${ACME}/chat/threads/${t.id}`);
  assert.equal(detail.json.active_run, null);
  const answer = detail.json.messages.at(-1);
  assert.equal(answer.role, "assistant");
  assert.equal(answer.run_id, run.run_id);
  assert.equal(answer.verification.status, "grounded");
  assert.equal(answer.citations.length, 2);
  const runInfo = await api("alice", "GET", `/v1/orgs/${ACME}/chat/runs/${run.run_id}`);
  assert.equal(runInfo.json.state, "completed");
});

test("SSE accepts the long-lived BFF path: the request's Accept header and Last-Event-ID reach the API", async () => {
  const t = await newThread();
  const run = await newRun("alice", t.id, "headers please");
  await step(run.run_id, 3);
  const { sse } = await openRunStream("alice", ACME, run.run_id, { "last-event-id": "2" });
  await sse.next(); // retry:
  const first = await sse.next();
  assert.equal(first.id, "3", "replay resumes after the id given");
  const logged = (await stubJson("/__log")).filter((e) => e.path.endsWith(`/chat/runs/${run.run_id}/events`)).at(-1);
  assert.equal(logged.last_event_id, "2");
  assert.equal(logged.accept, "text/event-stream");
  await stubJson(`/__chat/drain?run=${run.run_id}`);
  await sse.cancel();
});

test("reconnect midstream: a dropped connection resumes with Last-Event-ID, replays only what was missed, no duplicates", async () => {
  const t = await newThread();
  const run = await newRun("alice", t.id, "reconnect me");
  const got = [];
  const first = await openRunStream("alice", ACME, run.run_id);
  await first.sse.next(); // retry:
  for (let i = 0; i < 3; i++) {
    await step(run.run_id);
    const f = await first.sse.next();
    got.push(Number(f.id));
  }
  assert.deepEqual(got, [1, 2, 3]);
  // network drop; the run keeps going on the server (a closed tab does not cancel it)
  await stubJson(`/__chat/drop?run=${run.run_id}`);
  assert.equal(await first.sse.next().catch(() => null), null, "the first connection ended");
  await step(run.run_id, 2); // events 4 and 5 happen while nobody is connected
  const second = await openRunStream("alice", ACME, run.run_id, { "last-event-id": String(got.at(-1)) });
  await second.sse.next(); // retry:
  const replayed = [await second.sse.next(), await second.sse.next()];
  assert.deepEqual(replayed.map((f) => Number(f.id)), [4, 5], "only events after the last id are replayed");
  assert.equal((await stubJson("/__log")).filter((e) => e.path.endsWith(`/runs/${run.run_id}/events`)).at(-1).last_event_id, "3");
  await stubJson(`/__chat/drain?run=${run.run_id}`);
  assert.deepEqual([(await second.sse.next()).event, (await second.sse.next()).event], ["verification", "completed"]);
  assert.equal((await second.sse.next()).event, "end");
  // a reconnect after completion replays from the log and ends at once
  const third = await openRunStream("alice", ACME, run.run_id, { "last-event-id": "6" });
  await third.sse.next();
  assert.equal((await third.sse.next()).event, "completed");
  assert.equal((await third.sse.next()).event, "end");
  await second.sse.cancel();
  await third.sse.cancel();
  assert.equal(await untilStreamsClosed(), 0);
});

test("an `end: timeout` frame passes through so the client can reconnect", async () => {
  const t = await newThread();
  const run = await newRun("alice", t.id, "timeout me");
  const { sse } = await openRunStream("alice", ACME, run.run_id);
  await sse.next();
  await step(run.run_id, 2);
  await sse.next();
  await sse.next();
  await stubJson(`/__chat/end?run=${run.run_id}&reason=timeout`);
  const end = await sse.next();
  assert.equal(end.event, "end");
  assert.equal(JSON.parse(end.data).reason, "timeout");
  await sse.cancel();
  await stubJson(`/__chat/drain?run=${run.run_id}`);
});

test("closing the browser connection closes the upstream stream (no leak); the run itself continues", async () => {
  const t = await newThread();
  const run = await newRun("alice", t.id, "abort me");
  const ctl = new AbortController();
  const { sse } = await openRunStream("alice", ACME, run.run_id, {}, ctl.signal);
  await sse.next();
  assert.equal((await stubJson("/__chat/state")).streams, 1);
  ctl.abort();
  assert.equal(await untilStreamsClosed(), 0);
  const state = (await stubJson("/__chat/state")).runs[run.run_id];
  assert.equal(state.state, "queued", "disconnecting is not cancelling");
  await stubJson(`/__chat/drain?run=${run.run_id}`);
});

test("idempotent send: the same key returns the same run and message and never starts a second run", async () => {
  const t = await newThread();
  const key = newKey();
  const a = await newRun("alice", t.id, "only once", { key });
  const b = await newRun("alice", t.id, "only once", { key });
  assert.deepEqual(b, { ...a, state: b.state });
  assert.equal(b.run_id, a.run_id);
  assert.equal(b.message_id, a.message_id);
  const runs = Object.values((await stubJson("/__chat/state")).runs).filter((r) => r.thread === t.id);
  assert.equal(runs.length, 1, "exactly one run");
  const detail = await api("alice", "GET", `/v1/orgs/${ACME}/chat/threads/${t.id}`);
  assert.equal(detail.json.messages.filter((m) => m.role === "user").length, 1, "one user message");
  assert.deepEqual(detail.json.active_run, { run_id: a.run_id, state: "queued" }, "an active run is reported so a reload can resume it");
  // a different key is a different run
  const c = await api("alice", "POST", `/v1/orgs/${ACME}/chat/threads/${t.id}/runs`, { body: { content: "second", idempotency_key: newKey() } });
  assert.equal(c.status, 202);
  assert.notEqual(c.json.run_id, a.run_id);
  await stubJson(`/__chat/drain?run=${a.run_id}`);
  await stubJson(`/__chat/drain?run=${c.json.run_id}`);
});

test("run validation and limits: bad key/content are 422, rate limit 429 keeps Retry-After, a third active run is 409", async () => {
  const t = await newThread();
  for (const body of [{ content: "", idempotency_key: newKey() }, { content: "x".repeat(4001), idempotency_key: newKey() }, { content: "hi", idempotency_key: "short" }, { idempotency_key: newKey() }]) {
    const r = await api("alice", "POST", `/v1/orgs/${ACME}/chat/threads/${t.id}/runs`, { body });
    assert.equal(r.status, 422, JSON.stringify(body).slice(0, 50));
  }
  await control("/__chat/mode?mode=rate");
  const limited = await api("alice", "POST", `/v1/orgs/${ACME}/chat/threads/${t.id}/runs`, { body: { content: "hi", idempotency_key: newKey() } });
  assert.equal(limited.status, 429);
  assert.equal(limited.res.headers.get("retry-after"), "5");
  await control("/__chat/mode?mode=ok");
  const state0 = Object.values((await stubJson("/__chat/state")).runs).filter((r) => r.state === "queued" || r.state === "running").length;
  const made = [];
  for (let i = state0; i < 2; i++) made.push(await newRun("alice", t.id, `active ${i}`));
  const third = await api("alice", "POST", `/v1/orgs/${ACME}/chat/threads/${t.id}/runs`, { body: { content: "third", idempotency_key: newKey() } });
  assert.equal(third.status, 409);
  assert.equal(third.json.detail, "too_many_runs");
  for (const id of Object.keys((await stubJson("/__chat/state")).runs)) await stubJson(`/__chat/drain?run=${id}`);
  void made;
});

test("cancel through the BFF: the stream shows `cancelled` then ends; cancelling again is harmless; others cannot cancel", async () => {
  const t = await newThread();
  const run = await newRun("alice", t.id, "cancel me");
  const { sse } = await openRunStream("alice", ACME, run.run_id);
  await sse.next();
  await step(run.run_id, 3);
  for (let i = 0; i < 3; i++) await sse.next();
  assert.equal((await api("bob", "POST", `/v1/orgs/${ACME}/chat/runs/${run.run_id}/cancel`)).status, 404, "bob cannot cancel alice's run");
  const c = await api("alice", "POST", `/v1/orgs/${ACME}/chat/runs/${run.run_id}/cancel`);
  assert.equal(c.status, 202);
  assert.equal(c.json.state, "cancelled");
  const f = await sse.next();
  assert.equal(f.event, "cancelled");
  assert.equal((await sse.next()).event, "end");
  const again = await api("alice", "POST", `/v1/orgs/${ACME}/chat/runs/${run.run_id}/cancel`);
  assert.equal(again.status, 202);
  assert.equal(again.json.state, "cancelled");
  await sse.cancel();
  assert.equal(await untilStreamsClosed(), 0);
  const detail = await api("alice", "GET", `/v1/orgs/${ACME}/chat/threads/${t.id}`);
  assert.equal(detail.json.active_run, null);
  assert.ok(!detail.json.messages.some((m) => m.role === "assistant"), "a cancelled run leaves no answer");
});

test("abstained, partial and failed runs deliver their states (the UI words them; raw upstream text is never relied on)", async () => {
  const t = await newThread();
  const abstain = await newRun("alice", t.id, "ABSTAIN please");
  await stubJson(`/__chat/drain?run=${abstain.run_id}`);
  const part = await newRun("alice", t.id, "PARTIAL please");
  await stubJson(`/__chat/drain?run=${part.run_id}`);
  const fail = await newRun("alice", t.id, "FAIL please");
  await stubJson(`/__chat/drain?run=${fail.run_id}`);
  const detail = (await api("alice", "GET", `/v1/orgs/${ACME}/chat/threads/${t.id}`)).json;
  const byRun = Object.fromEntries(detail.messages.filter((m) => m.role === "assistant").map((m) => [m.run_id, m]));
  assert.equal(byRun[abstain.run_id].verification.status, "abstained");
  assert.deepEqual(byRun[abstain.run_id].citations, []);
  assert.equal(byRun[part.run_id].verification.status, "partial");
  assert.equal(byRun[part.run_id].verification.withheld_count, 1);
  assert.ok(!byRun[part.run_id].content.includes("cheese"), "the unsupported statement is not in the kept answer");
  assert.ok(!byRun[fail.run_id], "a failed run has no answer");
  const { sse } = await openRunStream("alice", ACME, fail.run_id);
  await sse.next();
  const frames = [];
  for (;;) {
    const f = await sse.next();
    if (!f || f.event === "end") break;
    frames.push(f.event);
  }
  assert.deepEqual(frames, ["started", "failed"]);
  const info = await api("alice", "GET", `/v1/orgs/${ACME}/chat/runs/${fail.run_id}`);
  assert.equal(info.json.state, "failed");
  assert.equal(info.json.error_code, "model_unavailable");
  await sse.cancel();
});

test("tenancy: another user cannot stream, read, cancel or start runs on someone else's thread or run", async () => {
  const t = await newThread("alice");
  const run = await newRun("alice", t.id, "private question");
  await step(run.run_id, 3);
  const stream = await openRunStream("bob", ACME, run.run_id);
  assert.equal(stream.res.status, 404);
  await stream.res.text();
  assert.equal((await api("bob", "GET", `/v1/orgs/${ACME}/chat/runs/${run.run_id}`)).status, 404);
  assert.equal((await api("bob", "POST", `/v1/orgs/${ACME}/chat/runs/${run.run_id}/cancel`)).status, 404);
  const post = await api("bob", "POST", `/v1/orgs/${ACME}/chat/threads/${t.id}/runs`, { body: { content: "hijack", idempotency_key: newKey() } });
  assert.equal(post.status, 404);
  assert.ok(!post.text.includes("private question"));
  await stubJson(`/__chat/drain?run=${run.run_id}`); // (the per-user limit is two active runs)
  // the same idempotency key on another thread is a different run (no cross-thread leakage)
  const t2 = await newThread("alice");
  const key = newKey();
  const a = await newRun("alice", t.id, "k", { key });
  const b = await newRun("alice", t2.id, "k", { key });
  assert.notEqual(a.run_id, b.run_id);
  // and through the page layer: bob's chat page for alice's thread carries none of her data
  const page = await web(`/o/acme/chat/${t.id}`, { cookie: sessions.bob });
  assert.ok(!page.html.includes("private question"));
  for (const id of [a.run_id, b.run_id]) await stubJson(`/__chat/drain?run=${id}`);
});

test("chat BFF refusals: POST without a session is 401, with a foreign Origin 403; unsafe paths are rejected", async () => {
  const t = await newThread();
  const nosess = await fetch(`${WEB}/api/v1/orgs/${ACME}/chat/threads/${t.id}/runs`, { method: "POST", headers: { origin: WEB, "content-type": "application/json" }, body: JSON.stringify({ content: "x", idempotency_key: newKey() }) });
  assert.equal(nosess.status, 401);
  await nosess.text();
  const evil = await api("alice", "POST", `/v1/orgs/${ACME}/chat/threads/${t.id}/runs`, { body: { content: "x", idempotency_key: newKey() }, origin: "http://evil.invalid" });
  assert.equal(evil.status, 403);
  const dot = await fetch(`${WEB}/api/v1/orgs/${ACME}/chat/../threads`, { headers: { cookie: `${SESSION_COOKIE}=${sessions.alice}` } });
  assert.notEqual(dot.status, 200);
  await dot.text();
  assert.ok(!(await stubJson("/__log")).some((e) => e.path.includes("..")), "dot segments never reach the API");
});

test("no tokens, session ids or client secrets in any response the web app produced", () => {
  assert.ok(bodies.length > 30);
  for (const b of bodies) {
    assert.doesNotMatch(b, JWT_RE, "no JWT in a response body");
    for (const s of Object.values(sessions)) assert.ok(!b.includes(s), "session cookie value never echoed");
    assert.ok(!b.includes("change-me-oidc-client-secret"));
  }
});
