// In-memory stub of the UKOS API for the web documents e2e: the M01 tenancy shapes (orgs/workspaces) plus the
// contract m02 §5 documents / versions / content-url routes. It ALSO plays the object store: the URLs it
// issues point at /__store/... on this same server (a different path = the "object store origin").
// NOT the real API. Identity = the Dex JWT's email (signature verified against Dex's JWKS).
//
// Seed (Dex test users):
//   acme   alice=owner   workspaces: notes (alice admin) holding 34 documents, papers
//   globex bob=owner, alice=member   bench (alice VIEWER: read-only, can_upload=false) holds 1 doc; vault (bob only) holds 1 secret doc
//   initech mallory=owner   plans
//
//   GET /__log     every API request: {method, path, caller, status, ...}   GET /__seed   ids   GET /__store_log
import { createServer } from "node:http";
import { createPublicKey, createVerify, randomUUID } from "node:crypto";

const port = Number(process.env.STUB_PORT ?? 23331);
const issuer = process.env.OIDC_ISSUER_URL ?? "http://127.0.0.1:28393/dex";
const origin = `http://127.0.0.1:${port}`;
const log = [];
const storeLog = [];
const now = () => new Date().toISOString();

const orgs = new Map();
const workspaces = new Map();
const documents = new Map(); // id -> {id, workspace_id, title, created_at, updated_at, deleted, versions: [oldest..newest]}
const blobs = new Map(); // version id -> {bytes, content_type}

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

const acme = seedOrg("acme", "Acme Research", { "alice@ukos.test": "owner" }, [
  ["notes", "Acme Notes", { "alice@ukos.test": "admin" }],
  ["papers", "Acme Papers", { "alice@ukos.test": "admin" }],
]);
const globex = seedOrg("globex", "Globex Lab", { "bob@ukos.test": "owner", "alice@ukos.test": "member" }, [
  ["bench", "Globex Bench", { "bob@ukos.test": "admin", "alice@ukos.test": "viewer" }],
  ["vault", "Globex Vault", { "bob@ukos.test": "admin" }],
]);
seedOrg("initech", "Initech Holdings", { "mallory@ukos.test": "owner" }, [["plans", "Initech Secret Plans", { "mallory@ukos.test": "admin" }]]);

const sha = (n) => String(n).padStart(2, "0").repeat(32).slice(0, 64);
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");
const PDF = Buffer.from("%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n");

let tick = 0;
const at = (minutesAgo) => new Date(Date.UTC(2026, 10, 18, 12, 0, 0) - minutesAgo * 60_000).toISOString();

function addDoc(wsId, { title, versions, updatedMinutesAgo }) {
  const id = randomUUID();
  const doc = { id, workspace_id: wsId, title, created_at: at(updatedMinutesAgo + 60), updated_at: at(updatedMinutesAgo), deleted: false, versions: [] };
  versions.forEach((v, i) => {
    const vid = randomUUID();
    doc.versions.push({
      id: vid,
      document_id: id,
      version_number: i + 1,
      original_filename: v.filename,
      content_type: v.type,
      size_bytes: v.bytes.length,
      sha256: sha(++tick),
      created_by: "u-alice",
      created_at: at(updatedMinutesAgo + 30 - i),
    });
    blobs.set(vid, { bytes: v.bytes, content_type: v.type });
  });
  documents.set(id, doc);
  return doc;
}

