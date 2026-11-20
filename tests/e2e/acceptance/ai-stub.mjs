// One OpenAI-compatible endpoint for the acceptance stack: the API and worker have a single AI_BASE_URL for
// both embeddings and chat, so this tiny proxy sends /v1/embeddings to the embedding stub and everything else
// (chat completions, models) to the scripted chat stub. Streams are piped through unchanged. Never logs bodies.
//   PORT=18650 HOST=0.0.0.0 EMBED_URL=http://127.0.0.1:18651 CHAT_URL=http://127.0.0.1:18652 node ai-stub.mjs
import http from "node:http";

const PORT = Number(process.env.PORT ?? 18650);
const HOST = process.env.HOST ?? "127.0.0.1";
const EMBED = new URL(process.env.EMBED_URL ?? "http://127.0.0.1:18651");
const CHAT = new URL(process.env.CHAT_URL ?? "http://127.0.0.1:18652");

const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"status":"ok"}');
    return;
  }
  const target = req.url.startsWith("/v1/embeddings") ? EMBED : CHAT;
  const up = http.request(
    { host: target.hostname, port: target.port, path: req.url, method: req.method, headers: req.headers },
    (r) => {
      res.writeHead(r.statusCode ?? 502, r.headers);
      r.pipe(res);
      res.on("close", () => r.destroy());
    },
  );
  up.on("error", () => {
    if (!res.headersSent) res.writeHead(502);
    res.end();
  });
  req.on("aborted", () => up.destroy());
  req.pipe(up);
});
server.listen(PORT, HOST, () => console.log(`ai-stub listening on http://${HOST}:${PORT}`));
