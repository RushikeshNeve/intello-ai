// Lean end-to-end smoke (M04): sign in -> org/workspace -> upload -> ingestion ready -> search -> grounded chat
// (real worker + real LM Studio) -> evidence chunk -> another user cannot see the thread.
// Runs against a RUNNING stack (default: the dev stack at http://127.0.0.1:3300). Creates ONE new org "M04 smoke ...".
//   node tests/e2e/smoke_dev/smoke.mjs        (env: WEB_URL)
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";

const WEB = process.env.WEB_URL ?? "http://127.0.0.1:3300";
const USERS = {
  alice: { login: "alice@ukos.test", password: "ukos-dev-alice-password" },
  mallory: { login: "mallory@ukos.test", password: "ukos-dev-mallory-password" },
};
const SESSION = "ukos_session";
const TX = "ukos_auth_tx";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const unescapeHtml = (s) => s.replace(/&amp;/g, "&").replace(/&#43;/g, "+").replace(/&quot;/g, '"');
const cookies = (res) => Object.fromEntries(res.headers.getSetCookie().map((c) => c.split(";")[0].split(/=(.*)/s).slice(0, 2)));

async function web(path, { session, method = "GET", json, extra = {} } = {}) {
  const headers = { ...extra };
  const c = { ...(session ? { [SESSION]: session } : {}) };
  const ch = Object.entries(c).map(([k, v]) => `${k}=${v}`).join("; ");
  if (ch) headers.cookie = ch;
  if (method !== "GET") Object.assign(headers, { origin: WEB, "sec-fetch-site": "same-origin" });
  if (json !== undefined) headers["content-type"] = "application/json";
  const res = await fetch(path.startsWith("http") ? path : WEB + path, {
    method, redirect: "manual", headers, body: json === undefined ? undefined : JSON.stringify(json),
  });
  const text = await res.text();
  let body = null;
  try { body = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status, text, body, res };
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

async function sse(path, session, { timeoutMs = 240_000 } = {}) {
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
      const f = { event: "message", data: "", id: null };
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) f.event = line.slice(6).trim();
        else if (line.startsWith("data:")) f.data += line.slice(5).trim();
        else if (line.startsWith("id:")) f.id = Number(line.slice(3));
      }
      if (f.data) { try { f.json = JSON.parse(f.data); } catch { /* ignore */ } }
      if (f.event !== "message" || f.data) frames.push(f);
      if (f.event === "end") return frames;
    }
    if (Date.now() - t0 > timeoutMs) break;
  }
  return frames;
}

const out = (k, v) => console.log(`${k.padEnd(26)} ${v}`);
const suffix = Math.random().toString(36).slice(2, 7);
const FACT = `The Kestrel-9 telemetry gateway ships with a seven year limited warranty. It restarts itself every night at 03:15 UTC. The maintenance contact for Kestrel-9 is Priya Raman in the Lisbon office.`;
const TEXT = [FACT, "Unrelated note: the cafeteria menu rotates weekly and includes soup on Tuesdays.", "Another unrelated note: parking permits are renewed each April."].join("\n\n");

const alice = await login("alice");
const mallory = await login("mallory");
out("sign-in", "alice + mallory OK");

const org = await web("/api/v1/orgs", { session: alice, method: "POST", json: { slug: `m04-smoke-${suffix}`, name: `M04 smoke ${suffix}` } });
assert.equal(org.status, 201, org.text);
const ws = await web(`/api/v1/orgs/${org.body.id}/workspaces`, { session: alice, method: "POST", json: { slug: "lab", name: "Lab" } });
assert.equal(ws.status, 201, ws.text);
const O = org.body.id, W = ws.body.id, wsPath = `/api/v1/orgs/${O}/workspaces/${W}`;

const bytes = Buffer.from(TEXT, "utf8");
const created = await web(`${wsPath}/uploads`, { session: alice, method: "POST", json: {
  filename: "kestrel.txt", content_type: "text/plain", size_bytes: bytes.length,
  sha256: createHash("sha256").update(bytes).digest("hex") } });
