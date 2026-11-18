// M01.02.03 / M01.02.05 end-to-end over HTTP (no browser): real Dex login, the built web app, and an
// in-memory stub of the contract §4 tenancy API. Run through ./run.sh.
import { test } from "node:test";
import assert from "node:assert/strict";

const WEB = process.env.WEB_URL ?? "http://127.0.0.1:23310";
const STUB = process.env.STUB_URL ?? "http://127.0.0.1:23311";
// Test-only Dex users (infrastructure/compose/identity/README.md).
const ALICE = { login: "alice@ukos.test", password: "ukos-dev-alice-password" };
const SESSION_COOKIE = "ukos_session";
const TX_COOKIE = "ukos_auth_tx";

function setCookies(res) {
  const out = {};
  for (const raw of res.headers.getSetCookie()) {
    const pair = raw.split(";")[0];
    const eq = pair.indexOf("=");
    out[pair.slice(0, eq)] = pair.slice(eq + 1);
  }
  return out;
}

async function web(path, { cookie, method = "GET", body, headers = {} } = {}) {
  const res = await fetch(path.startsWith("http") ? path : WEB + path, {
    method,
    body,
    redirect: "manual",
    headers: { ...(cookie ? { cookie: `${SESSION_COOKIE}=${cookie}` } : {}), ...headers },
  });
  return { res, text: await res.text() };
}

