// M08.02.03 load + fault injection against a running full stack (see tests/load/run.sh). Node 20+, no deps.
// Phases (PHASES="upload,search,chat,redis,postgres,embedder", default all, in this order):
//   upload    burst of N small documents (concurrency C): upload+finalize latency, queue drain time, finalize->ready latency
//   search    N hybrid searches at concurrency C (two steps)
//   chat      N chat runs through the scripted chat stub (threads + run + poll): latency, outcome states
//   redis     restart Redis while documents are ingesting and searches run
//   postgres  restart PostgreSQL while documents are ingesting and searches run
//   embedder  stop the embedding server while documents are ingesting (retries), start it again
// After every phase the integrity check runs: every uploaded document has exactly one published revision, the job
// and outbox state is clean, and RushDB holds exactly the chunks PostgreSQL says it should (no loss, no duplicates).
// Writes docs/ops/load-results.md (machine description from MACHINE_DESC) and .data/load/results.json.
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";
import {
  PROJECT, login, newTenant, uploadFile, waitFor, textDoc, search, chatRun, runPool, summarize, fmt, sleep, sql, sh, web, ingestionState, suffix,
} from "./lib/client.mjs";
import { startAiStubs } from "./lib/ai-stubs.mjs";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const N = (name, dflt) => Number(process.env[name] ?? dflt);
const PHASES = (process.env.PHASES ?? "upload,search,chat,redis,postgres,embedder").split(",");
const RUSHDB_URL = process.env.RUSHDB_HOST_URL ?? "http://127.0.0.1:33372";
const RUSHDB_TOKEN = process.env.RUSHDB_TOKEN ?? "change-me-rushdb-token";
const report = { started: new Date().toISOString(), phases: {}, integrity: {} };
const log = (m) => console.log(`[${new Date().toISOString().slice(11, 19)}] ${m}`);
const docker = (a) => sh(`docker ${a}`);
const ctr = (s) => `${PROJECT}-${s}-1`;

const ai = await startAiStubs();
const alice = await login("alice");
const t = await newTenant(alice, "load");
const docs = []; // every uploaded document, for the integrity check
let docCounter = 0;
const mkDoc = (tag) => textDoc(`ld${tag}${suffix}x${docCounter++}`, 3);

// ---- helpers ---------------------------------------------------------------------------------------------
async function uploadMany(count, concurrency, tag) {
  const finished = [];
  const stats = await runPool(count, concurrency, async () => {
    const t0 = performance.now();
    const up = await uploadFile(t, { filename: `${tag}-${docCounter}.txt`, bytes: mkDoc(tag) });
    docs.push(up);
    finished.push({ ...up, t0: performance.now(), startMs: t0 });
    return { ok: true, ms: performance.now() - t0 };
  });
  return { stats, finished };
}

/** Wait until the given documents are all terminal; returns per-document finalize->ready latencies in ms. */
async function drain(finished, timeout = 600_000) {
  const t0 = performance.now();
  const lat = [];
  const pending = new Map(finished.map((f) => [f.docId, f]));
  const states = {};
  await waitFor(async () => {
    for (const [id, f] of [...pending]) {
      const r = await ingestionState(t, id);
      if (r.status === 200 && ["ready", "failed", "unsupported", "no_text", "cancelled"].includes(r.body.state)) {
        states[r.body.state] = (states[r.body.state] ?? 0) + 1;
        lat.push(performance.now() - f.t0);
        pending.delete(id);
      }
    }
    return pending.size === 0;
  }, { timeout, interval: 1000, what: `${pending.size} documents to finish` });
  return { drainSeconds: (performance.now() - t0) / 1000, latency: summarize(lat), states };
}

async function rushdbChunks(orgId) {
  const out = sh(`node ${ROOT}scripts/ops/rushdb.mjs snapshot ${RUSHDB_URL} ${RUSHDB_TOKEN} ${orgId}`);
  return JSON.parse(out)[orgId];
}

