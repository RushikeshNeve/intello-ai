// Post-deploy smoke for the public (Caddy/TLS) stack. UNTESTED against a real deployment (M10 preparation).
//   BASE_URL=https://app.example.org node tests/e2e/prod_smoke/smoke.mjs
// Optional authenticated flow: SMOKE_USER=<email> SMOKE_PASSWORD=<password> (the Dex admin or a normal user; never commit them).
// Optional: FILES_URL=https://files.example.org (checks the object-store host answers over TLS).
// Read-only by default: it creates nothing. The authenticated part only lists organizations.
import assert from "node:assert/strict";

const BASE = (process.env.BASE_URL ?? "").replace(/\/+$/, "");
if (!BASE.startsWith("https://")) {
  console.error("Set BASE_URL to the public https URL, e.g. BASE_URL=https://app.example.org");
  process.exit(2);
}
const FILES = (process.env.FILES_URL ?? "").replace(/\/+$/, "");
const USER = process.env.SMOKE_USER;
const PASSWORD = process.env.SMOKE_PASSWORD;
const SESSION = "__Host-ukos_session";
const TX = "__Secure-ukos_auth_tx";

let failed = 0;
const step = async (name, fn) => {
  try {
    const detail = await fn();
    console.log(`ok    ${name}${detail ? ` (${detail})` : ""}`);
  } catch (e) {
    failed += 1;
    console.log(`FAIL  ${name}: ${e instanceof Error ? e.message : e}`);
  }
};
const cookies = (res) => Object.fromEntries(res.headers.getSetCookie().map((c) => c.split(";")[0].split(/=(.*)/s).slice(0, 2)));
const unescapeHtml = (s) => s.replace(/&amp;/g, "&").replace(/&#43;/g, "+").replace(/&quot;/g, '"');
const get = (path, init = {}) => fetch(path.startsWith("http") ? path : BASE + path, { redirect: "manual", signal: AbortSignal.timeout(30_000), ...init });

await step("web health", async () => {
  const r = await get("/api/health");
  assert.equal(r.status, 200);
});

await step("home page is served over TLS with HSTS", async () => {
  const r = await get("/");
  assert.ok([200, 302, 307].includes(r.status), `status ${r.status}`);
  assert.ok(r.headers.get("strict-transport-security"), "missing Strict-Transport-Security");
});

await step("http redirects to https", async () => {
  const r = await fetch(BASE.replace("https://", "http://") + "/", { redirect: "manual", signal: AbortSignal.timeout(15_000) });
  assert.ok([301, 302, 307, 308].includes(r.status), `status ${r.status}`);
  assert.match(r.headers.get("location") ?? "", /^https:\/\//);
});

await step("OIDC discovery matches the public issuer", async () => {
  const r = await get("/dex/.well-known/openid-configuration");
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.equal(j.issuer, `${BASE}/dex`);
});

await step("sign-in starts: redirect to the issuer, secure tx cookie", async () => {
  const r = await get("/api/auth/login?returnTo=%2Fhome");
  assert.equal(r.status, 302);
  assert.ok(r.headers.get("location")?.startsWith(`${BASE}/dex/`), `location ${r.headers.get("location")}`);
  assert.ok(cookies(r)[TX], "auth transaction cookie (__Secure-) not set: is WEB_PUBLIC_URL https?");
});

await step("anonymous API call is 401", async () => {
  const r = await get("/api/v1/orgs");
  assert.equal(r.status, 401);
});

if (FILES) {
  await step("object-store host answers over TLS", async () => {
    const r = await get(FILES + "/");
    assert.ok(r.status > 0 && r.status < 500, `status ${r.status}`); // 403/404 without credentials is expected
  });
}

async function login() {
  const start = await get("/api/auth/login?returnTo=%2Fhome");
  const tx = cookies(start)[TX];
  let url = start.headers.get("location");
  let html = "";
  for (let i = 0; i < 5; i++) {
    const r = await get(url);
    if (r.status >= 300 && r.status < 400) { url = new URL(r.headers.get("location"), url).href; continue; }
    html = await r.text();
    break;
  }
  const m = html.match(/<form method="post" action="([^"]+)"/);
  assert.ok(m, "Dex login form not found");
  const action = new URL(unescapeHtml(m[1]), url).href;
  let r = await get(action, {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ login: USER, password: PASSWORD }).toString(),
  });
  let cur = action;
  for (let i = 0; i < 6 && r.status >= 300 && r.status < 400; i++) {
    const next = new URL(r.headers.get("location"), cur).href;
    if (next.startsWith(`${BASE}/api/auth/callback`)) {
      const cb = await get(next, { headers: { cookie: `${TX}=${tx}` } });
      const s = cookies(cb)[SESSION];
      assert.ok(s, "session cookie not issued after callback");
      return s;
    }
    cur = next;
    r = await get(cur);
  }
  throw new Error("sign-in did not reach the callback (wrong credentials?)");
}

if (USER && PASSWORD) {
  let session = "";
  await step("sign in through Dex", async () => { session = await login(); });
  if (session) {
    await step("authenticated API call (list organizations)", async () => {
      const r = await get("/api/v1/orgs", { headers: { cookie: `${SESSION}=${session}` } });
      assert.equal(r.status, 200);
    });
    await step("sign out invalidates the session", async () => {
      const out = await get("/api/auth/logout", { method: "POST", headers: { cookie: `${SESSION}=${session}`, origin: BASE, "sec-fetch-site": "same-origin" } });
      assert.ok(out.status < 500, `logout status ${out.status}`);
      const r = await get("/api/v1/orgs", { headers: { cookie: `${SESSION}=${session}` } });
      assert.equal(r.status, 401);
    });
  }
} else {
  console.log("skip  authenticated flow (set SMOKE_USER and SMOKE_PASSWORD to enable)");
}

console.log(failed ? `\nsmoke: ${failed} step(s) FAILED` : "\nsmoke: all steps passed");
process.exit(failed ? 1 : 0);
