#!/usr/bin/env node
// Dependency-free stand-in for an OpenAI-compatible chat server (LM Studio) used by end-to-end
// tests of grounded chat (M04). See README.md in this folder.
//
//   node chat-stub.mjs                      # serve on 127.0.0.1:18434
//
// Scripted replies (first match wins):
//   CHAT_SCRIPT_FILE=path.json  or  CHAT_SCRIPT='<json>'
//   JSON is an array of replies used in request order (the last one repeats), or an object
//   { "rules": [{ "match": "substring of the last user message", "reply": R }], "default": R }.
//   R is a string, or { "text": "...", "error_after": N } (N deltas, then the stream dies with an
//   error event), or { "text": "...", "status": 500 }.
// With no script the reply is a fixed grounded sentence citing [1].
// The stub never logs prompt or answer text.

import { readFileSync } from "node:fs";
import { createServer } from "node:http";

const env = process.env;
const int = (name, dflt) => {
  const v = env[name];
  return v === undefined || v === "" ? dflt : Number.parseInt(v, 10);
};
const flag = (name) => ["1", "true", "yes"].includes((env[name] ?? "").toLowerCase());

const PORT = int("PORT", 18434);
const HOST = env.HOST || "127.0.0.1";
const MODEL = env.MODEL || "google/gemma-3-4b";
const CHUNK_CHARS = int("CHUNK_CHARS", 8);
const MAX_BODY_BYTES = 8 * 1024 * 1024;

function loadScript() {
  if (env.CHAT_SCRIPT_FILE) return JSON.parse(readFileSync(env.CHAT_SCRIPT_FILE, "utf8"));
  if (env.CHAT_SCRIPT) return JSON.parse(env.CHAT_SCRIPT);
  return ["The requested value is stated in the evidence [1]."];
}
let script = loadScript();

// Mutable state: initial values from the environment, changeable through POST /__control.
const state = {
  delay_ms: int("DELAY_MS", 0), // wait before each delta
  fail_next: int("FAIL_FIRST", 0), // answer the next n chat requests with fail_status
  fail_status: int("FAIL_STATUS", 503),
  echo_prompt: flag("ECHO_PROMPT"), // reply with the messages the server received (JSON text)
};
const stats = { requests: 0, failed: 0, served: 0, aborted: 0 };
let scriptIndex = 0;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function pickReply(messages) {
  if (state.echo_prompt) return JSON.stringify(messages);
  if (Array.isArray(script)) {
    const reply = script[Math.min(scriptIndex, script.length - 1)];
    scriptIndex += 1;
    return reply;
  }
  const lastUser = [...messages].reverse().find((m) => m.role === "user")?.content ?? "";
  for (const rule of script.rules ?? []) {
    if (lastUser.includes(rule.match)) return rule.reply;
  }
  return script.default ?? "";
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("body too large"));
        req.destroy();
      } else chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sendJson(res, status, body) {
  const raw = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(raw) });
  res.end(raw);
}

const sse = (obj) => `data: ${JSON.stringify(obj)}\n\n`;
const chunk = (delta, finish = null) => ({
  id: "chatcmpl-stub",
  object: "chat.completion.chunk",
  model: MODEL,
  choices: [{ index: 0, delta, finish_reason: finish }],
});

function split(text) {
  const parts = [];
  const chars = [...text]; // code points: never splits a surrogate pair
  for (let i = 0; i < chars.length; i += CHUNK_CHARS) parts.push(chars.slice(i, i + CHUNK_CHARS).join(""));
  return parts;
}

async function handleChat(req, res) {
  stats.requests += 1;
  let payload;
  try {
    payload = JSON.parse(await readBody(req));
  } catch {
    return sendJson(res, 400, { error: { message: "invalid JSON" } });
  }
  if (payload.model && payload.model !== MODEL) {
    return sendJson(res, 404, { error: { message: "model not found" } });
  }
  if (state.fail_next > 0) {
    state.fail_next -= 1;
    stats.failed += 1;
    return sendJson(res, state.fail_status, { error: { message: "injected failure" } });
  }
  const messages = Array.isArray(payload.messages) ? payload.messages : [];
  const reply = pickReply(messages);
  const spec = typeof reply === "string" ? { text: reply } : reply;
  if (spec.status) {
    stats.failed += 1;
    return sendJson(res, spec.status, { error: { message: "scripted failure" } });
  }
  const text = spec.text ?? "";

  if (payload.stream !== true) {
    if (state.delay_ms) await sleep(state.delay_ms);
    stats.served += 1;
    return sendJson(res, 200, {
      id: "chatcmpl-stub",
      object: "chat.completion",
      model: MODEL,
      choices: [{ index: 0, message: { role: "assistant", content: text, reasoning_content: "" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    });
  }

  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
  let closed = false;
  res.on("close", () => {
    if (!res.writableEnded) {
      closed = true;
      stats.aborted += 1;
    }
  });
  res.write(": stub keepalive\n\n");
  res.write(sse(chunk({ role: "assistant", content: "" })));
  const parts = split(text);
  for (let i = 0; i < parts.length; i += 1) {
    if (closed) return;
    if (state.delay_ms) await sleep(state.delay_ms);
    if (spec.error_after !== undefined && i >= spec.error_after) {
      res.write(sse({ error: { message: "scripted mid-stream failure" } }));
      stats.failed += 1;
      return res.end();
    }
    res.write(sse(chunk({ content: parts[i] })));
  }
  if (closed) return;
  res.write(sse(chunk({}, "stop")));
  res.write("data: [DONE]\n\n");
  stats.served += 1;
  res.end();
}

const server = createServer(async (req, res) => {
  try {
    const path = (req.url ?? "").split("?")[0];
    if (req.method === "GET" && path === "/v1/models") {
      return sendJson(res, 200, { object: "list", data: [{ id: MODEL, object: "model" }] });
    }
    if (req.method === "GET" && path === "/health") return sendJson(res, 200, { status: "ok" });
    if (req.method === "GET" && path === "/__stats") return sendJson(res, 200, { ...state, ...stats });
    if (req.method === "POST" && path === "/__control") {
      const body = JSON.parse((await readBody(req)) || "{}");
      if (body.reset) {
        Object.assign(state, { delay_ms: 0, fail_next: 0, fail_status: 503, echo_prompt: false });
        Object.assign(stats, { requests: 0, failed: 0, served: 0, aborted: 0 });
        scriptIndex = 0;
      }
      for (const key of Object.keys(state)) if (key in body) state[key] = body[key];
      if (body.script !== undefined) {
        script = body.script;
        scriptIndex = 0;
      }
      return sendJson(res, 200, { ...state, ...stats });
    }
    if (req.method === "POST" && path === "/v1/chat/completions") return await handleChat(req, res);
    return sendJson(res, 404, { error: { message: "not found" } });
  } catch {
    if (!res.headersSent) sendJson(res, 500, { error: { message: "stub error" } });
    else res.end();
  }
});

server.listen(PORT, HOST, () => {
  console.log(`chat-stub listening on http://${HOST}:${PORT} model=${MODEL}`);
});