const notes = acme.ws.notes.id;
export const HOSTILE = "# Meeting notes\n<script>window.__pwned=1</script>\n<img src=x onerror=alert(1)>\n";
addDoc(notes, { title: "Quarterly report", updatedMinutesAgo: 1, versions: [
  { filename: "report-v1.pdf", type: "application/pdf", bytes: PDF },
  { filename: "report-v2.pdf", type: "application/pdf", bytes: Buffer.concat([PDF, Buffer.from("% 2\n")]) },
  { filename: "report-v3.pdf", type: "application/pdf", bytes: Buffer.concat([PDF, Buffer.from("% 3\n")]) },
] });
addDoc(notes, { title: "Meeting notes", updatedMinutesAgo: 2, versions: [{ filename: "notes.md", type: "text/markdown", bytes: Buffer.from(HOSTILE) }] });
addDoc(notes, { title: "Logo", updatedMinutesAgo: 3, versions: [{ filename: "logo.png", type: "image/png", bytes: PNG }] });
addDoc(notes, { title: "Archive bundle", updatedMinutesAgo: 4, versions: [{ filename: "bundle.zip", type: "application/zip", bytes: Buffer.from("PK\x03\x04 not really a zip") }] });
for (let i = 1; i <= 30; i++) {
  addDoc(notes, { title: `Bulk document ${String(i).padStart(2, "0")}`, updatedMinutesAgo: 10 + i, versions: [{ filename: `bulk-${i}.txt`, type: "text/plain", bytes: Buffer.from(`bulk ${i}\n`) }] });
}
const benchDoc = addDoc(globex.ws.bench.id, { title: "Bench readout", updatedMinutesAgo: 5, versions: [{ filename: "readout.txt", type: "text/plain", bytes: Buffer.from("readout\n") }] });
const vaultDoc = addDoc(globex.ws.vault.id, { title: "Vault Secret Doc", updatedMinutesAgo: 5, versions: [{ filename: "secret.txt", type: "text/plain", bytes: Buffer.from("secret\n") }] });

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

