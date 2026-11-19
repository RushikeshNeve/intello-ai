// M02.03.02 / M02.03.03 end-to-end over HTTP (no browser): real Dex login, the built web app, and an in-memory
// stub of the contract m02 §5 API that also plays the object store. Run through ./run.sh.
import { test, before } from "node:test";
import assert from "node:assert/strict";

const WEB = process.env.WEB_URL ?? "http://127.0.0.1:23330";
const STUB = process.env.STUB_URL ?? "http://127.0.0.1:23331";
const ALICE = { login: "alice@ukos.test", password: "ukos-dev-alice-password" };
const SESSION_COOKIE = "ukos_session";
const TX_COOKIE = "ukos_auth_tx";
const JWT_RE = /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\./;

const bodies = []; // every response body the web app produced, for the "no tokens" sweep
let sessionValue = "";

function setCookies(res) {
  const out = {};
  for (const raw of res.headers.getSetCookie()) {
    const pair = raw.split(";")[0];
    const eq = pair.indexOf("=");
    out[pair.slice(0, eq)] = pair.slice(eq + 1);
  }
  return out;
}

async function web(path, { cookie, method = "GET", headers = {} } = {}) {
  const res = await fetch(path.startsWith("http") ? path : WEB + path, {
    method,
    redirect: "manual",
    headers: { ...(cookie ? { cookie: `${SESSION_COOKIE}=${cookie}` } : {}), ...headers },
  });
  const text = await res.text();
  bodies.push(text);
  return { res, text, html: text.replace(/<!-- -->/g, "") };
}

