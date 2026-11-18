// M02 gate: real web (BFF) + API + Dex + PostgreSQL + RustFS, scripted over HTTP like a browser.
// API calls go through the web BFF (cookie session); file bytes go DIRECTLY to the object store through the
// presigned URL, exactly as the upload component does. Run through ./run.sh.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execSync } from "node:child_process";
import { createHash, randomBytes, randomUUID } from "node:crypto";

const WEB = process.env.WEB_URL ?? "http://127.0.0.1:33500";
const STORE = process.env.STORE_URL ?? "http://127.0.0.1:39500";
const MAX_BYTES = Number(process.env.UPLOAD_MAX_BYTES ?? 5242880);
const SESSION_COOKIE = "ukos_session";
const TX_COOKIE = "ukos_auth_tx";
const JWT_RE = /eyJ[\w-]+\.[\w-]+\.[\w-]+/;
const USERS = {
  alice: { login: "alice@ukos.test", password: "ukos-dev-alice-password" },
  bob: { login: "bob@ukos.test", password: "ukos-dev-bob-password" },
  mallory: { login: "mallory@ukos.test", password: "ukos-dev-mallory-password" },
};
const transcript = []; // everything the web app returned to the "browser"

// ---------------------------------------------------------------------------------------------
// HTTP helpers (browser-like)
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

