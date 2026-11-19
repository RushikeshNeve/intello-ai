#!/usr/bin/env node
// Dependency-free stand-in for an OpenAI-compatible embeddings server (LM Studio) used by
// end-to-end tests. See README.md in this folder.
//
//   node embedding-stub.mjs                       # serve on 127.0.0.1:18234
//   node embedding-stub.mjs --vectors cases.json  # print golden vectors to stdout and exit
//
// The embedding algorithm is the SAME as apps/api/app/modules/ai/embeddings/fake.py
// (feature-hashing bag of words, documented there); a golden-file test keeps them identical.
// The stub never logs request texts.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";

const env = process.env;
const int = (name, dflt) => {
  const v = env[name];
  return v === undefined || v === "" ? dflt : Number.parseInt(v, 10);
};
const flag = (name) => ["1", "true", "yes"].includes((env[name] ?? "").toLowerCase());

const PORT = int("PORT", 18234);
const HOST = env.HOST || "127.0.0.1";
const DIMENSIONS = int("DIMENSIONS", 768);
const MODEL = env.MODEL || "google/embedding-gemma-300m";
const MAX_BODY_BYTES = 8 * 1024 * 1024;

/** The feature-hashing embedding. Keep in sync with fake.py. */
export function hashEmbed(text, dimensions) {
  const tokens = text.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];
  const sums = new Array(dimensions).fill(0);
  for (const token of tokens) {
    const digest = createHash("sha256").update(token, "utf8").digest();
    const bucket = digest.readUInt32BE(0) % dimensions;
    sums[bucket] += digest[4] % 2 === 0 ? 1 : -1;
  }
  const norm = Math.sqrt(sums.reduce((acc, s) => acc + s * s, 0));
  if (norm === 0) {
    const unit = new Array(dimensions).fill(0);
    unit[0] = 1;
    return unit;
  }
  return sums.map((s) => s / norm);
}

// Mutable fault-injection state: initial values from the environment, changeable at run time
// through POST /__control.
const faults = {
  fail_next: int("FAIL_FIRST", 0), // answer the next n embedding requests with fail_status
  fail_status: int("FAIL_STATUS", 503),
  wrong_dimensions: flag("WRONG_DIMENSIONS"), // vectors with DIMENSIONS-1 entries
  wrong_count: flag("WRONG_COUNT"), // one vector too few
  reverse_order: flag("REVERSE_ORDER"), // data returned in reverse order (indexes kept)
  delay_ms: int("DELAY_MS", 0),
  echo_model: env.ECHO_MODEL || null, // model id reported in the response
};
const stats = { requests: 0, failed: 0, served: 0, inputs: 0 };

function parseArgs(argv) {
  const i = argv.indexOf("--vectors");
  return i >= 0 ? argv[i + 1] : null;
}

const vectorsFile = parseArgs(process.argv.slice(2));
if (vectorsFile) {
  // cases.json: {"cases":[{"dimensions":64,"texts":["..."]}]}  ->  same + "vectors"
  const input = JSON.parse(readFileSync(vectorsFile, "utf8"));
  const cases = input.cases.map((c) => ({
    dimensions: c.dimensions,
    texts: c.texts,
    vectors: c.texts.map((t) => hashEmbed(t, c.dimensions)),
  }));
  process.stdout.write(`${JSON.stringify({ algorithm: "feature-hashing-v1", cases }, null, 1)}\n`);
  process.exit(0);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function send(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

async function handleEmbeddings(req, res) {
  stats.requests += 1;
  let parsed;
  try {
    parsed = JSON.parse(await readBody(req));
  } catch {
    return send(res, 400, { error: { message: "invalid JSON body", type: "invalid_request_error" } });
  }
  if (faults.delay_ms > 0) await sleep(faults.delay_ms);
  if (faults.fail_next > 0) {
    faults.fail_next -= 1;
    stats.failed += 1;
    return send(res, faults.fail_status, { error: { message: "injected failure" } });
  }
  const input = parsed?.input;
  const texts = typeof input === "string" ? [input] : input;
  if (!Array.isArray(texts) || texts.length === 0 || !texts.every((t) => typeof t === "string")) {
    return send(res, 400, { error: { message: "input must be a string or an array of strings" } });
  }
  if (parsed.model !== undefined && parsed.model !== MODEL) {
    return send(res, 404, { error: { message: `model not found`, type: "invalid_request_error" } });
  }
  const dims = faults.wrong_dimensions ? DIMENSIONS - 1 : DIMENSIONS;
  let data = texts.map((text, index) => ({
    object: "embedding",
    index,
    embedding: hashEmbed(text, dims),
  }));
  if (faults.wrong_count) data = data.slice(0, -1);
  if (faults.reverse_order) data = data.reverse();
  stats.served += 1;
  stats.inputs += texts.length;
  // Like LM Studio, usage is reported as 0: clients must count tokens themselves.
  return send(res, 200, {
    object: "list",
    data,
    model: faults.echo_model ?? parsed.model ?? MODEL,
    usage: { prompt_tokens: 0, total_tokens: 0 },
  });
}

async function handleControl(req, res) {
  let patch;
  try {
    patch = JSON.parse(await readBody(req));
  } catch {
    return send(res, 400, { error: "invalid JSON body" });
  }
  if (patch.reset) {
    Object.assign(faults, {
      fail_next: 0, fail_status: 503, wrong_dimensions: false, wrong_count: false,
      reverse_order: false, delay_ms: 0, echo_model: null,
    });
    Object.assign(stats, { requests: 0, failed: 0, served: 0, inputs: 0 });
  }
  for (const key of Object.keys(faults)) {
    if (key in patch) faults[key] = patch[key];
  }
  return send(res, 200, { faults, stats });
}

const server = createServer(async (req, res) => {
  const path = (req.url ?? "").split("?")[0];
  try {
    if (req.method === "GET" && (path === "/v1/models" || path === "/models")) {
      return send(res, 200, {
        object: "list",
        data: [{ id: MODEL, object: "model", owned_by: "embedding-stub" }],
      });
    }
    if (req.method === "POST" && (path === "/v1/embeddings" || path === "/embeddings")) {
      return await handleEmbeddings(req, res);
    }
    if (req.method === "POST" && path === "/__control") return await handleControl(req, res);
    if (req.method === "GET" && path === "/__stats") return send(res, 200, { faults, stats });
    if (req.method === "GET" && path === "/health") return send(res, 200, { status: "ok" });
    return send(res, 404, { error: { message: "not found" } });
  } catch {
    return send(res, 500, { error: { message: "stub error" } });
  }
});

server.listen(PORT, HOST, () => {
  // One stable line that harnesses can wait for.
  console.log(`embedding-stub listening on http://${HOST}:${PORT} model=${MODEL} dimensions=${DIMENSIONS}`);
});
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => server.close(() => process.exit(0)));
