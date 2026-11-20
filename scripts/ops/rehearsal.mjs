#!/usr/bin/env node
// Application-level half of the restore rehearsal (M08.02.02), over the real web BFF + API.
//   node scripts/ops/rehearsal.mjs seed   <state.json>   two tenants (alice, bob), text documents, wait for "ready"
//   node scripts/ops/rehearsal.mjs verify <state.json>   on the RESTORED stack: sign in again, per tenant
//        - document list size and ingestion state "ready" for every document
//        - search finds the seeded document at rank 1 (hybrid: keyword + vector through RushDB)
//        - the citation (chunk evidence) of that hit resolves and carries the seeded marker
//        - a graph query (entities) answers 200 (GRAPH_ENABLED is off by default: an empty list is correct)
//        - cross-tenant: the other user gets a concealed 404 on the org
import { readFile, writeFile } from "node:fs/promises";
import { login, newTenant, uploadFile, waitReady, web, search, textDoc, suffix } from "../../tests/load/lib/client.mjs";

const [cmd, stateFile] = process.argv.slice(2);
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok }); console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail ? ` (${detail})` : ""}`); };

if (cmd === "seed") {
  const sessions = { alice: await login("alice"), bob: await login("bob") };
  const state = { tenants: [] };
  for (const [who, tag, n] of [["alice", "rehearsal-a", 5], ["bob", "rehearsal-b", 3]]) {
    const t = await newTenant(sessions[who], tag);
    const docs = [];
    for (let i = 0; i < n; i++) {
      const marker = `zq${tag.replace("-", "")}${suffix}doc${i}`;
      const up = await uploadFile(t, { filename: `${marker}.txt`, bytes: textDoc(marker, 3), title: `Doc ${i} ${tag}` });
      docs.push({ ...up, marker });
    }
    for (const d of docs) { const s = await waitReady(t, d.docId); if (s.state !== "ready") throw new Error(`doc ${d.docId} ended ${s.state}`); }
    state.tenants.push({ who, orgId: t.orgId, wsId: t.wsId, docs });
    console.log(`seeded ${tag}: org ${t.orgId}, ${n} documents ready`);
  }
  await writeFile(stateFile, JSON.stringify(state, null, 2));
} else if (cmd === "verify") {
  const state = JSON.parse(await readFile(stateFile, "utf8"));
  const sessions = { alice: await login("alice"), bob: await login("bob") };
  for (const tn of state.tenants) {
    const t = { orgId: tn.orgId, wsId: tn.wsId, wsPath: `/api/v1/orgs/${tn.orgId}/workspaces/${tn.wsId}`, session: sessions[tn.who] };
    const list = await web(`${t.wsPath}/documents?limit=100`, { session: t.session });
    const items = list.body?.items ?? list.body?.documents ?? list.body ?? [];
    check(`${tn.who}: document list has ${tn.docs.length} documents`, list.status === 200 && items.length === tn.docs.length, `status ${list.status}, got ${items.length}`);
    for (const d of tn.docs) {
      const s = await web(`${t.wsPath}/documents/${d.docId}/ingestion`, { session: t.session });
      if (s.body?.state !== "ready") check(`${tn.who}: ingestion state of ${d.docId}`, false, String(s.body?.state));
    }
    const probe = tn.docs[tn.docs.length - 1];
    const res = await search(t, probe.marker, 5);
    const top = res.body?.results?.[0];
    check(`${tn.who}: search finds the seeded document at rank 1`, res.status === 200 && top?.document_id === probe.docId, `status ${res.status}, degraded=${JSON.stringify(res.body?.degraded)}`);
    const chunkId = top?.snippets?.[0]?.chunk_id;
    const cite = top && chunkId ? await web(`${t.wsPath.replace(`/workspaces/${tn.wsId}`, "")}/workspaces/${top.workspace_id}/documents/${top.document_id}/versions/${top.version_id}/chunks/${chunkId}`, { session: t.session }) : { status: 0, text: "" };
    check(`${tn.who}: citation (chunk evidence) resolves and carries the marker`, cite.status === 200 && cite.text.includes(probe.marker), `status ${cite.status}`);
    const g = await web(`${t.wsPath}/graph/entities?limit=5`, { session: t.session });
    check(`${tn.who}: graph query answers`, g.status === 200, `status ${g.status}`);
    const other = tn.who === "alice" ? "bob" : "alice";
    const x = await search({ ...t, session: sessions[other] }, probe.marker, 3);
    check(`${tn.who}: the other user gets a concealed 404 on this org`, x.status === 404, `status ${x.status}`);
  }
  const failed = results.filter((r) => !r.ok);
  console.log(failed.length ? `APP VERIFY FAILED: ${failed.length} of ${results.length} checks` : `APP VERIFY OK: ${results.length} checks (search, citation, graph, isolation)`);
  process.exit(failed.length ? 1 : 0);
} else {
  console.error("usage: rehearsal.mjs seed|verify <state.json>");
  process.exit(2);
}
