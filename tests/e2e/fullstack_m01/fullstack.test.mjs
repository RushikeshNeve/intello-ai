// M01 full-stack e2e: real web (BFF) + API + Dex + PostgreSQL, scripted over HTTP.
// Run through ./run.sh. Exercises contract §4 through the browser-facing BFF exactly as the UI does.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execSync } from "node:child_process";
import { randomBytes } from "node:crypto";

const WEB = process.env.WEB_URL ?? "http://127.0.0.1:33400";
const SESSION_COOKIE = "ukos_session";
const TX_COOKIE = "ukos_auth_tx";
const JWT_RE = /eyJ[\w-]+\.[\w-]+\.[\w-]+/;
const USERS = {
  alice: { login: "alice@ukos.test", password: "ukos-dev-alice-password" },
  bob: { login: "bob@ukos.test", password: "ukos-dev-bob-password" },
  mallory: { login: "mallory@ukos.test", password: "ukos-dev-mallory-password" },
};
const transcript = [];

// ---------- HTTP helpers (browser-like: same-origin headers on writes, cookies only) ----------
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

/** Full login; returns { session, landing } where landing is where the callback redirected. */
async function login(who, returnTo = "/home") {
  const start = await web(`/api/auth/login?returnTo=${encodeURIComponent(returnTo)}`);
  assert.equal(start.status, 302, "login starts with a redirect to Dex");
  const callback = await dexLogin(start.location, { login: USERS[who].login, password: USERS[who].password });
  const cb = await web(callback, { extraCookies: { [TX_COOKIE]: start.setCookies[TX_COOKIE] } });
  assert.ok([302, 303, 307].includes(cb.status), `callback redirects (${cb.status})`);
  const session = cb.setCookies[SESSION_COOKIE];
  assert.ok(session, `${who} got a session cookie`);
  return { session, landing: cb.location };
}

function sql(query) {
  const cmd = `${process.env.PG_EXEC} psql -U ukos_admin -d ukos -Atc "${query.replace(/"/g, '\\"')}"`;
  return execSync(cmd, { encoding: "utf8" }).trim();
}

const suffix = randomBytes(3).toString("hex");
const ORG = { slug: `acme-${suffix}`, name: `Acme Research ${suffix}` };
const WS = { slug: `lab-${suffix}`, name: `Lab Notes ${suffix}` };
const state = {};

// ---------- scenario ----------
test("alice signs in and is provisioned (GET /api/v1/me)", async () => {
  state.alice = await login("alice");
  const me = await web("/api/v1/me", { session: state.alice.session });
  assert.equal(me.status, 200);
  assert.equal(me.body.email, USERS.alice.login);
  state.aliceId = me.body.id;
});

test("alice creates an organization and becomes its owner", async () => {
  const r = await web("/api/v1/orgs", { session: state.alice.session, method: "POST", json: ORG });
  assert.equal(r.status, 201, r.text);
  assert.equal(r.body.slug, ORG.slug);
  assert.equal(r.body.role, "owner");
  state.orgId = r.body.id;
  const dup = await web("/api/v1/orgs", { session: state.alice.session, method: "POST", json: { ...ORG, slug: ORG.slug.toUpperCase() } });
  assert.ok([409, 422].includes(dup.status), `case-variant slug rejected (${dup.status})`);
});

test("bob and mallory sign in; neither can see alice's org yet", async () => {
  state.bob = await login("bob");
  state.mallory = await login("mallory");
  // A user row is created on the first authenticated API call, not at web login: make both calls now.
  state.bobId = (await web("/api/v1/me", { session: state.bob.session })).body.id;
  state.malloryId = (await web("/api/v1/me", { session: state.mallory.session })).body.id;
  assert.ok(state.bobId && state.malloryId && state.bobId !== state.malloryId, "bob and mallory are distinct users");
  for (const who of ["bob", "mallory"]) {
    const list = await web("/api/v1/orgs", { session: state[who].session });
    assert.equal(list.status, 200);
    assert.ok(!list.body.items.some((o) => o.id === state.orgId), `${who} must not list the org`);
    const direct = await web(`/api/v1/orgs/${state.orgId}`, { session: state[who].session });
    assert.equal(direct.status, 404, `${who} gets 404 (existence hidden)`);
  }
});

test("alice adds bob as a member; bob now lists the org with role member", async () => {
  const r = await web(`/api/v1/orgs/${state.orgId}/members/${state.bobId}`, { session: state.alice.session, method: "PUT", json: { role: "member" } });
  assert.equal(r.status, 200, r.text);
  const list = await web("/api/v1/orgs", { session: state.bob.session });
  const org = list.body.items.find((o) => o.id === state.orgId);
  assert.ok(org, "bob lists the org");
  assert.equal(org.role, "member");
});

