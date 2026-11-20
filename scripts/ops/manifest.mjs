#!/usr/bin/env node
// Backup manifest writer/verifier (M08.02.01). Dependency-free Node 20+.
//   node manifest.mjs write  <dir> --migration-head H --embedding-space S --pipeline-version P --project N --buckets "a b" --images k=v,k=v
//   node manifest.mjs verify <dir>      recompute every sha256 and compare with manifest.json (exit 1 on mismatch)
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { join, relative } from "node:path";

const [cmd, dir, ...rest] = process.argv.slice(2);
const opt = (name, fallback = "") => {
  const i = rest.indexOf(`--${name}`);
  return i >= 0 ? rest[i + 1] : fallback;
};

function sha256File(path) {
  return new Promise((resolve, reject) => {
    const h = createHash("sha256");
    createReadStream(path).on("data", (c) => h.update(c)).on("error", reject).on("end", () => resolve(h.digest("hex")));
  });
}
async function walk(root) {
  const out = [];
  for (const e of await readdir(root, { withFileTypes: true })) {
    const p = join(root, e.name);
    if (e.isDirectory()) out.push(...(await walk(p)));
    else out.push(p);
  }
  return out;
}
const rel = (p) => relative(dir, p).split("\\").join("/");

async function collect() {
  const files = {};
  for (const name of ["postgres.dump", "rushdb-data.tgz", "rowcounts.txt", "rushdb-counts.json"]) {
    files[name] = { sha256: await sha256File(join(dir, name)), bytes: (await stat(join(dir, name))).size };
  }
  const objects = {};
  for (const p of (await walk(join(dir, "objects"))).sort()) {
    const key = rel(p);
    objects[key] = { sha256: await sha256File(p), bytes: (await stat(p)).size };
  }
  return { files, objects };
}

