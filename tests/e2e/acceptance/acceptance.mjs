// M08.03.03 acceptance flows over HTTP against the acceptance stack started by run.sh (own compose project,
// embedding stub + scripted chat stub, no LM Studio). Modelled on tests/e2e/smoke_dev/smoke.mjs.
//   sign-in -> upload -> ready -> search -> grounded chat -> graph -> memory -> research -> CSV -> deletion
// Each flow prints one line "PASS <name>"; the first failure throws and exits non-zero.
//   WEB_URL=http://127.0.0.1:33650 node tests/e2e/acceptance/acceptance.mjs
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";

const WEB = process.env.WEB_URL ?? "http://127.0.0.1:33650";
const USERS = {
  alice: { login: "alice@ukos.test", password: "ukos-dev-alice-password" },
  mallory: { login: "mallory@ukos.test", password: "ukos-dev-mallory-password" },
};
const SESSION = "ukos_session";
const TX = "ukos_auth_tx";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const unescapeHtml = (s) => s.replace(/&amp;/g, "&").replace(/&#43;/g, "+").replace(/&quot;/g, '"');
const cookies = (res) => Object.fromEntries(res.headers.getSetCookie().map((c) => c.split(";")[0].split(/=(.*)/s).slice(0, 2)));

async function web(path, { session, method = "GET", json } = {}) {
  const headers = {};
  if (session) headers.cookie = `${SESSION}=${session}`;
  if (method !== "GET") Object.assign(headers, { origin: WEB, "sec-fetch-site": "same-origin" });
  if (json !== undefined) headers["content-type"] = "application/json";
  const res = await fetch(path.startsWith("http") ? path : WEB + path, {
    method, redirect: "manual", headers, body: json === undefined ? undefined : JSON.stringify(json),
  });
  const text = await res.text();
  let body = null;
  try { body = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status, text, body };
}

async function login(who) {
  const start = await fetch(`${WEB}/api/auth/login?returnTo=%2Fhome`, { redirect: "manual" });
  assert.equal(start.status, 302);
  const tx = cookies(start)[TX];
  let url = start.headers.get("location");
  let html = "";
  for (let i = 0; i < 5; i++) {
    const r = await fetch(url, { redirect: "manual" });
    if (r.status >= 300 && r.status < 400) { url = new URL(r.headers.get("location"), url).href; continue; }
    html = await r.text();
    break;
  }
  const m = html.match(/<form method="post" action="([^"]+)"/);
  assert.ok(m, "Dex login form");
  const action = new URL(unescapeHtml(m[1]), url).href;
  let r = await fetch(action, {
    method: "POST", redirect: "manual", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(USERS[who]).toString(),
  });
  let cur = action;
  for (let i = 0; i < 6 && r.status >= 300 && r.status < 400; i++) {
    const next = new URL(r.headers.get("location"), cur).href;
    if (next.startsWith(`${WEB}/api/auth/callback`)) {
      const cb = await fetch(next, { redirect: "manual", headers: { cookie: `${TX}=${tx}` } });
      const s = cookies(cb)[SESSION];
      assert.ok(s, "session cookie");
      return s;
    }
    cur = next;
    r = await fetch(cur, { redirect: "manual" });
  }
  throw new Error("login failed");
}

async function sse(path, session, { timeoutMs = 120_000 } = {}) {
  const res = await fetch(WEB + path, { headers: { cookie: `${SESSION}=${session}`, accept: "text/event-stream" } });
  assert.equal(res.status, 200, `SSE ${res.status}`);
  const frames = [];
  const dec = new TextDecoder();
  let buf = "";
  const t0 = Date.now();
  for await (const chunk of res.body) {
    buf += dec.decode(chunk, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      const f = { event: "message", data: "" };
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) f.event = line.slice(6).trim();
        else if (line.startsWith("data:")) f.data += line.slice(5).trim();
      }
      if (f.data) { try { f.json = JSON.parse(f.data); } catch { /* ignore */ } }
      if (f.event !== "message" || f.data) frames.push(f);
      if (f.event === "end") return frames;
    }
    if (Date.now() - t0 > timeoutMs) break;
  }
  return frames;
}

const pass = (name, detail = "") => console.log(`PASS ${name}${detail ? ` - ${detail}` : ""}`);
const suffix = Math.random().toString(36).slice(2, 7);
const FACT = "The Kestrel-9 telemetry gateway ships with a seven year limited warranty. It restarts itself every night at 03:15 UTC.";
const TEXT = [FACT, "Unrelated note: the cafeteria menu rotates weekly and includes soup on Tuesdays."].join("\n\n");