async function integrity(label) {
  const orgId = t.orgId;
  const rows = sql(`SELECT count(*) FILTER (WHERE state='published'), coalesce(sum(chunk_count) FILTER (WHERE state='published'),0), count(DISTINCT document_id) FROM knowledge_revisions WHERE org_id='${orgId}'`).split("|");
  const jobs = sql(`SELECT state || '=' || count(*) FROM ingestion_jobs WHERE org_id='${orgId}' GROUP BY state ORDER BY 1`).split("\n").filter(Boolean);
  const undispatched = Number(sql(`SELECT count(*) FROM outbox_events WHERE org_id='${orgId}' AND dispatched_at IS NULL AND event_type='ingestion.job.queued'`));
  const dupJobs = Number(sql(`SELECT count(*) FROM (SELECT revision_id FROM ingestion_jobs WHERE org_id='${orgId}' GROUP BY revision_id HAVING count(*) > 1) x`));
  const dupPub = Number(sql(`SELECT count(*) FROM (SELECT document_id FROM knowledge_revisions WHERE org_id='${orgId}' AND state='published' GROUP BY document_id HAVING count(*) > 1) x`));
  const rdb = await rushdbChunks(orgId);
  const [published, chunkSum, docCount] = rows.map(Number);
  const ok = published === docs.length && docCount === docs.length && rdb.chunks === chunkSum && rdb.published === chunkSum && dupJobs === 0 && dupPub === 0 && undispatched === 0;
  report.integrity[label] = { documents: docs.length, published_revisions: published, postgres_chunks: chunkSum, rushdb_chunks: rdb.chunks, rushdb_published: rdb.published, duplicate_jobs: dupJobs, duplicate_published: dupPub, undispatched_outbox: undispatched, jobs, ok };
  log(`integrity after ${label}: ${ok ? "OK" : "FAILED"} ${JSON.stringify(report.integrity[label])}`);
  return ok;
}

const searchTask = (q) => async () => {
  const r = await search(t, q, 5);
  return { ok: r.status === 200 && (r.body?.results?.length ?? 0) >= 0, ms: r.ms, status: r.status, degraded: (r.body?.degraded ?? []).length > 0 };
};

/** Background searches until stop() is called; records per-request status and time offset from the start. */
function backgroundSearches(query, concurrency = 4) {
  const log2 = [];
  let running = true;
  const t0 = performance.now();
  const loops = Array.from({ length: concurrency }, async () => {
    while (running) {
      try { const r = await search(t, query, 3); log2.push({ at: performance.now() - t0, status: r.status, ms: r.ms, degraded: (r.body?.degraded ?? []).length > 0 }); }
      catch { log2.push({ at: performance.now() - t0, status: 0, ms: 0 }); }
      await sleep(100);
    }
  });
  return { stop: async () => { running = false; await Promise.all(loops); return log2; }, t0 };
}

function faultSummary(entries, faultStartMs, faultEndMs) {
  const during = entries.filter((e) => e.at >= faultStartMs && e.at <= faultEndMs);
  const bad = entries.filter((e) => e.status !== 200);
  const firstOkAfter = entries.find((e) => e.at >= faultEndMs && e.status === 200);
  return { requests: entries.length, non200_total: bad.length, non200_during_fault: during.filter((e) => e.status !== 200).length,
    statuses: [...new Set(bad.map((e) => e.status))], degraded_200: entries.filter((e) => e.status === 200 && e.degraded).length,
    recovery_seconds_after_fault_end: firstOkAfter ? (firstOkAfter.at - faultEndMs) / 1000 : null };
}