assert.equal(created.status, 201, created.text);
const put = await fetch(created.body.upload.url, { method: created.body.upload.method, headers: created.body.upload.headers, body: bytes });
assert.equal(put.status, 200, "object store PUT");
const fin = await web(`${wsPath}/uploads/${created.body.upload_id}/finalize`, { session: alice, method: "POST" });
assert.equal(fin.status, 201, fin.text);
const docId = fin.body.document.id;
out("upload", "finalized");

let state = "";
const t0 = Date.now();
while (Date.now() - t0 < 120_000) {
  const s = await web(`${wsPath}/documents/${docId}/ingestion`, { session: alice });
  state = s.body?.state;
  if (["ready", "failed", "unsupported", "no_text"].includes(state)) break;
  await sleep(1000);
}
out("ingestion", `${state} in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
assert.equal(state, "ready", "document must become ready (is LM Studio serving the embedding model?)");

const search = await web(`/api/v1/orgs/${O}/search`, { session: alice, method: "POST", json: { query: "Kestrel-9 warranty", limit: 5 } });
assert.equal(search.status, 200, search.text);
const top = search.body.results[0];
out("search", `${search.body.results.length} result(s), degraded=${JSON.stringify(search.body.degraded)}, top="${top?.title}", chunk_id=${top?.snippets?.[0]?.chunk_id ? "yes" : "NO"}`);
assert.ok(top, "search finds the document");

const outsiderSearch = await web(`/api/v1/orgs/${O}/search`, { session: mallory, method: "POST", json: { query: "Kestrel-9 warranty" } });
out("outsider search", String(outsiderSearch.status));
assert.equal(outsiderSearch.status, 404);

const thread = await web(`/api/v1/orgs/${O}/chat/threads`, { session: alice, method: "POST", json: { workspace_id: W, title: "smoke" } });
assert.equal(thread.status, 201, thread.text);
const run = await web(`/api/v1/orgs/${O}/chat/threads/${thread.body.id}/runs`, { session: alice, method: "POST", json: {
  content: "How long is the warranty of the Kestrel-9 gateway?", idempotency_key: randomUUID() } });
assert.ok([200, 202].includes(run.status), run.text);
const tc = Date.now();
const frames = await sse(`/api/v1/orgs/${O}/chat/runs/${run.body.run_id}/events`, alice);
const types = frames.map((f) => f.event);
const done = frames.find((f) => f.event === "completed")?.json;
const failed = frames.find((f) => f.event === "failed")?.json;
const verification = frames.find((f) => f.event === "verification")?.json;
const citations = frames.find((f) => f.event === "citations")?.json?.citations ?? [];
out("chat events", types.filter((t, i) => t !== "delta" || types[i - 1] !== "delta").join(" > "));
out("chat time", `${((Date.now() - tc) / 1000).toFixed(1)}s, deltas=${types.filter((t) => t === "delta").length}`);
if (failed) out("chat FAILED", JSON.stringify(failed));
assert.ok(done, "run completed (is LM Studio serving the chat model google/gemma-3-4b?)");
out("answer", JSON.stringify(done.answer_text));
out("verification", JSON.stringify(verification));
out("citations", `${citations.length}`);
assert.match(done.answer_text.toLowerCase(), /seven|7/, "answer mentions the warranty");
assert.ok(citations.length >= 1, "answer has a citation");

const c = citations[0];
const ev = await web(`${wsPath}/documents/${c.document_id}/versions/${c.version_id}/chunks/${c.chunk_id}`, { session: alice });
out("evidence chunk", `${ev.status}, contains fact=${/Kestrel-9/.test(ev.body?.text ?? "")}`);
assert.equal(ev.status, 200);

const peek = await web(`/api/v1/orgs/${O}/chat/runs/${run.body.run_id}`, { session: mallory });
out("outsider run read", String(peek.status));
assert.equal(peek.status, 404);

// no-evidence question abstains without inventing an answer
const run2 = await web(`/api/v1/orgs/${O}/chat/threads/${thread.body.id}/runs`, { session: alice, method: "POST", json: {
  content: "What is the capital of Australia?", idempotency_key: randomUUID() } });
const f2 = await sse(`/api/v1/orgs/${O}/chat/runs/${run2.body.run_id}/events`, alice);
const v2 = f2.find((f) => f.event === "verification")?.json;
const d2 = f2.find((f) => f.event === "completed")?.json;
out("off-topic question", `${v2?.status ?? "n/a"} :: ${JSON.stringify(d2?.answer_text ?? "").slice(0, 90)}`);
if (process.env.GRAPH === "1") {
  // M05: relationship document -> async graph extraction (local LLM) -> entities/neighborhood/claim -> graph chat
  const gtext = "Priya Raman works for Northwind Labs. Northwind Labs owns the Kestrel-9 project. The Kestrel-9 project depends on the Aurora scheduler. Marcus Webb is a member of Northwind Labs.";
  const gb = Buffer.from(gtext, "utf8");
  const gc = await web(`${wsPath}/uploads`, { session: alice, method: "POST", json: { filename: "org-chart.txt", content_type: "text/plain", size_bytes: gb.length, sha256: createHash("sha256").update(gb).digest("hex") } });
  const gp = await fetch(gc.body.upload.url, { method: gc.body.upload.method, headers: gc.body.upload.headers, body: gb });
  assert.equal(gp.status, 200);
  const gf = await web(`${wsPath}/uploads/${gc.body.upload_id}/finalize`, { session: alice, method: "POST" });
  assert.equal(gf.status, 201, gf.text);
  const tg = Date.now();
  let ent = null;
  while (Date.now() - tg < 180_000) {
    const e = await web(`${wsPath}/graph/entities?q=northwind&limit=5`, { session: alice });
    if (e.status === 200 && e.body.items?.length) { ent = e.body.items[0]; break; }
    await sleep(3000);
  }
  out("graph entities", ent ? `${ent.name} (${ent.type}) after ${((Date.now() - tg) / 1000).toFixed(0)}s` : "NONE");
  assert.ok(ent, "graph extraction produced an entity (GRAPH_ENABLED=true and gemma serving?)");
  const nb = await web(`${wsPath}/graph/neighborhood?entity_id=${ent.id}&depth=1&limit=30`, { session: alice });
  assert.equal(nb.status, 200, nb.text);
  out("neighborhood", `${nb.body.nodes.length} nodes, ${nb.body.edges.length} edges, truncated=${nb.body.truncated}`);
  assert.ok(nb.body.edges.length >= 1, "at least one verified edge");
  const cl = await web(`${wsPath}/graph/claims/${nb.body.edges[0].claim_id}`, { session: alice });
  assert.equal(cl.status, 200, cl.text);
  out("claim evidence", `${cl.body.claim.predicate}, ${cl.body.evidence.length} evidence item(s)`);
  assert.ok(cl.body.evidence.length >= 1);
  const mo = await web(`${wsPath}/graph/neighborhood?entity_id=${ent.id}`, { session: mallory });
  out("outsider graph", String(mo.status));
  assert.equal(mo.status, 404);
  const r3 = await web(`/api/v1/orgs/${O}/chat/threads/${thread.body.id}/runs`, { session: alice, method: "POST", json: { content: "Who works for the organization that owns Kestrel-9?", idempotency_key: randomUUID() } });
  const f3 = await sse(`/api/v1/orgs/${O}/chat/runs/${r3.body.run_id}/events`, alice);
  const d3 = f3.find((f) => f.event === "completed")?.json;
  const v3 = f3.find((f) => f.event === "verification")?.json;
  out("graph chat answer", `${v3?.status} :: ${JSON.stringify(d3?.answer_text ?? "").slice(0, 160)}`);
}
if (process.env.M06 === "1") {
  // M06: CSV through the pipeline, safe URL capture, GitHub source sync + revoke (needs outbound internet)
  const csv = Buffer.from("product,region,units\nKestrel-9,EMEA,120\nKestrel-9,APAC,75\nAurora,EMEA,40\n", "utf8");
  const cc = await web(`${wsPath}/uploads`, { session: alice, method: "POST", json: { filename: "sales.csv", content_type: "text/csv", size_bytes: csv.length, sha256: createHash("sha256").update(csv).digest("hex") } });
  assert.equal(cc.status, 201, cc.text);
  assert.equal((await fetch(cc.body.upload.url, { method: cc.body.upload.method, headers: cc.body.upload.headers, body: csv })).status, 200);
  const cf = await web(`${wsPath}/uploads/${cc.body.upload_id}/finalize`, { session: alice, method: "POST" });
  assert.equal(cf.status, 201, cf.text);
  const waitReady = async (docId, ms = 120_000) => {
    const t = Date.now(); let st = "";
    while (Date.now() - t < ms) { st = (await web(`${wsPath}/documents/${docId}/ingestion`, { session: alice })).body?.state; if (["ready", "failed", "unsupported", "no_text"].includes(st)) break; await sleep(1500); }
    return st;
  };
  out("csv ingestion", await waitReady(cf.body.document.id));
  const cs = await web(`/api/v1/orgs/${O}/search`, { session: alice, method: "POST", json: { query: "Kestrel-9 EMEA units" } });
  const csvHit = cs.body?.results?.find((r) => r.document_id === cf.body.document.id);
  out("csv search", csvHit ? `hit "${csvHit.title}"` : "NO HIT");
  assert.ok(csvHit, "csv content searchable");

  const bad = await web(`${wsPath}/documents/from-url`, { session: alice, method: "POST", json: { url: "http://169.254.169.254/latest/meta-data/" } });
  out("url capture (blocked)", `${bad.status} ${bad.body?.detail ?? ""}`);
  assert.equal(bad.status, 422);
  const good = await web(`${wsPath}/documents/from-url`, { session: alice, method: "POST", json: { url: "https://example.com/", title: "Example page" } });
  out("url capture (public)", `${good.status} ${good.status === 201 ? "document created" : good.text.slice(0, 80)}`);
  if (good.status === 201) out("url doc ingestion", await waitReady(good.body.document_id));

  const src = await web(`${wsPath}/sources`, { session: alice, method: "POST", json: { provider: "github", repo: "octocat/Hello-World" } });
  out("github source", `${src.status} ${src.body?.state ?? src.text.slice(0, 80)}`);
  assert.equal(src.status, 201, src.text);
  const syncRes = await web(`${wsPath}/sources/${src.body.id}/sync`, { session: alice, method: "POST" });
  assert.equal(syncRes.status, 202, syncRes.text);
  let detail = null; const ts = Date.now();
  while (Date.now() - ts < 120_000) {
    detail = (await web(`${wsPath}/sources/${src.body.id}`, { session: alice })).body;
    if (detail?.last_sync && ["succeeded", "failed"].includes(detail.last_sync.state)) break;
    await sleep(2000);
  }
  out("github sync", `${detail?.last_sync?.state} items=${JSON.stringify(detail?.items)} err=${detail?.last_sync?.error_code ?? "-"}`);
  assert.equal(detail?.last_sync?.state, "succeeded", "GitHub sync succeeded (internet access? rate limit?)");
  assert.ok(detail.items.active >= 1, "synced at least one file");
  const outsiderSrc = await web(`${wsPath}/sources/${src.body.id}`, { session: mallory });
  out("outsider source read", String(outsiderSrc.status));
  assert.equal(outsiderSrc.status, 404);
  const rev = await web(`${wsPath}/sources/${src.body.id}/revoke`, { session: alice, method: "POST" });
  out("revoke", String(rev.status));
  assert.ok([200, 202, 204].includes(rev.status), rev.text);
  const after = await web(`${wsPath}/sources/${src.body.id}`, { session: alice });
  out("source after revoke", `${after.body?.state}, has_token=${after.body?.has_token}`);
  assert.equal(after.body?.state, "revoked");
}
console.log("\nSMOKE PASSED");