const alice = await login("alice");
const mallory = await login("mallory");
assert.equal((await web("/api/v1/orgs")).status, 401, "anonymous request is rejected");
pass("1 sign-in", "alice + mallory; anonymous request 401");

const org = await web("/api/v1/orgs", { session: alice, method: "POST", json: { slug: `accept-${suffix}`, name: `Acceptance ${suffix}` } });
assert.equal(org.status, 201, org.text);
const ws = await web(`/api/v1/orgs/${org.body.id}/workspaces`, { session: alice, method: "POST", json: { slug: "lab", name: "Lab" } });
assert.equal(ws.status, 201, ws.text);
const O = org.body.id, W = ws.body.id, wsPath = `/api/v1/orgs/${O}/workspaces/${W}`;

async function upload(filename, contentType, bytes) {
  const created = await web(`${wsPath}/uploads`, { session: alice, method: "POST", json: {
    filename, content_type: contentType, size_bytes: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") } });
  assert.equal(created.status, 201, created.text);
  const put = await fetch(created.body.upload.url, { method: created.body.upload.method, headers: created.body.upload.headers, body: bytes });
  assert.equal(put.status, 200, "object store PUT");
  const fin = await web(`${wsPath}/uploads/${created.body.upload_id}/finalize`, { session: alice, method: "POST" });
  assert.equal(fin.status, 201, fin.text);
  return fin.body.document.id;
}
async function waitReady(docId, ms = 120_000) {
  const t = Date.now(); let st = "";
  while (Date.now() - t < ms) {
    st = (await web(`${wsPath}/documents/${docId}/ingestion`, { session: alice })).body?.state;
    if (["ready", "failed", "unsupported", "no_text"].includes(st)) break;
    await sleep(1000);
  }
  return st;
}

const docId = await upload("kestrel.txt", "text/plain", Buffer.from(TEXT, "utf8"));
assert.equal(await waitReady(docId), "ready", "document becomes ready");
pass("2 upload -> ready");

const search = await web(`/api/v1/orgs/${O}/search`, { session: alice, method: "POST", json: { query: "Kestrel-9 warranty", limit: 5 } });
assert.equal(search.status, 200, search.text);
const top = search.body.results[0];
assert.ok(top?.snippets?.[0]?.chunk_id, "search finds the document with a chunk id");
const outsiderSearch = await web(`/api/v1/orgs/${O}/search`, { session: mallory, method: "POST", json: { query: "Kestrel-9 warranty" } });
assert.equal(outsiderSearch.status, 404);
pass("3 search", `top="${top.title}"; outsider 404`);

const thread = await web(`/api/v1/orgs/${O}/chat/threads`, { session: alice, method: "POST", json: { workspace_id: W, title: "acceptance" } });
assert.equal(thread.status, 201, thread.text);
const run = await web(`/api/v1/orgs/${O}/chat/threads/${thread.body.id}/runs`, { session: alice, method: "POST", json: {
  content: "How long is the warranty of the Kestrel-9 gateway?", idempotency_key: randomUUID() } });
assert.ok([200, 202].includes(run.status), run.text);
const frames = await sse(`/api/v1/orgs/${O}/chat/runs/${run.body.run_id}/events`, alice);
const done = frames.find((f) => f.event === "completed")?.json;
const citations = frames.find((f) => f.event === "citations")?.json?.citations ?? [];
assert.ok(done, `chat completed (events: ${frames.map((f) => f.event).join(",")})`);
assert.match(done.answer_text.toLowerCase(), /seven|7/);
assert.ok(citations.length >= 1, "answer has a citation");
const c = citations[0];
const ev = await web(`${wsPath}/documents/${c.document_id}/versions/${c.version_id}/chunks/${c.chunk_id}`, { session: alice });
assert.equal(ev.status, 200);
assert.match(ev.body.text, /Kestrel-9/);
assert.equal((await web(`/api/v1/orgs/${O}/chat/runs/${run.body.run_id}`, { session: mallory })).status, 404);
pass("4 grounded chat", `${citations.length} citation(s); evidence chunk readable; outsider 404`);

const gtext = "Priya Raman works for Northwind Labs. Northwind Labs owns the Kestrel-9 project.";
await upload("org-chart.txt", "text/plain", Buffer.from(gtext, "utf8"));
let ent = null;
for (let t = Date.now(); Date.now() - t < 120_000 && !ent; await sleep(2000)) {
  const e = await web(`${wsPath}/graph/entities?q=northwind&limit=5`, { session: alice });
  if (e.status === 200 && e.body.items?.length) ent = e.body.items[0];
}
assert.ok(ent, "graph extraction produced an entity (GRAPH_ENABLED and scripted extraction reply)");
const nb = await web(`${wsPath}/graph/neighborhood?entity_id=${ent.id}&depth=1&limit=30`, { session: alice });
assert.equal(nb.status, 200, nb.text);
assert.ok(nb.body.edges.length >= 1, "at least one verified edge");
const cl = await web(`${wsPath}/graph/claims/${nb.body.edges[0].claim_id}`, { session: alice });
assert.equal(cl.status, 200, cl.text);
assert.ok(cl.body.evidence.length >= 1, "claim has evidence");
assert.equal((await web(`${wsPath}/graph/neighborhood?entity_id=${ent.id}`, { session: mallory })).status, 404);
pass("5 graph", `${ent.name}; ${nb.body.nodes.length} nodes, ${nb.body.edges.length} edges; claim evidence; outsider 404`);

const evid = { chunk_id: top.snippets[0].chunk_id, document_id: top.document_id, version_id: top.version_id };
const sug = await web(`/api/v1/orgs/${O}/memory/suggestions`, { session: alice, method: "POST", json: {
  statement: "The Kestrel-9 gateway has a seven year limited warranty.", kind: "fact", workspace_id: W, evidence: [evid] } });
assert.equal(sug.status, 201, sug.text);
assert.equal(sug.body.state, "suggested");
const pending = await web(`/api/v1/orgs/${O}/memory?state=approved`, { session: alice });
assert.ok(!pending.body.items.some((m) => m.id === sug.body.id), "a suggestion is not an approved memory");
assert.equal((await web(`/api/v1/orgs/${O}/memory/${sug.body.id}`, { session: mallory })).status, 404);
const appr = await web(`/api/v1/orgs/${O}/memory/${sug.body.id}/approve`, { session: alice, method: "POST" });
assert.equal(appr.body?.state, "approved");
pass("6 memory", "suggested -> approved; outsider 404");

const rs = await web(`/api/v1/orgs/${O}/research`, { session: alice, method: "POST", json: {
  question: "What do the documents say about the Kestrel-9 gateway?", workspace_ids: [W] } });
assert.equal(rs.status, 202, rs.text);
let rd = null;
for (let t = Date.now(); Date.now() - t < 180_000; await sleep(2000)) {
  rd = (await web(`/api/v1/orgs/${O}/research/${rs.body.id}`, { session: alice })).body;
  if (["completed", "failed", "cancelled"].includes(rd?.state)) break;
}
assert.equal(rd?.state, "completed", `research completed (${rd?.state}, ${rd?.error_code ?? "-"})`);
const stmts = rd.report?.sections?.flatMap((s) => s.statements) ?? [];
assert.ok(stmts.length >= 1 && stmts.every((s) => s.citations?.length >= 1), "every statement is cited");
const exp = await web(`/api/v1/orgs/${O}/research/${rs.body.id}/export?format=md`, { session: alice });
assert.equal(exp.status, 200);
assert.equal((await web(`/api/v1/orgs/${O}/research/${rs.body.id}`, { session: mallory })).status, 404);
pass("7 research", `${stmts.length} cited statement(s); markdown export; outsider 404`);

const csvId = await upload("sales.csv", "text/csv", Buffer.from("product,region,units\nKestrel-9,EMEA,120\nAurora,EMEA,40\n", "utf8"));
assert.equal(await waitReady(csvId), "ready", "csv becomes ready");
const cs = await web(`/api/v1/orgs/${O}/search`, { session: alice, method: "POST", json: { query: "Kestrel-9 EMEA units" } });
assert.ok(cs.body?.results?.some((r) => r.document_id === csvId), "csv content searchable");
pass("8 CSV format path (no connector credentials, no internet)");

const denied = await web(`${wsPath}/documents/${top.document_id}`, { session: mallory, method: "DELETE" });
assert.ok([403, 404].includes(denied.status), `outsider delete denied (got ${denied.status})`);
assert.equal((await web(`${wsPath}/documents/${top.document_id}`, { session: alice })).status, 200, "document survived the denied delete");
const del = await web(`${wsPath}/documents/${top.document_id}`, { session: alice, method: "DELETE" });
assert.ok([200, 202, 204].includes(del.status), `owner delete (got ${del.status})`);
let inv = null;
for (let i = 0; i < 30; i++) {
  inv = (await web(`/api/v1/orgs/${O}/memory/${sug.body.id}`, { session: alice })).body;
  if (inv?.state === "invalidated") break;
  await sleep(1500);
}
assert.equal(inv?.state, "invalidated", "memory backed by the deleted document is invalidated");
pass("9 deletion", `outsider ${denied.status}; owner ${del.status}; memory invalidated (${inv.invalidated_reason ?? "-"})`);

console.log("\nACCEPTANCE PASSED (9 flows)");