async function web(path, { session, method = "GET", json, extraCookies = {} } = {}) {
  const cookies = { ...extraCookies, ...(session ? { [SESSION_COOKIE]: session } : {}) };
  const headers = {};
  const cookieHeader = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join("; ");
  if (cookieHeader) headers.cookie = cookieHeader;
  if (method !== "GET") Object.assign(headers, { origin: WEB, "sec-fetch-site": "same-origin" });
  if (json !== undefined) headers["content-type"] = "application/json";
  const res = await fetch(path.startsWith("http") ? path : WEB + path, {
    method, redirect: "manual", headers, body: json === undefined ? undefined : JSON.stringify(json),
  });
  const text = await res.text();
  transcript.push({ path, status: res.status, headers: [...res.headers.entries()], body: text });
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

function sql(query) {
  const cmd = `${process.env.PG_EXEC} psql -U ukos_admin -d ukos -Atc "${query}"`;
  return execSync(cmd, { encoding: "utf8" }).trim();
}

// ---------------------------------------------------------------------------------------------
// Upload helpers: exactly what the browser-side engine does
// ---------------------------------------------------------------------------------------------
const sha256hex = (bytes) => createHash("sha256").update(bytes).digest("hex");

async function createSession(session, wsPath, { filename, contentType, bytes, documentId, title, sha256, size }) {
  return web(`${wsPath}/uploads`, {
    session, method: "POST",
    json: {
      filename, content_type: contentType, size_bytes: size ?? bytes.length, sha256: sha256 ?? sha256hex(bytes),
      ...(documentId ? { document_id: documentId } : {}), ...(title ? { title } : {}),
    },
  });
}

async function putToStore(upload, bytes) {
  // Direct to the object store, with EXACTLY the headers the API returned and no cookies or auth.
  return fetch(upload.url, { method: upload.method, headers: upload.headers, body: bytes });
}

async function uploadFile(session, wsPath, spec) {
  const created = await createSession(session, wsPath, spec);
  assert.equal(created.status, 201, `create session: ${created.text}`);
  const put = await putToStore(created.body.upload, spec.bytes);
  assert.equal(put.status, 200, `PUT to the object store: ${await put.text()}`);
  const fin = await web(`${wsPath}/uploads/${created.body.upload_id}/finalize`, { session, method: "POST" });
  return { created, put, fin };
}

const suffix = randomBytes(3).toString("hex");
const ORG = { slug: `acme-${suffix}`, name: `Acme ${suffix}` };
const WS = { slug: `lab-${suffix}`, name: `Lab ${suffix}` };
const state = {};
const TEXT = Buffer.from(`Intello gate document ${suffix}\n` + "line of text\n".repeat(100));
const HTML = Buffer.from("<html><body><script>alert('xss')</script></body></html>");

// ---------------------------------------------------------------------------------------------
// Scenario
// ---------------------------------------------------------------------------------------------
test("setup: users sign in; alice builds an org + workspace; bob is a workspace viewer", async () => {
  state.alice = await login("alice");
  state.bob = await login("bob");
  state.mallory = await login("mallory");
  state.bobId = (await web("/api/v1/me", { session: state.bob })).body.id;
  state.malloryId = (await web("/api/v1/me", { session: state.mallory })).body.id;
  assert.ok((await web("/api/v1/me", { session: state.alice })).body.id);

  const org = await web("/api/v1/orgs", { session: state.alice, method: "POST", json: ORG });
  assert.equal(org.status, 201, org.text);
  state.orgId = org.body.id;
  const ws = await web(`/api/v1/orgs/${state.orgId}/workspaces`, { session: state.alice, method: "POST", json: WS });
  assert.equal(ws.status, 201, ws.text);
  state.wsId = ws.body.id;
  state.wsPath = `/api/v1/orgs/${state.orgId}/workspaces/${state.wsId}`;
  assert.equal((await web(`/api/v1/orgs/${state.orgId}/members/${state.bobId}`, { session: state.alice, method: "PUT", json: { role: "member" } })).status, 200);
  assert.equal((await web(`${state.wsPath}/members/${state.bobId}`, { session: state.alice, method: "PUT", json: { role: "viewer" } })).status, 200);
});

test("browser-style upload: CORS, direct PUT to RustFS with the signed headers, finalize, list", async () => {
  const created = await createSession(state.alice, state.wsPath, { filename: "notes.txt", contentType: "text/plain", bytes: TEXT, title: "Team notes" });
  assert.equal(created.status, 201, created.text);
  const { upload, upload_id: uploadId, document_id: documentId } = created.body;
  assert.ok(upload.url.startsWith(`${STORE}/`), "the URL points at the object store, not the app");
  assert.ok(!upload.url.includes("notes.txt"), "the filename never reaches the object key");
  assert.deepEqual(Object.keys(upload.headers).map((h) => h.toLowerCase()).sort(), ["content-type", "x-amz-checksum-sha256"]);

  // Bucket CORS (applied by the init job from API_CORS_ORIGINS): the web origin may PUT, an unknown origin may not.
  const pre = await fetch(upload.url, { method: "OPTIONS", headers: {
    origin: WEB, "access-control-request-method": "PUT", "access-control-request-headers": "content-type,x-amz-checksum-sha256" } });
  assert.ok(pre.status < 300, `preflight from the web origin (${pre.status})`);
  assert.equal(pre.headers.get("access-control-allow-origin"), WEB);
  const evil = await fetch(upload.url, { method: "OPTIONS", headers: { origin: "http://evil.example", "access-control-request-method": "PUT" } });
  assert.notEqual(evil.headers.get("access-control-allow-origin"), "http://evil.example");
  assert.notEqual(evil.headers.get("access-control-allow-origin"), "*");

  const put = await putToStore(upload, TEXT);
  assert.equal(put.status, 200, await put.text());
  const fin = await web(`${state.wsPath}/uploads/${uploadId}/finalize`, { session: state.alice, method: "POST" });
  assert.equal(fin.status, 201, fin.text);
  assert.equal(fin.body.document.id, documentId);
  assert.equal(fin.body.document.title, "Team notes");
  assert.equal(fin.body.version.version_number, 1);
  assert.equal(fin.body.version.sha256, sha256hex(TEXT));
  assert.equal(fin.body.version.retention.status, "current");
  state.docId = documentId;
  state.v1 = fin.body.version.id;

  const again = await web(`${state.wsPath}/uploads/${uploadId}/finalize`, { session: state.alice, method: "POST" });
  assert.equal(again.status, 200, "finalize is idempotent");
  assert.equal(again.body.version.id, state.v1);

  const list = await web(`${state.wsPath}/documents`, { session: state.alice });
  assert.equal(list.status, 200);
  assert.equal(list.body.can_upload, true);
  const row = list.body.items.find((d) => d.id === state.docId);
  assert.ok(row && row.version_count === 1 && row.permissions.manage === true);
});

test("download: exact bytes through the signed URL; html is forced to attachment, text may be inline", async () => {
  const inline = await web(`${state.wsPath}/documents/${state.docId}/versions/${state.v1}/content-url?disposition=inline`, { session: state.alice });
  assert.equal(inline.status, 200, inline.text);
  assert.equal(inline.body.disposition, "inline");
  const got = await fetch(inline.body.url);
  assert.equal(got.status, 200);
  assert.deepEqual(Buffer.from(await got.arrayBuffer()), TEXT, "downloaded bytes equal the uploaded bytes");
  assert.match(got.headers.get("content-disposition") ?? "", /^inline/);

  const up = await uploadFile(state.alice, state.wsPath, { filename: "page.html", contentType: "text/html", bytes: HTML });
  assert.equal(up.fin.status, 201, up.fin.text);
  state.htmlDoc = up.fin.body.document.id;
  const cu = await web(`${state.wsPath}/documents/${state.htmlDoc}/versions/${up.fin.body.version.id}/content-url?disposition=inline`, { session: state.alice });
  assert.equal(cu.body.disposition, "attachment", "html is never served inline");
  const res = await fetch(cu.body.url);
  assert.match(res.headers.get("content-disposition") ?? "", /^attachment/);
  assert.deepEqual(Buffer.from(await res.arrayBuffer()), HTML);
});

test("versions: a new upload adds version 2, never overwrites version 1, and never renames", async () => {
  const v2 = Buffer.from("second version " + suffix);
  const up = await uploadFile(state.alice, state.wsPath, { filename: "notes-v2.txt", contentType: "text/plain", bytes: v2, documentId: state.docId, title: "Ignored" });
  assert.equal(up.fin.status, 201, up.fin.text);
  assert.equal(up.fin.body.version.version_number, 2);
  assert.equal(up.fin.body.document.title, "Team notes", "an upload never renames the document");
  assert.equal(up.fin.body.document.version_count, 2);
  state.v2 = up.fin.body.version.id;

  const timeline = await web(`${state.wsPath}/documents/${state.docId}/versions`, { session: state.alice });
  assert.deepEqual(timeline.body.items.map((v) => [v.version_number, v.retention.status]), [[2, "current"], [1, "superseded"]]);
  // Version 1 still downloads the ORIGINAL bytes.
  const cu = await web(`${state.wsPath}/documents/${state.docId}/versions/${state.v1}/content-url`, { session: state.alice });
  const old = await fetch(cu.body.url);
  assert.deepEqual(Buffer.from(await old.arrayBuffer()), TEXT);
});

test("integrity: a tampered upload is rejected by the store; finalize reports the file missing, then a correct PUT succeeds", async () => {
  const good = Buffer.from("the real content " + suffix);
  const bad = Buffer.from("the REAL content " + suffix); // same length, different bytes
  const created = await createSession(state.alice, state.wsPath, { filename: "tamper.bin", contentType: "application/octet-stream", bytes: good });
  assert.equal(created.status, 201, created.text);
  const rejected = await putToStore(created.body.upload, bad);
  assert.ok(rejected.status >= 400, `the object store must reject mismatching bytes (${rejected.status})`);
  const fin1 = await web(`${state.wsPath}/uploads/${created.body.upload_id}/finalize`, { session: state.alice, method: "POST" });
  assert.equal(fin1.status, 409);
  assert.equal(fin1.body.detail, "object_missing");
  assert.equal((await putToStore(created.body.upload, good)).status, 200);
  const fin2 = await web(`${state.wsPath}/uploads/${created.body.upload_id}/finalize`, { session: state.alice, method: "POST" });
  assert.equal(fin2.status, 201, fin2.text);
  assert.equal(fin2.body.version.sha256, sha256hex(good));
});

test("limits and authorization on uploads", async () => {
  const small = Buffer.from("x");
  const tooBig = await createSession(state.alice, state.wsPath, { filename: "huge.bin", contentType: "application/octet-stream", bytes: small, size: MAX_BYTES + 1 });
  assert.equal(tooBig.status, 413);
  assert.equal(tooBig.body.detail, "file_too_large");
  const badSha = await createSession(state.alice, state.wsPath, { filename: "x.bin", contentType: "application/octet-stream", bytes: small, sha256: "nope" });
  assert.equal(badSha.status, 422);
  const viewer = await createSession(state.bob, state.wsPath, { filename: "x.txt", contentType: "text/plain", bytes: small });
  assert.equal(viewer.status, 403, "a workspace viewer cannot upload");
  const outsider = await createSession(state.mallory, state.wsPath, { filename: "x.txt", contentType: "text/plain", bytes: small });
  assert.equal(outsider.status, 404, "an outsider learns nothing");
  const fin = await web(`${state.wsPath}/uploads/${randomUUID()}/finalize`, { session: state.mallory, method: "POST" });
  assert.equal(fin.status, 404);
});

test("org storage quota: pending sessions count, so it can't be bypassed without uploading", async () => {
  const org2 = await web("/api/v1/orgs", { session: state.alice, method: "POST", json: { slug: `quota-${suffix}`, name: `Quota ${suffix}` } });
  assert.equal(org2.status, 201, org2.text);
  const ws2 = await web(`/api/v1/orgs/${org2.body.id}/workspaces`, { session: state.alice, method: "POST", json: { slug: "q", name: "Q" } });
  const path2 = `/api/v1/orgs/${org2.body.id}/workspaces/${ws2.body.id}`;
  const statuses = [];
  for (let i = 0; i < 6; i++) {
    const r = await createSession(state.alice, path2, { filename: `q${i}.bin`, contentType: "application/octet-stream", bytes: Buffer.from("q"), size: MAX_BYTES, sha256: sha256hex(Buffer.from(`q${i}`)) });
    statuses.push(r.status);
    if (r.status !== 201) { assert.equal(r.body.detail, "quota_exceeded"); assert.equal(r.status, 409); break; }
  }
  assert.ok(statuses.includes(409), `the 20 MiB quota must be hit by 5 MiB sessions (got ${statuses.join(",")})`);
  assert.ok(statuses.filter((s) => s === 201).length <= 4);
});

test("permissions: a viewer reads and downloads but cannot edit or delete; an outsider sees nothing", async () => {
  const docPath = `${state.wsPath}/documents/${state.docId}`;
  assert.equal((await web(docPath, { session: state.bob })).status, 200);
  const list = await web(`${state.wsPath}/documents`, { session: state.bob });
  assert.equal(list.body.can_upload, false);
  assert.ok(list.body.items.every((d) => d.permissions.write === false && d.permissions.manage === false));
  assert.equal((await web(`${docPath}/versions/${state.v1}/content-url`, { session: state.bob })).status, 200);
  assert.equal((await web(docPath, { session: state.bob, method: "PATCH", json: { title: "hijack" } })).status, 403);
  assert.equal((await web(docPath, { session: state.bob, method: "DELETE" })).status, 403);
  for (const path of [docPath, `${state.wsPath}/documents`, `${docPath}/versions`, `${docPath}/versions/${state.v1}/content-url`]) {
    assert.equal((await web(path, { session: state.mallory })).status, 404, `outsider: ${path}`);
  }
});

test("rename is visible to everyone who can read", async () => {
  const docPath = `${state.wsPath}/documents/${state.docId}`;
  const r = await web(docPath, { session: state.alice, method: "PATCH", json: { title: "Team notes (final)" } });
  assert.equal(r.status, 200, r.text);
  assert.equal((await web(docPath, { session: state.bob })).body.title, "Team notes (final)");
  state.title = "Team notes (final)";
});

test("UI: the library and viewer pages render for members; an outsider and a bad link get the calm page", async () => {
  const lib = `/o/${ORG.slug}/w/${WS.slug}/documents`;
  const view = `${lib}/${state.docId}`;
  for (const who of ["alice", "bob"]) {
    const l = await web(lib, { session: state[who] });
    assert.equal(l.status, 200);
    assert.ok(l.text.includes(state.title), `${who}: the library lists the document`);
    const v = await web(view, { session: state[who] });
    assert.equal(v.status, 200);
    assert.ok(v.text.includes(state.title), `${who}: the viewer shows the document`);
  }
  for (const path of [lib, view]) {
    const m = await web(path, { session: state.mallory });
    assert.ok(!m.text.includes(state.title) && !m.text.includes(WS.name), `outsider sees nothing at ${path}`);
  }
  const anon = await web(lib);
  assert.ok([302, 307].includes(anon.status), "signed out: redirect to login");
  assert.ok(anon.location.includes("/api/auth/login"));
});

test("deletion: immediate denial everywhere, for everyone", async () => {
  const docPath = `${state.wsPath}/documents/${state.docId}`;
  const del = await web(docPath, { session: state.alice, method: "DELETE" });
  assert.equal(del.status, 202, del.text);
  assert.equal(del.body.status, "deletion_requested");
  assert.ok(Date.parse(del.body.purge_after) > Date.now());
  for (const who of ["alice", "bob"]) {
    for (const path of [docPath, `${docPath}/versions`, `${docPath}/versions/${state.v1}`, `${docPath}/versions/${state.v1}/content-url`]) {
      assert.equal((await web(path, { session: state[who] })).status, 404, `${who} ${path}`);
    }
    const list = await web(`${state.wsPath}/documents`, { session: state[who] });
    assert.ok(!list.body.items.some((d) => d.id === state.docId), `${who}: gone from the list`);
  }
  assert.equal((await web(docPath, { session: state.alice, method: "DELETE" })).status, 404, "a second delete is a 404");
  const view = await web(`/o/${ORG.slug}/w/${WS.slug}/documents/${state.docId}`, { session: state.alice });
  assert.ok(!view.text.includes(state.title), "the viewer no longer shows the deleted document");
  // The other document in the workspace is untouched.
  assert.equal((await web(`${state.wsPath}/documents/${state.htmlDoc}`, { session: state.alice })).status, 200);
});

test("audit trail: every step recorded, and no file name or content leaks into it", () => {
  const events = sql(`SELECT event_type || ':' || outcome FROM audit_events WHERE org_id = '${state.orgId}' ORDER BY occurred_at`).split("\n");
  for (const e of ["document.created:success", "document.version.created:success", "document.updated:success", "document.deleted:success", "authz.denied:denied"]) {
    assert.ok(events.includes(e), `audit has ${e} (got ${[...new Set(events)].join(", ")})`);
  }
  // Titles legitimately appear in rename events; FILE names (the uploaded names) must never be recorded.
  const names = ["notes.txt", "notes-v2.txt", "page.html", "tamper.bin"];
  const clause = names.map((n) => `position('${n}' in metadata::text) > 0`).join(" OR ");
  assert.equal(sql(`SELECT count(*) FROM audit_events WHERE ${clause}`), "0", "no uploaded file names in audit metadata");
  const versions = sql(`SELECT count(*) FROM document_versions WHERE document_id = '${state.docId}'`);
  assert.equal(versions, "2", "versions are kept after the deletion request (purge comes later)");
  assert.equal(sql(`SELECT count(*) FROM document_versions WHERE purged_at IS NOT NULL`), "0");
});

test("no token ever reached the browser", () => {
  for (const t of transcript) {
    assert.ok(!JWT_RE.test(JSON.stringify(t.headers) + t.body), `token-shaped value in response to ${t.path}`);
  }
});
