// M08.02.04 integration proof: ONE trace id, minted by the API for a finalize request, shows up in the API log, the
// dispatcher log and the worker log of the ingestion job that request created; the request id sent by the client
// is the same in all three; and no document text, token or org id appears in those logs. Requires the full stack
// (tests/load/run.sh trace). The web BFF forwards `x-request-id` (allow-list), not `traceparent`.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { PROJECT, login, newTenant, web, waitReady, sh, sha256hex, sleep, textDoc } from "./lib/client.mjs";
import { startAiStubs } from "./lib/ai-stubs.mjs";

const ai = await startAiStubs();
let code = 0;
try {
  const session = await login("alice");
  const t = await newTenant(session, "trace");
  const marker = `trc${randomBytes(3).toString("hex")}`;
  const secretText = `TRACE-CANARY ${marker}`;
  const bytes = Buffer.from(`${secretText}\n\n${textDoc(marker, 2).toString()}`);
  const requestId = `trace-test-${randomBytes(6).toString("hex")}`;
  const hdr = { "x-request-id": requestId };

  const created = await web(`${t.wsPath}/uploads`, { session, method: "POST", headers: hdr,
    json: { filename: `${marker}.txt`, content_type: "text/plain", size_bytes: bytes.length, sha256: sha256hex(bytes) } });
  assert.equal(created.status, 201, created.text);
  assert.equal(created.headers.get("x-request-id"), requestId, "the API echoes the client's request id");
  const put = await fetch(created.body.upload.url, { method: created.body.upload.method, headers: created.body.upload.headers, body: bytes });
  assert.equal(put.status, 200);
  const fin = await web(`${t.wsPath}/uploads/${created.body.upload_id}/finalize`, { session, method: "POST", headers: hdr });
  assert.equal(fin.status, 201, fin.text);
  const responseTrace = fin.headers.get("traceparent");
  assert.match(responseTrace ?? "", /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/, "the API answers with a traceparent");
  const traceId = responseTrace.split("-")[1];
  const ready = await waitReady(t, fin.body.document.id);
  assert.equal(ready.state, "ready");
  await sleep(2000); // let the log pipes flush

  const logs = (svc) => sh(`docker logs ${PROJECT}-${svc}-1 2>&1`, { maxBuffer: 256 * 1024 * 1024 })
    .split("\n").filter((l) => l.startsWith("{")).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  const apiLines = logs("api").filter((l) => l.trace_id === traceId);
  const dispLines = logs("dispatcher").filter((l) => l.trace_id === traceId);
  const workerLines = logs("worker").filter((l) => l.trace_id === traceId);
  console.log(`trace ${traceId}: api ${apiLines.length} line(s), dispatcher ${dispLines.length}, worker ${workerLines.length}`);
  assert.ok(apiLines.some((l) => l.msg === "request" && l.route.endsWith("/finalize") && l.status === 201), "API request line for finalize");
  assert.ok(dispLines.some((l) => l.msg === "dispatched" && l.task === "ingest.run_job"), "dispatcher line for the job");
  assert.ok(workerLines.some((l) => l.msg === "task finished" && l.task === "ingest.run_job" && l.status === "SUCCESS"), "worker task line");
  for (const l of [...apiLines, ...dispLines, ...workerLines]) assert.equal(l.request_id, requestId, `request id on ${l.service} line "${l.msg}"`);
  assert.ok(workerLines.every((l) => l.job_id === undefined || /^[0-9a-f-]{36}$/.test(l.job_id)));
  assert.ok(workerLines.some((l) => l.job_id), "worker lines carry the job id");

  // the logs never carry content, tokens or the raw org id
  for (const svc of ["api", "dispatcher", "worker"]) {
    const text = sh(`docker logs ${PROJECT}-${svc}-1 2>&1`, { maxBuffer: 256 * 1024 * 1024 });
    for (const bad of [secretText, marker, t.orgId, "X-Amz-Signature", "eyJ"]) {
      const hit = text.split(/\r?\n/).find((l) => l.includes(bad));
      assert.ok(!hit, `${svc} log must not contain ${bad.slice(0, 12)}...: ${(hit ?? "").slice(0, 300)}`);
    }
  }
  console.log("TRACE PROPAGATION OK: api -> outbox -> dispatcher -> celery -> worker share one trace id and request id; logs contain no content, org id or token");
} catch (e) {
  console.error(e);
  code = 1;
} finally {
  await ai.stop();
}
process.exit(code);