// ---- phases ------------------------------------------------------------------------------------------------
const phases = {
  async upload() {
    const count = N("UPLOAD_COUNT", 60), conc = N("UPLOAD_CONCURRENCY", 12);
    log(`upload burst: ${count} documents, concurrency ${conc}`);
    const t0 = performance.now();
    const { stats, finished } = await uploadMany(count, conc, "burst");
    const accepted = (performance.now() - t0) / 1000;
    const drained = await drain(finished);
    report.phases.upload = { count, concurrency: conc, uploadAndFinalize: stats, secondsToAcceptAll: accepted, queueDrainSecondsAfterLastAccept: drained.drainSeconds,
      totalSeconds: accepted + drained.drainSeconds, finalizeToReady: drained.latency, finalStates: drained.states,
      docsPerMinuteEndToEnd: count / ((accepted + drained.drainSeconds) / 60) };
    await integrity("upload");
  },

  async search() {
    // the corpus from the upload phase (or a small one when upload was skipped)
    if (!docs.length) { const { finished } = await uploadMany(10, 5, "seed"); await drain(finished); }
    const word = (i) => ["alpha0", "beta7 gamma11", "section 1", "delta", "ld"][i % 5];
    const out = {};
    for (const [label, total, conc] of [["c10", N("SEARCH_COUNT", 300), 10], ["c40", N("SEARCH_COUNT", 300), 40]]) {
      log(`search: ${total} requests, concurrency ${conc}`);
      const stats = await runPool(total, conc, async (i) => { const r = await searchTask(word(i))(); return { ...r, ok: r.ok }; });
      out[label] = stats;
    }
    report.phases.search = out;
  },

  async chat() {
    if (!docs.length) { const { finished } = await uploadMany(10, 5, "seed"); await drain(finished); }
    const total = N("CHAT_COUNT", 40), conc = N("CHAT_CONCURRENCY", 10);
    log(`chat: ${total} runs, concurrency ${conc}`);
    const states = {};
    const stats = await runPool(total, conc, async (i) => {
      const r = await chatRun(t, `What does section ${i % 3} say about alpha?`);
      states[r.state] = (states[r.state] ?? 0) + 1;
      return { ok: r.state === "completed", ms: r.ms };
    });
    report.phases.chat = { ...stats, states, stubStats: await ai.chatStats() };
  },

  async redis() {
    log("fault: redis restart during ingestion + searches");
    const bg = backgroundSearches("alpha0");
    const { finished } = await uploadMany(N("FAULT_DOCS", 12), 6, "redis");
    await sleep(1500);
    const f0 = performance.now() - bg.t0;
    docker(`restart ${ctr("redis")}`);
    await waitFor(() => docker(`inspect -f {{.State.Health.Status}} ${ctr("redis")}`) === "healthy", { timeout: 60_000, interval: 500, what: "redis healthy" });
    const f1 = performance.now() - bg.t0;
    const drained = await drain(finished);
    const entries = await bg.stop();
    report.phases.redis = { redisDownSeconds: (f1 - f0) / 1000, drain: drained, searches: faultSummary(entries, f0, f1) };
    await integrity("redis");
  },

  async postgres() {
    log("fault: postgres restart during ingestion + searches");
    const bg = backgroundSearches("alpha0");
    const { finished } = await uploadMany(N("FAULT_DOCS", 12), 6, "pg");
    await sleep(1500);
    const f0 = performance.now() - bg.t0;
    docker(`restart ${ctr("postgres")}`);
    await waitFor(() => docker(`inspect -f {{.State.Health.Status}} ${ctr("postgres")}`) === "healthy", { timeout: 120_000, interval: 500, what: "postgres healthy" });
    const f1 = performance.now() - bg.t0;
    const drained = await drain(finished, 900_000);
    const entries = await bg.stop();
    report.phases.postgres = { postgresDownSeconds: (f1 - f0) / 1000, drain: drained, searches: faultSummary(entries, f0, f1) };
    await integrity("postgres");
  },

  async embedder() {
    log("fault: embedder down while documents ingest (and searches run)");
    await ai.stopEmbedder();
    const bg = backgroundSearches("alpha0");
    const f0 = performance.now() - bg.t0;
    const { finished } = await uploadMany(N("FAULT_DOCS", 12), 6, "emb");
    await sleep(N("EMBEDDER_DOWN_SECONDS", 25) * 1000);
    const stuck = Number(sql(`SELECT count(*) FROM ingestion_jobs WHERE org_id='${t.orgId}' AND state IN ('queued','running')`));
    const before = [];
    for (const f of finished) before.push((await ingestionState(t, f.docId)).body?.state);
    await ai.startEmbedder();
    const f1 = performance.now() - bg.t0;
    const drained = await drain(finished, 600_000);
    const entries = await bg.stop();
    report.phases.embedder = { downSeconds: (f1 - f0) / 1000, jobsStillActiveWhileDown: stuck, statesWhileDown: before.reduce((m, s) => ({ ...m, [s]: (m[s] ?? 0) + 1 }), {}), drain: drained, searches: faultSummary(entries, f0, f1) };
    await integrity("embedder");
  },
};