const unescapeHtml = (s) => s.replace(/&amp;/g, "&").replace(/&#43;/g, "+").replace(/&quot;/g, '"');

async function login() {
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
  let res = await fetch(formAction, {
    method: "POST",
    redirect: "manual",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(ALICE).toString(),
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
  const session = setCookies(cb)[SESSION_COOKIE];
  assert.ok(session, "session cookie set");
  return session;
}

const stubJson = async (p) => (await fetch(STUB + p)).json();
const rowTitles = (html) => [...html.matchAll(/id="doc-link-[^"]+"[^>]*>([^<]*)</g)].map((m) => m[1]);
const count = (html, id) => (html.match(new RegExp(`data-testid="${id}"`, "g")) ?? []).length;
const docListCalls = async (wsId) => (await stubJson("/__log")).filter((e) => e.method === "GET" && e.path.startsWith(`/v1/orgs/`) && e.path.includes(`/workspaces/${wsId}/documents`) && !/documents\/[0-9a-f-]{36}/.test(e.path));

let seed;
let session;
let LIB;
before(async () => {
  seed = await stubJson("/__seed");
  session = await login();
  sessionValue = session;
  LIB = "/o/acme/w/notes/documents";
});

test("signed out: the library sends the user to login (no data)", async () => {
  const r = await web(LIB);
  assert.equal(r.res.status, 307);
  assert.match(r.res.headers.get("location"), /\/api\/auth\/login\?returnTo=%2Fo%2Facme%2Fw%2Fnotes%2Fdocuments$/);
});

test("library renders the seeded rows on the server, with the default API request", async () => {
  const r = await web(LIB, { cookie: session });
  assert.equal(r.res.status, 200);
  assert.equal(count(r.html, "document-row"), 25, "first page = 25 rows (limit)");
  const titles = rowTitles(r.html);
  assert.deepEqual(titles.slice(0, 4), ["Quarterly report", "Meeting notes", "Logo", "Archive bundle"]);
  assert.match(r.html, /data-testid="documents-heading"[^>]*>Documents</);
  assert.match(r.html, /Page 1/);
  assert.match(r.html, /data-testid="upload-input"/, "can_upload=true renders the uploader");
  assert.match(r.html, /aria-sort="descending"[^>]*>(?:<button[^>]*>)?Updated/);
  // Row details: type label, size, versions count, row actions for an admin.
  assert.match(r.html, /aria-label="Rename Quarterly report"/);
  assert.match(r.html, /aria-label="Delete Quarterly report"/);
  const calls = await docListCalls(seed.workspaces.notes);
  const last = calls.at(-1);
  assert.equal(last.path.split("?")[1], "sort=updated_at&order=desc&limit=25");
  assert.equal(last.caller, "alice@ukos.test");
  assert.equal(last.items, 25);
  assert.ok(last.next_cursor, "a next cursor exists (34 documents, 25 per page)");
});

test("URL state: q / type / sort / order produce the matching API call and the matching HTML", async () => {
  const r = await web(`${LIB}?q=report&type=pdf&sort=title&order=asc`, { cookie: session });
  assert.deepEqual(rowTitles(r.html), ["Quarterly report"]);
  const last = (await docListCalls(seed.workspaces.notes)).at(-1);
  assert.equal(last.path.split("?")[1], "sort=title&order=asc&limit=25&q=report&content_type=application%2Fpdf");
  assert.match(r.html, /id="doc-search"[^>]*value="report"|value="report"[^>]*id="doc-search"/);
  assert.match(r.html, /<option value="pdf" selected="">/);
  assert.match(r.html, /<option value="title" selected="">/);
  assert.match(r.html, /aria-sort="ascending"[^>]*>(?:<button[^>]*>)?Title/);

  const text = await web(`${LIB}?type=text&sort=title&order=desc&q=bulk document 3`, { cookie: session });
  assert.deepEqual(rowTitles(text.html), ["Bulk document 30"]);
  const call = (await docListCalls(seed.workspaces.notes)).at(-1);
  assert.equal(call.path.split("?")[1], "sort=title&order=desc&limit=25&q=bulk+document+3&content_type=text%2F");

  const none = await web(`${LIB}?q=zzzz-no-such-doc`, { cookie: session });
  assert.equal(count(none.html, "document-row"), 0);
  assert.match(none.html, /data-testid="documents-no-results"/);
  assert.match(none.html, /Clear filters/);
});

test("URL state: images filter by prefix; the default order of title is ascending", async () => {
  const img = await web(`${LIB}?type=image`, { cookie: session });
  assert.deepEqual(rowTitles(img.html), ["Logo"]);
  assert.equal((await docListCalls(seed.workspaces.notes)).at(-1).path.split("?")[1], "sort=updated_at&order=desc&limit=25&content_type=image%2F");
  const t = await web(`${LIB}?sort=title`, { cookie: session });
  assert.equal((await docListCalls(seed.workspaces.notes)).at(-1).path.split("?")[1], "sort=title&order=asc&limit=25");
  assert.equal(rowTitles(t.html)[0], "Archive bundle");
});

test("keyset pagination: the cursor in the URL selects page 2; the stack gives the page number", async () => {
  await web(LIB, { cookie: session });
  const cursor = (await docListCalls(seed.workspaces.notes)).at(-1).next_cursor;
  assert.ok(cursor);
  const p2 = await web(`${LIB}?cursor=${cursor}`, { cookie: session });
  assert.equal(count(p2.html, "document-row"), 9, "34 documents: 25 + 9");
  assert.match(p2.html, /Page 2/);
  assert.ok(!rowTitles(p2.html).includes("Quarterly report"));
  const call = (await docListCalls(seed.workspaces.notes)).at(-1);
  assert.equal(call.path.split("?")[1], `sort=updated_at&order=desc&limit=25&cursor=${cursor}`);
  assert.equal(call.next_cursor, null);
  // Previous is enabled on page 2, Next is disabled on the last page.
  assert.doesNotMatch(p2.html, /<button[^>]*disabled=""[^>]*>Previous</);
  assert.match(p2.html, /<button[^>]*disabled=""[^>]*>Next</);
  const p3 = await web(`${LIB}?cursor=${cursor}&prev=${cursor}`, { cookie: session });
  assert.match(p3.html, /Page 3/);
});

test("invalid cursor: the API says 422 invalid_cursor; the page does not show rows or leak an error", async () => {
  const r = await web(`${LIB}?cursor=garbage`, { cookie: session });
  assert.equal(r.res.status, 200);
  const last = (await docListCalls(seed.workspaces.notes)).at(-1);
  assert.equal(last.status, 422);
  assert.equal(count(r.html, "document-row"), 0);
  assert.match(r.html, /data-testid="documents-loading"/, "client falls back to page 1 after hydration");
});

test("invalid query values are ignored (treated as defaults)", async () => {
  const r = await web(`${LIB}?type=zip&sort=bogus&order=sideways&cursor=%3Cscript%3E`, { cookie: session });
  assert.equal(count(r.html, "document-row"), 25);
  assert.equal((await docListCalls(seed.workspaces.notes)).at(-1).path.split("?")[1], "sort=updated_at&order=desc&limit=25");
});

test("unauthorized or unknown scope -> the calm not-found page, no documents, no documents API call", async () => {
  const before = (await stubJson("/__log")).length;
  for (const path of [
    "/o/globex/w/vault/documents",
    `/o/globex/w/vault/documents/${seed.vault}`,
    "/o/initech/w/plans/documents",
    "/o/no-such-org/w/x/documents",
    "/o/acme/w/no-such-ws/documents",
  ]) {
    const r = await web(path, { cookie: session });
    assert.match(r.html, /data-testid="scope-not-found"/, path);
    assert.match(r.html, /Not found or no access/, path);
    for (const leak of ["Vault Secret Doc", "secret.txt", "Initech", "document-row"]) assert.ok(!r.html.includes(leak), `${path} leaks ${leak}`);
  }
  const after = (await stubJson("/__log")).slice(before);
  assert.ok(!after.some((e) => e.path.includes(seed.workspaces.vault) && e.path.includes("/documents")), "never asked the API for the vault's documents");
});

test("viewer: renders the document, the version timeline (newest first) and the default (latest) version", async () => {
  const qr = seed.docs["Quarterly report"];
  const r = await web(`${LIB}/${qr.id}`, { cookie: session });
  assert.equal(r.res.status, 200);
  assert.match(r.html, /data-testid="document-title"[^>]*>Quarterly report</);
  assert.match(r.html, /data-testid="selected-version-meta"[^>]*>Version 3 · report-v3\.pdf/);
  const items = [...r.html.matchAll(/data-testid="version-item"[^>]*>(.*?)<\/a>/g)].map((m) => m[1].replace(/<[^>]+>/g, " ").replace(/\s+/g, " "));
  assert.equal(items.length, 3);
  assert.match(items[0], /Version 3.*Current.*report-v3\.pdf/);
  assert.match(items[1], /Version 2.*Superseded/);
  assert.match(items[2], /Version 1.*Superseded.*report-v1\.pdf/);
  assert.match(items[0], /sha256 [0-9a-f]{8}\b/);
  // Newest first, links carry ?v=, selected one is aria-current.
  assert.match(r.html, new RegExp(`href="${LIB}/${qr.id}\\?v=${qr.versions[2]}"[^>]*aria-current="true"|aria-current="true"[^>]*href="${LIB}/${qr.id}\\?v=${qr.versions[2]}"`));
  assert.match(r.html, /Upload new version/);
  assert.match(r.html, /aria-label="Preview"|data-testid="preview-region"/);
  assert.match(r.html, />Download</);
  assert.ok(!/data-testid="document-unavailable"/.test(r.html));
});

test("viewer: ?v= picks the version; unknown or other-document versions are 'not available'", async () => {
  const qr = seed.docs["Quarterly report"];
  const v1 = await web(`${LIB}/${qr.id}?v=${qr.versions[0]}`, { cookie: session });
  assert.match(v1.html, /data-testid="selected-version-meta"[^>]*>Version 1 · report-v1\.pdf/);

  const unknown = await web(`${LIB}/${qr.id}?v=00000000-0000-4000-8000-000000000000`, { cookie: session });
  assert.match(unknown.html, /data-testid="document-unavailable"/);
  assert.match(unknown.html, /This document isn(&#x27;|')t available/);
  assert.doesNotMatch(unknown.html, /data-testid="selected-version-meta"/);

  const other = seed.docs["Meeting notes"].versions[0];
  const wrongDoc = await web(`${LIB}/${qr.id}?v=${other}`, { cookie: session });
  assert.match(wrongDoc.html, /data-testid="document-unavailable"/, "a version of another document is never shown");
  assert.ok(!wrongDoc.html.includes("notes.md"));
});

test("viewer: a missing/deleted/foreign document id is the calm not-available page", async () => {
  for (const id of ["00000000-0000-4000-8000-000000000001", seed.bench, seed.vault]) {
    const r = await web(`${LIB}/${id}`, { cookie: session });
    assert.match(r.html, /data-testid="document-unavailable"/, id);
    for (const leak of ["Bench readout", "Vault Secret Doc", "readout.txt", "secret.txt", "data-testid=\"document-title\""]) assert.ok(!r.html.includes(leak), `${id} leaks ${leak}`);
  }
});

test("read-only caller (workspace viewer): documents render without write/manage actions or the uploader", async () => {
  const lib = await web("/o/globex/w/bench/documents", { cookie: session });
  assert.deepEqual(rowTitles(lib.html), ["Bench readout"]);
  assert.doesNotMatch(lib.html, /aria-label="Rename /);
  assert.doesNotMatch(lib.html, /aria-label="Delete /);
  assert.doesNotMatch(lib.html, /data-testid="upload-input"/);
  const v = await web(`/o/globex/w/bench/documents/${seed.bench}`, { cookie: session });
  assert.match(v.html, /data-testid="document-title"[^>]*>Bench readout</);
  assert.doesNotMatch(v.html, />Rename</);
  assert.doesNotMatch(v.html, />Delete</);
  assert.doesNotMatch(v.html, /Upload new version/);
});

test("content URL through the BFF: inline for the allow-list, forced attachment otherwise; bytes come from the store origin", async () => {
  const ws = `/api/v1/orgs/${seed.orgs.acme}/workspaces/${seed.workspaces.notes}/documents`;
  const get = async (title, i, disp) => {
    const d = seed.docs[title];
    const r = await web(`${ws}/${d.id}/versions/${d.versions[i]}/content-url?disposition=${disp}`, { cookie: session });
    assert.equal(r.res.status, 200, r.text);
    return JSON.parse(r.text);
  };
  const pdf = await get("Quarterly report", 2, "inline");
  assert.equal(pdf.disposition, "inline");
  assert.ok(pdf.url.startsWith(STUB + "/__store/"), "URL points at the object-store origin, not the web app");
  assert.ok(!pdf.url.startsWith(WEB));
  const bytes = await fetch(pdf.url);
  assert.equal(bytes.status, 200);
  assert.equal(bytes.headers.get("content-type"), "application/pdf");
  assert.equal(bytes.headers.get("content-disposition"), "inline");
  assert.match(Buffer.from(await bytes.arrayBuffer()).toString("latin1"), /^%PDF/);

  const zip = await get("Archive bundle", 0, "inline");
  assert.equal(zip.disposition, "attachment", "inline is refused for non-allow-listed types");
  const text = await get("Meeting notes", 0, "inline");
  assert.equal(text.disposition, "inline");
  assert.match(await (await fetch(text.url)).text(), /<script>window\.__pwned=1<\/script>/, "the store serves the raw bytes; the web app shows them as text only");

  // Nothing the browser sends to the store carries credentials.
  const storeLog = await stubJson("/__store_log");
  assert.ok(storeLog.length >= 2);
  assert.ok(storeLog.every((e) => e.authorization === null && e.cookie === null));

  // A version URL of a document the caller cannot see is a 404 through the BFF.
  const denied = await web(`/api/v1/orgs/${seed.orgs.globex}/workspaces/${seed.workspaces.vault}/documents/${seed.vault}/versions/x/content-url`, { cookie: session });
  assert.equal(denied.res.status, 404);
});

// ---- M03.03.04: processing status (library badges, viewer panel, SSE through the BFF, retry) ----
const PAPERS = "/o/acme/w/papers/documents";
const apiDoc = (ws, id) => `/api/v1/orgs/${seed.orgs[ws === seed.workspaces.bench ? "globex" : "acme"]}/workspaces/${ws}/documents/${id}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const post = (path, headers = { origin: WEB }) => fetch(WEB + path, { method: "POST", headers: { cookie: `${SESSION_COOKIE}=${session}`, ...headers } });

/** Reads SSE frames from a streaming response; `next()` resolves with the next frame or rejects after `ms`. */
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
      const chunk = await Promise.race([reader.read(), sleep(left).then(() => ({ timeout: true }))]);
      if (chunk.timeout) throw new Error("timed out waiting for an SSE frame");
      if (chunk.done) throw new Error("stream ended");
      buf += dec.decode(chunk.value, { stream: true });
    }
  }
  return { next, cancel: () => reader.cancel() };
}

test("library: server-rendered rows carry an accessible status badge (text, not colour only)", async () => {
  const r = await web(PAPERS, { cookie: session });
  assert.equal(count(r.html, "ingestion-badge"), 2);
  assert.match(r.html, /data-testid="ingestion-badge" data-state="queued"[^>]*>.*?Queued</s);
  assert.match(r.html, /data-state="failed"[^>]*>.*?Failed</s);
  assert.match(r.html, /<th[^>]*>Status<\/th>/);
  const notes = await web(LIB, { cookie: session });
  assert.equal(count(notes.html, "ingestion-badge"), 25);
  assert.match(notes.html, /data-state="ready"[^>]*>.*?Ready</s);
});

test("viewer: failed document shows a human message and Retry; raw codes and internal ids never reach the page", async () => {
  const broken = seed.docs["Broken PDF"];
  const r = await web(`${PAPERS}/${broken.id}`, { cookie: session });
  assert.match(r.html, /data-testid="processing-panel"/);
  assert.match(r.html, /The PDF could not be read/);
  assert.match(r.html, /aria-live="polite"/);
  assert.match(r.html, />Retry</);
  // Visible text only: the RSC payload in <script> tags carries the stable error code as a prop, the user never sees it.
  const visible = r.html.replace(/<script[\s\S]*?<\/script>/g, "").replace(/<[^>]+>/g, " ");
  assert.doesNotMatch(visible, /pdf_unreadable|Traceback|00000000-0000-4000-8000-0000000000aa/);
  // The timeline / steps render as lists.
  assert.match(r.html, /data-testid="processing-steps"/);
});

test("viewer: a read-only caller sees the failure but no Retry button, and the API refuses a forged retry (403)", async () => {
  const v = await web(`/o/globex/w/bench/documents/${seed.bench}`, { cookie: session });
  assert.match(v.html, /data-testid="processing-failure"/);
  assert.match(v.html, /search index was unavailable/);
  assert.doesNotMatch(v.html, />Retry</);
  const forged = await post(`${apiDoc(seed.workspaces.bench, seed.bench)}/ingestion/retry`);
  assert.equal(forged.status, 403);
  assert.equal(await forged.text(), JSON.stringify({ detail: "forbidden" }));
  const st = await stubJson("/__ingest/state");
  assert.equal(st.docs[seed.bench].state, "failed", "the read-only retry changed nothing");
});

test("SSE through the BFF: streams unbuffered (snapshot, then job events with ids), tears down on client abort", async () => {
  const pipe = seed.docs["Pipeline demo"];
  const res = await fetch(WEB + `${apiDoc(seed.workspaces.papers, pipe.id)}/ingestion/events`, {
    headers: { cookie: `${SESSION_COOKIE}=${session}`, accept: "text/event-stream" },
  });
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type"), /^text\/event-stream/);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.equal(res.headers.get("set-cookie"), null);
  const sse = sseReader(res);
  const hello = await sse.next();
  assert.equal(hello.data, "", "first frame is `retry: 3000`");
  const snap = await sse.next();
  assert.equal(snap.event, "snapshot");
  assert.equal(JSON.parse(snap.data).ingestion.state, "queued");
  const seen = [];
  for (let i = 0; i < 6; i++) {
    await stubJson(`/__ingest/step?doc=${pipe.id}`); // each step must arrive before the next is triggered: not buffered
    const f = await sse.next();
    assert.equal(f.event, "job");
    assert.match(f.id, /^\d+$/);
    seen.push(JSON.parse(f.data));
  }
  assert.deepEqual(seen.map((e) => [e.ingestion.state, e.ingestion.stage]), [["processing", "extract"], ["processing", "chunk"], ["processing", "embed"], ["processing", "index"], ["processing", "publish"], ["ready", null]]);
  const end = await sse.next();
  assert.equal(end.event, "end");
  assert.equal(JSON.parse(end.data).reason, "terminal");
  await sse.cancel();
  for (let i = 0; i < 20 && (await stubJson("/__ingest/state")).streams !== 0; i++) await sleep(100);
  assert.equal((await stubJson("/__ingest/state")).streams, 0, "the stream is closed after `end`");
});

test("SSE through the BFF: the browser's Last-Event-ID header reaches the API; aborting the client closes the upstream stream", async () => {
  const pipe = seed.docs["Pipeline demo"];
  const brokenDoc = seed.docs["Broken PDF"];
  await stubJson(`/__ingest/fail?doc=${brokenDoc.id}&code=pdf_unreadable`);
  await post(`${apiDoc(seed.workspaces.papers, brokenDoc.id)}/ingestion/retry`); // 409 or 202, either way active-or-terminal; make it active:
  const ctl = new AbortController();
  const res = await fetch(WEB + `${apiDoc(seed.workspaces.papers, brokenDoc.id)}/ingestion/events`, {
    headers: { cookie: `${SESSION_COOKIE}=${session}`, accept: "text/event-stream", "last-event-id": "41" },
    signal: ctl.signal,
  });
  assert.equal(res.status, 200);
  const sse = sseReader(res);
  await sse.next(); // retry:
  assert.equal((await sse.next()).event, "snapshot");
  const log = (await stubJson("/__log")).filter((e) => e.path.endsWith("/ingestion/events") && e.path.includes(brokenDoc.id));
  assert.equal(log.at(-1).last_event_id, "41");
  assert.equal((await stubJson("/__ingest/state")).streams, 1);
  ctl.abort();
  for (let i = 0; i < 20 && (await stubJson("/__ingest/state")).streams !== 0; i++) await sleep(100);
  assert.equal((await stubJson("/__ingest/state")).streams, 0, "aborting the browser request closes the upstream stream (no leak)");
  // leave the document failed again for the following tests
  await stubJson(`/__ingest/fail?doc=${brokenDoc.id}&code=pdf_unreadable`);
  void pipe;
});

test("SSE and retry are refused without a session (401) and cross-origin retry is refused (403)", async () => {
  const pipe = seed.docs["Pipeline demo"];
  const sseNoAuth = await fetch(WEB + `${apiDoc(seed.workspaces.papers, pipe.id)}/ingestion/events`, { headers: { accept: "text/event-stream" } });
  assert.equal(sseNoAuth.status, 401);
  await sseNoAuth.text();
  const broken = seed.docs["Broken PDF"];
  const evil = await post(`${apiDoc(seed.workspaces.papers, broken.id)}/ingestion/retry`, { origin: "http://evil.invalid" });
  assert.equal(evil.status, 403);
  await evil.text();
  assert.equal((await stubJson("/__ingest/state")).docs[broken.id].state, "failed");
});

test("retry through the BFF: 202 queues a new job, a second retry is 409, the status endpoint follows", async () => {
  const broken = seed.docs["Broken PDF"];
  const path = apiDoc(seed.workspaces.papers, broken.id);
  const ok = await post(`${path}/ingestion/retry`);
  assert.equal(ok.status, 202);
  assert.match((await ok.json()).job_id, /^[0-9a-f-]{36}$/);
  const again = await post(`${path}/ingestion/retry`);
  assert.equal(again.status, 409);
  await again.text();
  const status = await web(`${path}/ingestion`, { cookie: session });
  const body = JSON.parse(status.text);
  assert.equal(body.state, "queued");
  assert.ok(body.events.length >= 2);
  // The library now shows the retried document as queued.
  const lib = await web(PAPERS, { cookie: session });
  assert.equal([...lib.html.matchAll(/data-state="queued"/g)].length, 1, "Pipeline demo is already indexed by the SSE test; only the retried document is queued");
  // Drive it to a terminal state so later runs/pages are stable: processing -> failed with an unknown code.
  await stubJson(`/__ingest/step?doc=${broken.id}`);
  await stubJson(`/__ingest/fail?doc=${broken.id}&code=db_exploded_at_row_17`);
  const v = await web(`${PAPERS}/${broken.id}`, { cookie: session });
  assert.match(v.html, /Processing failed./);
  assert.doesNotMatch(v.html.replace(/<script[\s\S]*?<\/script>/g, ""), /db_exploded/, "an unknown code is never shown");
});

test("no tokens, session ids or client secrets in any response the web app produced", () => {
  assert.ok(bodies.length > 20);
  for (const b of bodies) {
    assert.doesNotMatch(b, JWT_RE, "no JWT in a response body");
    assert.ok(!b.includes(sessionValue), "session cookie value never echoed");
    assert.ok(!b.includes("change-me-oidc-client-secret"));
  }
});