test("role enforcement: member bob cannot update the org or manage members (403)", async () => {
  const patch = await web(`/api/v1/orgs/${state.orgId}`, { session: state.bob.session, method: "PATCH", json: { name: "hijack" } });
  assert.equal(patch.status, 403);
  const rm = await web(`/api/v1/orgs/${state.orgId}/members/${state.aliceId}`, { session: state.bob.session, method: "DELETE" });
  assert.equal(rm.status, 403);
});

test("last-owner protection: alice cannot demote herself (409 last_owner)", async () => {
  const r = await web(`/api/v1/orgs/${state.orgId}/members/${state.aliceId}`, { session: state.alice.session, method: "PUT", json: { role: "member" } });
  assert.equal(r.status, 409, r.text);
  assert.equal(r.body.detail, "last_owner");
});

test("workspaces: alice creates one; bob sees it only after getting a workspace role", async () => {
  const c = await web(`/api/v1/orgs/${state.orgId}/workspaces`, { session: state.alice.session, method: "POST", json: WS });
  assert.equal(c.status, 201, c.text);
  state.wsId = c.body.id;
  let list = await web(`/api/v1/orgs/${state.orgId}/workspaces`, { session: state.bob.session });
  assert.equal(list.status, 200);
  assert.ok(!list.body.items.some((w) => w.id === state.wsId), "bob has no workspace role yet");
  const add = await web(`/api/v1/orgs/${state.orgId}/workspaces/${state.wsId}/members/${state.bobId}`, { session: state.alice.session, method: "PUT", json: { role: "viewer" } });
  assert.equal(add.status, 200, add.text);
  list = await web(`/api/v1/orgs/${state.orgId}/workspaces`, { session: state.bob.session });
  assert.ok(list.body.items.some((w) => w.id === state.wsId && w.role === "viewer"), "bob sees the workspace as viewer");
  const bobCreate = await web(`/api/v1/orgs/${state.orgId}/workspaces`, { session: state.bob.session, method: "POST", json: { slug: `x-${suffix}`, name: "x" } });
  assert.equal(bobCreate.status, 403, "members cannot create workspaces");
  const mal = await web(`/api/v1/orgs/${state.orgId}/workspaces/${state.wsId}`, { session: state.mallory.session });
  assert.equal(mal.status, 404, "outsider gets 404 for the workspace");
});

test("UI: org and workspace pages render for members; outsiders see no data", async () => {
  const orgPage = await web(`/o/${ORG.slug}`, { session: state.alice.session });
  assert.equal(orgPage.status, 200);
  assert.ok(orgPage.text.includes(ORG.name), "org page shows the org name");
  const wsPage = await web(`/o/${ORG.slug}/w/${WS.slug}`, { session: state.bob.session });
  assert.equal(wsPage.status, 200);
  assert.ok(wsPage.text.includes(WS.name), "workspace page shows the workspace for bob");
  for (const path of [`/o/${ORG.slug}`, `/o/${ORG.slug}/w/${WS.slug}`]) {
    const mal = await web(path, { session: state.mallory.session });
    assert.ok(!mal.text.includes(ORG.name) && !mal.text.includes(WS.name), `mallory sees nothing at ${path}`);
  }
});

test("UI: a deep link on a fresh session goes through login and lands back on it", async () => {
  const deep = `/o/${ORG.slug}/w/${WS.slug}`;
  const anon = await web(deep);
  assert.ok([302, 303, 307].includes(anon.status), `anonymous deep link redirects (${anon.status})`);
  assert.ok(anon.location.includes("/api/auth/login"), "redirected to login");
  const returnTo = new URL(anon.location, WEB).searchParams.get("returnTo");
  assert.equal(returnTo, deep, "returnTo carries the deep link");
  const again = await login("alice", returnTo);
  assert.equal(new URL(again.landing, WEB).pathname, deep, "callback lands on the deep link");
});

test("first API call provisioned exactly one user and one user.provisioned audit row per user", () => {
  assert.equal(sql("SELECT count(*) FROM users"), "3");
  assert.equal(sql("SELECT count(*) FROM audit_events WHERE event_type = 'user.provisioned'"), "3");
});

test("audit trail recorded every mutation and denial for the org", () => {
  const rows = sql(`SELECT event_type || ':' || outcome FROM audit_events WHERE org_id = '${state.orgId}' ORDER BY occurred_at`).split("\n");
  for (const e of ["org.created:success", "org.member.added:success", "workspace.created:success", "workspace.member.added:success", "authz.denied:denied"]) {
    assert.ok(rows.includes(e), `audit has ${e} (got: ${rows.join(", ")})`);
  }
});

test("no token ever reached the browser", () => {
  for (const t of transcript) {
    const blob = JSON.stringify(t.headers) + t.body;
    assert.ok(!JWT_RE.test(blob), `token-shaped value in response to ${t.path}`);
  }
});
