// In-memory stub of the UKOS tenancy API (contract §4) for the web tenancy e2e. NOT the real API.
//
// Identity: the caller is the bearer token's `email` (falling back to `sub`). The token is a Dex JWT
// forwarded by the web BFF / server DAL; its signature is verified against Dex's JWKS so a forged token
// is rejected like the real API would (401 invalid_token).
//
// Seed (by email, Dex test users):
//   acme    "Acme Research"   alice=owner                    workspaces: notes "Acme Notes", papers "Acme Papers"
//   globex  "Globex Lab"      bob=owner, alice=member        workspaces: bench "Globex Bench" (alice viewer), vault "Globex Vault" (bob only)
//   initech "Initech Holdings" mallory=owner                 workspaces: plans "Initech Secret Plans"
//
//   GET /__log   every request received (method, path, caller)
import { createServer } from "node:http";
import { createPublicKey, createVerify, randomUUID } from "node:crypto";

const port = Number(process.env.STUB_PORT ?? 23311);
const issuer = process.env.OIDC_ISSUER_URL ?? "http://127.0.0.1:28392/dex";
const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,62}$/;
const log = [];

const now = () => new Date().toISOString();
const orgs = new Map(); // id -> {id, slug, name, created_at, members: Map(email -> role)}
const workspaces = new Map(); // id -> {id, org_id, slug, name, created_at, members: Map(email -> role)}

function seedOrg(slug, name, members, wss) {
  const org = { id: randomUUID(), slug, name, created_at: now(), members: new Map(Object.entries(members)) };
  orgs.set(org.id, org);
  for (const [wslug, wname, wmembers] of wss) {
    const w = { id: randomUUID(), org_id: org.id, slug: wslug, name: wname, created_at: now(), members: new Map(Object.entries(wmembers)) };
    workspaces.set(w.id, w);
  }
}
seedOrg("acme", "Acme Research", { "alice@ukos.test": "owner" }, [
  ["notes", "Acme Notes", { "alice@ukos.test": "admin" }],
  ["papers", "Acme Papers", { "alice@ukos.test": "admin" }],
]);
seedOrg("globex", "Globex Lab", { "bob@ukos.test": "owner", "alice@ukos.test": "member" }, [
  ["bench", "Globex Bench", { "bob@ukos.test": "admin", "alice@ukos.test": "viewer" }],
  ["vault", "Globex Vault", { "bob@ukos.test": "admin" }],
]);
seedOrg("initech", "Initech Holdings", { "mallory@ukos.test": "owner" }, [
  ["plans", "Initech Secret Plans", { "mallory@ukos.test": "admin" }],
]);

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

const send = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(status === 204 ? undefined : JSON.stringify(body));
};
const orgRole = (org, me) => org.members.get(me) ?? null;
const orgOut = (org, me) => ({ id: org.id, slug: org.slug, name: org.name, created_at: org.created_at, role: orgRole(org, me) });
const summary = (org, me) => ({ id: org.id, slug: org.slug, name: org.name, role: orgRole(org, me) });
function wsRole(w, org, me) {
  const r = orgRole(org, me);
  if (r === "owner" || r === "admin") return w.members.get(me) ?? "admin"; // implicit workspace admin (§3)
  return w.members.get(me) ?? null;
}
const wsOut = (w, org, me) => ({ id: w.id, org_id: w.org_id, slug: w.slug, name: w.name, created_at: w.created_at, role: wsRole(w, org, me) });

function paginate(url, items) {
  const limit = Math.min(Math.max(Number(url.searchParams.get("limit") ?? 50) || 50, 1), 200);
  const start = Number(url.searchParams.get("cursor") ?? 0) || 0;
  const page = items.slice(start, start + limit);
  const next = start + limit < items.length ? String(start + limit) : undefined;
  return next ? { items: page, next_cursor: next } : { items: page };
}