let failed = false;
try {
  for (const p of PHASES) { log(`=== phase ${p}`); await phases[p](); }
  failed = Object.values(report.integrity).some((i) => !i.ok);
} catch (e) {
  failed = true;
  report.error = String(e.stack ?? e).slice(0, 1500);
  console.error(report.error);
} finally {
  await ai.stop();
}

// ---- output ----------------------------------------------------------------------------------------------------
const l = (s) => (s ? `${fmt(s.p50)} / ${fmt(s.p95)} / ${fmt(s.p99)}` : "-");
const md = [];
md.push("# Load and fault-injection results (M08.02.03)\n");
md.push(`Run: ${report.started} (UTC). Generated by \`bash tests/load/run.sh\`; numbers below are from that run, unedited.\n`);
md.push("## Machine and setup\n");
md.push(process.env.MACHINE_DESC ?? "(machine description not provided)");
md.push("\nStack: full compose stack in the isolated project `" + PROJECT + "` (postgres 17, redis 7.4, RushDB 3.0.10, RustFS, Dex, api, web BFF, 1 worker with concurrency 2, 1 dispatcher). Embeddings and chat are the host stubs (`tests/e2e/support/embedding-stub.mjs`, `chat-stub.mjs`, 768 dimensions, scripted answer), so latencies exclude real model time. Per-user rate limits were raised for the run (`SEARCH_RATE_PER_MIN`, `CHAT_MAX_RUNS_PER_MIN`, `CHAT_MAX_CONCURRENT_RUNS`) because the test measures the system, not the limiter. The load generator runs on the same machine, so it competes for CPU with the stack: treat the numbers as a floor for this hardware, not a capacity claim. All requests go through the web BFF (cookie session), like a browser. Latencies are milliseconds, shown as p50 / p95 / p99.\n");
const ph = report.phases;
if (ph.upload) {
  const u = ph.upload;
  md.push("## Upload burst and queue drain\n");
  md.push(`${u.count} text documents (3 paragraphs each), ${u.concurrency} concurrent uploads (create session, PUT to the object store, finalize).\n`);
  md.push(`- upload + finalize latency: ${l(u.uploadAndFinalize.latency)} ms; errors ${u.uploadAndFinalize.errors}/${u.count}; accepted all in ${u.secondsToAcceptAll.toFixed(1)} s`);
  md.push(`- queue drain after the last accepted upload: ${u.queueDrainSecondsAfterLastAccept.toFixed(1)} s; finalize -> ready per document: ${l(u.finalizeToReady)} ms`);
  md.push(`- end to end: ${u.totalSeconds.toFixed(1)} s, ${u.docsPerMinuteEndToEnd.toFixed(1)} documents/minute (worker concurrency 2); final states ${JSON.stringify(u.finalStates)}\n`);
}
if (ph.search) {
  md.push("## Concurrent searches\n");
  md.push("| concurrency | requests | errors | error rate | throughput (req/s) | latency ms p50 / p95 / p99 |\n|---|---|---|---|---|---|");
  for (const [k, s] of Object.entries(ph.search)) md.push(`| ${s.concurrency} | ${s.total} | ${s.errors} | ${(s.errorRate * 100).toFixed(2)}% | ${s.throughput.toFixed(1)} | ${l(s.latency)} |`);
  md.push("");
}
if (ph.chat) {
  const c = ph.chat;
  md.push("## Chat runs (scripted chat stub)\n");
  md.push(`${c.total} runs, concurrency ${c.concurrency}, each: create thread, start run, poll until terminal. Latency to \`completed\`: ${l(c.latency)} ms; terminal states ${JSON.stringify(c.states)}; error rate (not completed) ${(c.errorRate * 100).toFixed(2)}%; throughput ${c.throughput.toFixed(2)} runs/s.\n`);
}
const faultMd = (name, title, extra) => {
  const f = ph[name];
  if (!f) return;
  md.push(`## Fault: ${title}\n`);
  md.push(extra(f));
  md.push(`- searches running in the background: ${f.searches.requests} requests, ${f.searches.non200_total} not 200 (statuses ${JSON.stringify(f.searches.statuses)}), ${f.searches.non200_during_fault} of them during the fault window; degraded-but-200 answers ${f.searches.degraded_200}; first success ${f.searches.recovery_seconds_after_fault_end == null ? "n/a" : f.searches.recovery_seconds_after_fault_end.toFixed(1) + " s"} after the fault ended`);
  md.push(`- ingestion of the documents uploaded around the fault: all terminal in ${f.drain.drainSeconds.toFixed(1)} s after the fault ended, states ${JSON.stringify(f.drain.states)}, finalize -> ready ${l(f.drain.latency)} ms\n`);
};
faultMd("redis", "Redis restart", (f) => `- Redis was down/unhealthy for ${f.redisDownSeconds.toFixed(1)} s (docker restart until healthy).`);
faultMd("postgres", "PostgreSQL restart", (f) => `- PostgreSQL was down/unhealthy for ${f.postgresDownSeconds.toFixed(1)} s (docker restart until healthy).`);
faultMd("embedder", "embedding server down", (f) => `- Embedder stopped for ${f.downSeconds.toFixed(1)} s while documents were uploaded; jobs still queued/running while it was down: ${f.jobsStillActiveWhileDown}; document states while down ${JSON.stringify(f.statesWhileDown)}.`);
md.push("## Integrity after each phase (no data loss, no duplicates)\n");
md.push("| after | documents | published revisions | PostgreSQL chunks | RushDB chunks | duplicate jobs | duplicate published | undispatched outbox | verdict |\n|---|---|---|---|---|---|---|---|---|");
for (const [k, i] of Object.entries(report.integrity)) md.push(`| ${k} | ${i.documents} | ${i.published_revisions} | ${i.postgres_chunks} | ${i.rushdb_chunks} | ${i.duplicate_jobs} | ${i.duplicate_published} | ${i.undispatched_outbox} | ${i.ok ? "OK" : "FAILED"} |`);
if (report.error) md.push(`\n**Run error:** \`${report.error.split("\n")[0]}\``);
md.push("\n## Reading the numbers\n\nThere are no pass/fail thresholds except integrity: every accepted document ends with exactly one published revision, RushDB holds exactly the chunks PostgreSQL records, no job or revision is duplicated and nothing stays undispatched. Faults that M03 already covers (worker kill mid-embed, dispatcher kill, RushDB stop, lease expiry, reconcile) are in `tests/e2e/fullstack_m03/` and are not repeated here.\n");
await mkdir(`${ROOT}.data/load`, { recursive: true });
await writeFile(`${ROOT}.data/load/results.json`, JSON.stringify(report, null, 2));
if (process.env.WRITE_DOCS !== "0") { await mkdir(`${ROOT}docs/ops`, { recursive: true }); await writeFile(`${ROOT}docs/ops/load-results.md`, md.join("\n") + "\n"); }
console.log(md.join("\n"));
process.exit(failed ? 1 : 0);
