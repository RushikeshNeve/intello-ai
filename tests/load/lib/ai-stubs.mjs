// One OpenAI-compatible endpoint for the stack (AI_BASE_URL is a single URL): a tiny path router in front of
// the two existing stubs (tests/e2e/support/embedding-stub.mjs and chat-stub.mjs, unchanged).
//   /v1/embeddings -> embedding stub, everything else under /v1 -> chat stub.
// If an upstream is down the router answers 502 (the embedder-down fault: kill the embedding stub process).
// Library use: const ai = await startAiStubs({ ... }); await ai.stopEmbedder(); await ai.startEmbedder(); await ai.stop();
// CLI: node ai-stubs.mjs   (ports from AI_ROUTER_PORT/AI_EMBED_PORT/AI_CHAT_PORT; Ctrl+C stops all)
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const SUPPORT = fileURLToPath(new URL("../../e2e/support/", import.meta.url));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitHealthy(url, timeout = 15_000) {
  const end = Date.now() + timeout;
  for (;;) {
    try { if ((await fetch(`${url}/health`)).ok) return; } catch { /* not yet */ }
    if (Date.now() > end) throw new Error(`stub at ${url} did not become healthy`);
    await sleep(150);
  }
}

export async function startAiStubs({
  routerPort = Number(process.env.AI_ROUTER_PORT ?? 18380),
  embedPort = Number(process.env.AI_EMBED_PORT ?? 18381),
  chatPort = Number(process.env.AI_CHAT_PORT ?? 18382),
  chatScript = '["The warranty is three years [1]."]',
  chatEnv = {},
} = {}) {
  let embedProc = null;
  let chatProc = null;
  const startEmbedder = async () => {
    if (embedProc) return;
    embedProc = spawn(process.execPath, [`${SUPPORT}embedding-stub.mjs`], {
      env: { ...process.env, PORT: String(embedPort), HOST: "127.0.0.1", DIMENSIONS: "768", MODEL: "google/embedding-gemma-300m" }, stdio: "ignore" });
    embedProc.on("exit", () => { embedProc = null; });
    await waitHealthy(`http://127.0.0.1:${embedPort}`);
  };
  const stopEmbedder = async () => {
    if (!embedProc) return;
    const p = embedProc;
    const done = new Promise((r) => p.once("exit", r));
    p.kill();
    await done;
  };
  const startChat = async () => {
    if (chatProc) return;
    chatProc = spawn(process.execPath, [`${SUPPORT}chat-stub.mjs`], {
      env: { ...process.env, PORT: String(chatPort), HOST: "127.0.0.1", CHAT_SCRIPT: chatScript, ...chatEnv }, stdio: "ignore" });
    chatProc.on("exit", () => { chatProc = null; });
    await waitHealthy(`http://127.0.0.1:${chatPort}`);
  };
  await startEmbedder();
  await startChat();

  const router = http.createServer((req, res) => {
    const port = req.url.startsWith("/v1/embeddings") ? embedPort : req.url === "/health" ? null : chatPort;
    if (port === null) { res.writeHead(200, { "content-type": "application/json" }); res.end('{"status":"ok"}'); return; }
    const up = http.request({ host: "127.0.0.1", port, path: req.url, method: req.method, headers: req.headers }, (ures) => {
      res.writeHead(ures.statusCode ?? 502, ures.headers);
      ures.pipe(res);
      res.on("close", () => ures.destroy());
    });
    up.on("error", () => { if (!res.headersSent) res.writeHead(502, { "content-type": "application/json" }); res.end('{"error":"upstream_down"}'); });
    res.on("close", () => up.destroy());
    req.pipe(up);
  });
  await new Promise((r) => router.listen(routerPort, "0.0.0.0", r));
  return {
    routerPort, embedPort, chatPort,
    embedUrl: `http://127.0.0.1:${embedPort}`, chatUrl: `http://127.0.0.1:${chatPort}`,
    startEmbedder, stopEmbedder,
    embedStats: async () => (await (await fetch(`http://127.0.0.1:${embedPort}/__stats`)).json()).stats,
    chatStats: async () => (await (await fetch(`http://127.0.0.1:${chatPort}/__stats`)).json()).stats,
    async stop() {
      router.close();
      router.closeAllConnections?.();
      await stopEmbedder();
      if (chatProc) { const p = chatProc; const d = new Promise((r) => p.once("exit", r)); p.kill(); await d; }
    },
  };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const ai = await startAiStubs();
  console.log(`ai stubs: router :${ai.routerPort} embedder :${ai.embedPort} chat :${ai.chatPort}`);
  const stop = async () => { await ai.stop(); process.exit(0); };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
}