const send = (res, status, body, entry) => {
  if (entry) entry.status = status;
  res.writeHead(status, { "content-type": "application/json" });
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

function versionOut(doc, v) {
  const latest = doc.versions.at(-1);
  return { ...v, retention: { status: v.id === latest.id ? "current" : "superseded", purge_after: null } };
}
function docOut(doc, w, org, role) {
  const latest = doc.versions.at(-1);
  return {
    id: doc.id,
    org_id: org.id,
    workspace_id: w.id,
    title: doc.title,
    created_by: "u-alice",
    created_at: doc.created_at,
    updated_at: doc.updated_at,
    version_count: doc.versions.length,
    latest_version: versionOut(doc, latest),
    permissions: perms(role),
  };
}

const INLINE = new Set(["application/pdf", "text/plain", "text/markdown", "image/png", "image/jpeg", "image/gif", "image/webp"]);

function listDocs(url, w, org, role, res, entry) {
  const q = (url.searchParams.get("q") ?? "").toLowerCase();
  const ct = url.searchParams.get("content_type");
  const sort = url.searchParams.get("sort") ?? "updated_at";
  const order = url.searchParams.get("order") ?? (sort === "title" ? "asc" : "desc");
  if (!["updated_at", "created_at", "title"].includes(sort) || !["asc", "desc"].includes(order)) {
    return send(res, 422, { detail: "invalid_request" }, entry);
  }
  const limit = Math.min(Math.max(Number(url.searchParams.get("limit") ?? 50) || 50, 1), 200);
  let offset = 0;
  const cursor = url.searchParams.get("cursor");
  if (cursor) {
    try {
      const c = JSON.parse(Buffer.from(cursor, "base64url").toString());
      if (typeof c.o !== "number" || c.sig !== `${sort}:${order}:${q}:${ct}`) throw new Error("bad");
      offset = c.o;
    } catch {
      return send(res, 422, { detail: "invalid_cursor" }, entry);
    }
  }
  let items = [...documents.values()].filter((d) => d.workspace_id === w.id && !d.deleted);
  if (q) items = items.filter((d) => d.title.toLowerCase().includes(q) || d.versions.at(-1).original_filename.toLowerCase().includes(q));
  if (ct) items = items.filter((d) => {
    const t = d.versions.at(-1).content_type;
    return ct.endsWith("/") || ct.endsWith(".") ? t.startsWith(ct) : t === ct;
  });
  const key = (d) => (sort === "title" ? d.title : d[sort]);
  items.sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : a.id < b.id ? -1 : 1) * (order === "asc" ? 1 : -1));
  const page = items.slice(offset, offset + limit);
  const next = offset + limit < items.length ? Buffer.from(JSON.stringify({ o: offset + limit, sig: `${sort}:${order}:${q}:${ct}` })).toString("base64url") : null;
  entry.items = page.length;
  entry.next_cursor = next;
  return send(res, 200, { items: page.map((d) => docOut(d, w, org, role)), next_cursor: next, can_upload: perms(role).write }, entry);
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
  const [, a, orgId, b, wsId, c, docId, d, versionId, e] = parts;
  if (a === "me") return send(res, 200, { id: claims.sub, email: claims.email ?? null, display_name: claims.name ?? null }, entry);
  if (a !== "orgs") return send(res, 404, { detail: "not_found" }, entry);

  if (parts.length === 2 && req.method === "GET") {
    const mine = [...orgs.values()].filter((o) => orgRole(o, me)).map((o) => ({ id: o.id, slug: o.slug, name: o.name, role: orgRole(o, me) }));
    return send(res, 200, { items: mine }, entry);
  }
  const org = orgs.get(orgId);
  if (!org || !orgRole(org, me)) return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 3) return send(res, 200, { id: org.id, slug: org.slug, name: org.name, created_at: org.created_at, role: orgRole(org, me) }, entry);
  if (b !== "workspaces") return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 4 && req.method === "GET") {
    const visible = [...workspaces.values()].filter((w) => w.org_id === org.id && wsRole(w, org, me)).map((w) => wsOut(w, org, me));
    return send(res, 200, { items: visible }, entry);
  }
  const w = workspaces.get(wsId);
  const role = w && w.org_id === org.id ? wsRole(w, org, me) : null;
  if (!role) return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 5) return send(res, 200, wsOut(w, org, me), entry);
  if (c !== "documents") return send(res, 404, { detail: "not_found" }, entry);

  if (parts.length === 6 && req.method === "GET") return listDocs(url, w, org, role, res, entry);
  const doc = documents.get(docId);
  if (!doc || doc.workspace_id !== w.id || doc.deleted) return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 7 && req.method === "GET") return send(res, 200, docOut(doc, w, org, role), entry);
  if (d !== "versions") return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 8 && req.method === "GET") {
    return send(res, 200, { items: [...doc.versions].reverse().map((v) => versionOut(doc, v)), next_cursor: null }, entry);
  }
  const ver = doc.versions.find((v) => v.id === versionId);
  if (!ver) return send(res, 404, { detail: "not_found" }, entry);
  if (parts.length === 9 && req.method === "GET") return send(res, 200, versionOut(doc, ver), entry);
  if (parts.length === 10 && e === "content-url" && req.method === "GET") {
    const want = url.searchParams.get("disposition") === "inline" ? "inline" : "attachment";
    const disposition = want === "inline" && INLINE.has(ver.content_type) ? "inline" : "attachment";
    const exp = Date.now() + 5 * 60_000;
    return send(res, 200, {
      url: `${origin}/__store/${ver.id}?disp=${disposition}&exp=${exp}&sig=ok`,
      expires_at: new Date(exp).toISOString(),
      filename: ver.original_filename,
      content_type: ver.content_type,
      disposition,
    }, entry);
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
  if (url.pathname === "/__store_log") return json(storeLog);
  if (url.pathname === "/__seed") {
    const byTitle = {};
    for (const dd of documents.values()) byTitle[dd.title] = { id: dd.id, workspace_id: dd.workspace_id, versions: dd.versions.map((v) => v.id) };
    return json({
      orgs: { acme: acme.org.id, globex: globex.org.id },
      workspaces: { notes: notes, bench: globex.ws.bench.id, vault: globex.ws.vault.id },
      docs: byTitle,
      bench: benchDoc.id,
      vault: vaultDoc.id,
    });
  }
  if (url.pathname.startsWith("/__store/")) {
    // The "object store": no auth header needed, only the signed query (sig/exp), like a presigned URL.
    const vid = url.pathname.slice("/__store/".length);
    const blob = blobs.get(vid);
    const cors = { "access-control-allow-origin": "*" };
    storeLog.push({ version: vid, query: url.search, authorization: req.headers.authorization ?? null, cookie: req.headers.cookie ?? null });
    if (!blob || url.searchParams.get("sig") !== "ok" || Number(url.searchParams.get("exp")) < Date.now()) {
      res.writeHead(blob ? 403 : 404, cors);
      return res.end();
    }
    const disp = url.searchParams.get("disp") === "inline" ? "inline" : "attachment";
    res.writeHead(200, { ...cors, "content-type": blob.content_type, "content-disposition": disp, "x-content-type-options": "nosniff" });
    return res.end(blob.bytes);
  }
  handle(req, res, url).catch((err) => {
    console.error(err);
    res.writeHead(500, { "content-type": "application/json" });
    res.end(JSON.stringify({ detail: "stub_error" }));
  });
});

server.listen(port, "127.0.0.1", () => console.log(`documents stub-api listening on 127.0.0.1:${port}`));