if (cmd === "write") {
  const { files, objects } = await collect();
  const rowCounts = {};
  for (const line of (await readFile(join(dir, "rowcounts.txt"), "utf8")).split("\n").filter(Boolean)) {
    const [t, n] = line.split("=");
    rowCounts[t] = Number(n);
  }
  const perBucket = {};
  for (const b of opt("buckets").split(/\s+/).filter(Boolean)) {
    const keys = Object.keys(objects).filter((k) => k.startsWith(`objects/${b}/`));
    perBucket[b] = { objects: keys.length, bytes: keys.reduce((s, k) => s + objects[k].bytes, 0) };
  }
  const manifest = {
    format: 1,
    created_at: new Date().toISOString(),
    source_project: opt("project"),
    versions: {
      migration_head: opt("migration-head"),
      embedding_space_ids: opt("embedding-space").split(",").filter(Boolean),
      pipeline_versions: opt("pipeline-version").split(",").filter(Boolean),
      images: Object.fromEntries(opt("images").split(",").filter(Boolean).map((kv) => kv.split(/=(.*)/s).slice(0, 2))),
    },
    contains_env_file: false,
    row_counts: rowCounts,
    buckets: perBucket,
    files,
    objects,
  };
  await writeFile(join(dir, "manifest.json"), JSON.stringify(manifest, null, 2));
  const sums = [...Object.entries(files).map(([k, v]) => `${v.sha256}  ${k}`), ...Object.entries(objects).map(([k, v]) => `${v.sha256}  ${k}`)];
  await writeFile(join(dir, "SHA256SUMS"), sums.join("\n") + "\n");
  console.log(`manifest: head=${manifest.versions.migration_head} spaces=${manifest.versions.embedding_space_ids} pipeline=${manifest.versions.pipeline_versions} ` +
    `tables=${Object.keys(rowCounts).length} rows=${Object.values(rowCounts).reduce((a, b) => a + b, 0)} ` +
    `objects=${Object.keys(objects).length} dump=${files["postgres.dump"].bytes}B rushdb=${files["rushdb-data.tgz"].bytes}B`);
} else if (cmd === "verify") {
  const manifest = JSON.parse(await readFile(join(dir, "manifest.json"), "utf8"));
  const now = await collect();
  const problems = [];
  for (const [k, v] of Object.entries(manifest.files)) if (now.files[k]?.sha256 !== v.sha256) problems.push(`file ${k}`);
  for (const [k, v] of Object.entries(manifest.objects)) if (now.objects[k]?.sha256 !== v.sha256) problems.push(`object ${k}`);
  for (const k of Object.keys(now.objects)) if (!manifest.objects[k]) problems.push(`unexpected object ${k}`);
  const all = (await walk(dir)).map(rel);
  if (all.some((p) => /(^|\/)\.env(\.|$)/.test(p))) problems.push(".env present in backup");
  if (problems.length) { console.error(`manifest verify FAILED: ${problems.slice(0, 10).join(", ")}`); process.exit(1); }
  console.log(`manifest verify ok: ${Object.keys(manifest.files).length} files, ${Object.keys(manifest.objects).length} objects`);
} else if (cmd === "verify-rows") {
  // verify-rows <backupDir> <rowcounts.txt of the restored database>
  const manifest = JSON.parse(await readFile(join(dir, "manifest.json"), "utf8"));
  const got = {};
  for (const line of (await readFile(rest[0], "utf8")).split("\n").filter(Boolean)) { const [t, n] = line.split("="); got[t] = Number(n); }
  const problems = [];
  for (const [t, n] of Object.entries(manifest.row_counts)) if (got[t] !== n) problems.push(`${t}: backup ${n}, restored ${got[t]}`);
  for (const t of Object.keys(got)) if (!(t in manifest.row_counts)) problems.push(`unexpected table ${t}`);
  if (problems.length) { console.error(`row counts FAILED: ${problems.slice(0, 10).join("; ")}`); process.exit(1); }
  console.log(`row counts ok: ${Object.keys(got).length} tables, ${Object.values(got).reduce((a, b) => a + b, 0)} rows identical`);
} else if (cmd === "verify-rushdb") {
  // verify-rushdb <backupDir> <snapshot.json taken on the restored RushDB, with --probe-from the backup's snapshot>
  const before = JSON.parse(await readFile(join(dir, "rushdb-counts.json"), "utf8"));
  const after = JSON.parse(await readFile(rest[0], "utf8"));
  const problems = [];
  for (const [org, b] of Object.entries(before)) {
    const a = after[org];
    if (!a) { problems.push(`org ${org} missing`); continue; }
    if (a.chunks !== b.chunks || a.published !== b.published) problems.push(`org ${org}: chunks ${b.chunks}/${b.published} -> ${a.chunks}/${a.published}`);
    const names = (x) => x.indexes.map((i) => `${i.name}:${i.state}`).join(",");
    if (names(a) !== names(b)) problems.push(`org ${org}: indexes ${names(b)} -> ${names(a)}`);
    if (b.chunks > 0 && !a.indexes.some((i) => i.name === "chunk-text" && i.state === "ready")) problems.push(`org ${org}: text index chunk-text not ready`);
    if (b.probe.term && (a.probe.hits !== b.probe.hits || !(a.probe.hits > 0))) problems.push(`org ${org}: BM25 "${b.probe.term}" hits ${b.probe.hits} -> ${a.probe.hits} (re-create the text index)`);
  }
  if (problems.length) { console.error(`rushdb verify FAILED: ${problems.join("; ")}`); process.exit(1); }
  const tot = Object.values(before).reduce((s, o) => s + o.chunks, 0);
  console.log(`rushdb verify ok: ${Object.keys(before).length} tenants, ${tot} chunks, text + vector indexes ready, BM25 probe hits identical`);
} else if (cmd === "verify-objects") {
  // verify-objects <backupDir> <exportDir>: an export of the RESTORED buckets (<exportDir>/<bucket>/...) equals the backup.
  const exportDir = rest[0];
  const manifest = JSON.parse(await readFile(join(dir, "manifest.json"), "utf8"));
  const got = {};
  for (const p of await walk(exportDir)) got[`objects/${relative(exportDir, p).split("\\").join("/")}`] = await sha256File(p);
  const problems = [];
  for (const [k, v] of Object.entries(manifest.objects)) if (got[k] !== v.sha256) problems.push(`${got[k] ? "checksum" : "missing"} ${k}`);
  for (const k of Object.keys(got)) if (!manifest.objects[k]) problems.push(`unexpected ${k}`);
  if (problems.length) { console.error(`object verify FAILED: ${problems.slice(0, 10).join(", ")}`); process.exit(1); }
  console.log(`object verify ok: ${Object.keys(got).length} restored objects match the backup checksums`);
} else {
  console.error("usage: manifest.mjs write|verify <dir> [options]");
  process.exit(2);
}