async function handle(req, res, body, url) {
  const auth = req.headers.authorization ?? "";
  const claims = auth.startsWith("Bearer ") ? await verify(auth.slice(7)) : null;
  const me = claims ? (claims.email ?? claims.sub) : null;
  log.push({ method: req.method, path: url.pathname + url.search, caller: me });
  if (!me) return send(res, 401, { detail: "invalid_token" });

  const parts = url.pathname.split("/").filter(Boolean); // ["v1", ...]
  if (parts[0] !== "v1") return send(res, 404, { detail: "not_found" });
  const [, a, orgId, b, wsId] = parts;
  let input = {};
  if (body) {
    try {
      input = JSON.parse(body);
    } catch {
      return send(res, 422, { detail: [{ msg: "invalid json" }] });
    }
  }

  if (a === "me" && parts.length === 2 && req.method === "GET") {
    return send(res, 200, { id: claims.sub, email: claims.email ?? null, display_name: claims.name ?? null });
  }
  if (a !== "orgs") return send(res, 404, { detail: "not_found" });

  if (parts.length === 2) {
    if (req.method === "GET") {
      const mine = [...orgs.values()].filter((o) => orgRole(o, me)).map((o) => summary(o, me));
      return send(res, 200, paginate(url, mine));
    }
    if (req.method === "POST") {
      if (typeof input.slug !== "string" || !SLUG_RE.test(input.slug) || typeof input.name !== "string" || !input.name.trim()) {
        return send(res, 422, { detail: [{ loc: ["body"], msg: "invalid slug or name" }] });
      }
      if ([...orgs.values()].some((o) => o.slug === input.slug)) return send(res, 409, { detail: "slug_taken" });
      const org = { id: randomUUID(), slug: input.slug, name: input.name.trim(), created_at: now(), members: new Map([[me, "owner"]]) };
      orgs.set(org.id, org);
      return send(res, 201, orgOut(org, me));
    }
    return send(res, 405, { detail: "method_not_allowed" });
  }

  const org = orgs.get(orgId);
  // Not a member ⇒ 404 (indistinguishable from a missing org).
  if (!org || !orgRole(org, me)) return send(res, 404, { detail: "not_found" });
  if (parts.length === 3 && req.method === "GET") return send(res, 200, orgOut(org, me));
  if (b !== "workspaces") return send(res, 404, { detail: "not_found" });

  if (parts.length === 4) {
    if (req.method === "GET") {
      const visible = [...workspaces.values()].filter((w) => w.org_id === org.id && wsRole(w, org, me)).map((w) => wsOut(w, org, me));
      return send(res, 200, paginate(url, visible));
    }
    if (req.method === "POST") {
      const r = orgRole(org, me);
      if (r !== "owner" && r !== "admin") return send(res, 403, { detail: "forbidden" });
      if (typeof input.slug !== "string" || !SLUG_RE.test(input.slug) || typeof input.name !== "string" || !input.name.trim()) {
        return send(res, 422, { detail: [{ loc: ["body"], msg: "invalid slug or name" }] });
      }
      if ([...workspaces.values()].some((w) => w.org_id === org.id && w.slug === input.slug)) return send(res, 409, { detail: "slug_taken" });
      const w = { id: randomUUID(), org_id: org.id, slug: input.slug, name: input.name.trim(), created_at: now(), members: new Map([[me, "admin"]]) };
      workspaces.set(w.id, w);
      return send(res, 201, wsOut(w, org, me));
    }
    return send(res, 405, { detail: "method_not_allowed" });
  }
  if (parts.length === 5 && req.method === "GET") {
    const w = workspaces.get(wsId);
    if (!w || w.org_id !== org.id || !wsRole(w, org, me)) return send(res, 404, { detail: "not_found" });
    return send(res, 200, wsOut(w, org, me));
  }
  return send(res, 404, { detail: "not_found" });
}

const server = createServer((req, res) => {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    if (url.pathname === "/__log") return send(res, 200, log);
    if (url.pathname === "/__orgs") return send(res, 200, [...orgs.values()].map((o) => ({ id: o.id, slug: o.slug })));
    handle(req, res, Buffer.concat(chunks).toString("utf8"), url).catch((err) => {
      console.error(err);
      send(res, 500, { detail: "stub_error" });
    });
  });
});

server.listen(port, "127.0.0.1", () => console.log(`tenancy stub-api listening on 127.0.0.1:${port}`));