const unescapeHtml = (s) => s.replace(/&amp;/g, "&").replace(/&#43;/g, "+").replace(/&quot;/g, '"');

/** Drives a login that starts at `loginPath` (e.g. the proxy's redirect target); returns cookie + landing URL. */
async function loginVia(loginPath, creds = ALICE) {
  const start = await fetch(new URL(loginPath, WEB), { redirect: "manual" });
  assert.equal(start.status, 302, "login route redirects to Dex");
  const tx = setCookies(start)[TX_COOKIE];
  assert.ok(tx, "tx cookie");
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
  let res = await fetch(formAction, {
    method: "POST",
    redirect: "manual",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(creds).toString(),
  });
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
  assert.ok(cb.status === 302 || cb.status === 303, `callback redirects to returnTo (got ${cb.status})`);
  const session = setCookies(cb)[SESSION_COOKIE];
  assert.ok(session, "session cookie set");
  return { session, landing: new URL(cb.headers.get("location"), WEB) };
}

const login = (creds = ALICE) => loginVia("/api/auth/login?returnTo=%2Fhome", creds);

/** Text of the element carrying `data-testid` (first match, tags stripped). */
function testIdText(html, id) {
  const m = html.match(new RegExp(`data-testid="${id}"[^>]*>([\\s\\S]*?)</`));
  return m ? m[1].replace(/<[^>]+>/g, "").trim() : null;
}

// The requested slug itself may be echoed (it is in the URL the user typed); nothing the API holds may be.
const OTHER_TENANT = ["Initech Holdings", "Initech Secret Plans", "mallory@ukos.test"];
const roleBadge = (html, role) => new RegExp(`data-testid="current-org-role"[^>]*>(?:<span[^>]*>[^<]*</span>)?${role}<`).test(html);
const ACME_WS = ["Acme Notes", "Acme Papers"];

test("login → /home lists the caller's orgs only", async () => {
  const { session, landing } = await login();
  assert.equal(landing.pathname, "/home");
  const home = await web("/home", { cookie: session });
  assert.equal(home.res.status, 200);
  assert.match(home.text, /alice@ukos\.test/);
  assert.match(home.text, /Your organizations/);
  assert.match(home.text, /href="\/o\/acme"/);
  assert.match(home.text, /href="\/o\/globex"/);
  for (const s of ["Initech Holdings", "/o/initech"]) assert.ok(!home.text.includes(s), `no ${s} on /home`);
  assert.match(home.text, /Create an organization/);
});

test("deep link on a fresh session: login with returnTo, land back on the workspace", async () => {
  const deep = "/o/acme/w/papers";
  const first = await web(deep);
  assert.equal(first.res.status, 307);
  const loginPath = first.res.headers.get("location");
  assert.equal(new URL(loginPath, WEB).pathname + new URL(loginPath, WEB).search, `/api/auth/login?returnTo=${encodeURIComponent(deep)}`);

  const { session, landing } = await loginVia(loginPath);
  assert.equal(landing.href, `${WEB}${deep}`, "callback returns to the deep link");

  const page = await web(deep, { cookie: session });
  assert.equal(page.res.status, 200);
  assert.equal(testIdText(page.text, "workspace-heading"), "Acme Papers");
  assert.equal(testIdText(page.text, "current-org-name"), "Acme Research");
  // Sidebar: the active workspace link is aria-current="page", the other is not.
  assert.match(page.text, /<a[^>]*href="\/o\/acme\/w\/papers"[^>]*aria-current="page"|<a[^>]*aria-current="page"[^>]*href="\/o\/acme\/w\/papers"/);
  assert.doesNotMatch(page.text, /<a[^>]*href="\/o\/acme\/w\/notes"[^>]*aria-current="page"/);
});

test("unknown or unauthorized org / workspace → the same calm not-found page, no other tenant's data", async () => {
  const { session } = await login();
  for (const path of ["/o/initech", "/o/initech/w/plans", "/o/no-such-org", "/o/globex/w/vault", "/o/acme/w/no-such-ws"]) {
    const r = await web(path, { cookie: session });
    assert.equal(r.res.status, 200, path); // streamed (Suspense) page; the content is what matters
    assert.match(r.text, /Not found or no access/, path);
    assert.match(r.text, /data-testid="scope-not-found"/, path);
    for (const leak of [...OTHER_TENANT, "Globex Vault"]) assert.ok(!r.text.includes(leak), `${path} leaks ${leak}`);
  }
  // Slug resolution happens against alice's own org list: the web never even asked the API about initech.
  const initech = (await (await fetch(`${STUB}/__orgs`)).json()).find((o) => o.slug === "initech");
  const log = await (await fetch(`${STUB}/__log`)).json();
  assert.ok(log.length > 0);
  assert.ok(!log.some((e) => e.path.includes(initech.id)), "no request ever targeted another tenant's org id");
});

test("switching org (server-rendered pages) shows the new org scope and none of the previous org's data", async () => {
  const { session } = await login();
  const a = await web("/o/acme", { cookie: session });
  assert.equal(testIdText(a.text, "current-org-name"), "Acme Research");
  assert.equal(testIdText(a.text, "org-heading"), "Acme Research");
  assert.ok(roleBadge(a.text, "Owner"), "role badge: Owner");
  for (const n of ACME_WS) assert.ok(a.text.includes(n), `acme shows ${n}`);
  assert.match(a.text, /Create a workspace/, "owner sees the create-workspace form");

  const b = await web("/o/globex", { cookie: session });
  assert.equal(b.res.status, 200);
  assert.equal(testIdText(b.text, "current-org-name"), "Globex Lab");
  assert.equal(testIdText(b.text, "org-heading"), "Globex Lab");
  assert.ok(roleBadge(b.text, "Member"), "role badge: Member");
  assert.ok(b.text.includes("Globex Bench"));
  assert.ok(!b.text.includes("Globex Vault"), "workspace alice cannot read is not listed");
  for (const n of ACME_WS) assert.ok(!b.text.includes(n), `globex view must not contain ${n}`);
  assert.ok(!b.text.includes("Create a workspace"), "members do not see the create-workspace form");

  const bw = await web("/o/globex/w/bench", { cookie: session });
  assert.equal(testIdText(bw.text, "workspace-heading"), "Globex Bench");
  for (const n of ACME_WS) assert.ok(!bw.text.includes(n));
});

test("create org / workspace through the BFF: 201, 409 slug_taken, 403 for members", async () => {
  const { session } = await login();
  const post = (path, body) =>
    web(`/api/v1${path}`, {
      cookie: session,
      method: "POST",
      body: JSON.stringify(body),
      headers: { "content-type": "application/json", origin: WEB, "sec-fetch-site": "same-origin" },
    });
  const created = await post("/orgs", { slug: "alice-lab", name: "Alice Lab" });
  assert.equal(created.res.status, 201, created.text);
  const org = JSON.parse(created.text);
  assert.equal(org.role, "owner");

  const dup = await post("/orgs", { slug: "alice-lab", name: "Again" });
  assert.equal(dup.res.status, 409);
  assert.deepEqual(JSON.parse(dup.text), { detail: "slug_taken" });

  const home = await web("/home", { cookie: session });
  assert.match(home.text, /href="\/o\/alice-lab"/);

  const ws = await post(`/orgs/${org.id}/workspaces`, { slug: "drafts", name: "Drafts" });
  assert.equal(ws.res.status, 201);
  const page = await web("/o/alice-lab/w/drafts", { cookie: session });
  assert.equal(testIdText(page.text, "workspace-heading"), "Drafts");

  const orgsList = JSON.parse((await web("/api/v1/orgs", { cookie: session })).text);
  const globex = orgsList.items.find((o) => o.slug === "globex");
  const forbidden = await post(`/orgs/${globex.id}/workspaces`, { slug: "x-ws", name: "X" });
  assert.equal(forbidden.res.status, 403);

  // CSRF: a cross-site POST never reaches the API.
  const csrf = await web("/api/v1/orgs", {
    cookie: session,
    method: "POST",
    body: JSON.stringify({ slug: "evil-org", name: "Evil" }),
    headers: { "content-type": "application/json", origin: "http://evil.example", "sec-fetch-site": "cross-site" },
  });
  assert.equal(csrf.res.status, 403);
  assert.deepEqual(JSON.parse(csrf.text), { detail: "csrf_rejected" });
});

test("signed out: org pages and the BFF send the user to login", async () => {
  const r = await web("/o/globex");
  assert.equal(r.res.status, 307);
  assert.match(r.res.headers.get("location"), /\/api\/auth\/login\?returnTo=%2Fo%2Fglobex$/);
  const api = await web("/api/v1/orgs");
  assert.equal(api.res.status, 401);
  assert.deepEqual(JSON.parse(api.text), { detail: "unauthenticated" });
});
