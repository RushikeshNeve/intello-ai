// Stub of the UKOS API for BFF tests: echoes what the web server forwarded.
//   GET|POST|... /v1/*        -> 200 {method, path, search, headers, body}
//   /v1/force-401             -> 401 (simulates the API rejecting the token)
//   /v1/set-cookie            -> 200 with a Set-Cookie header (must be stripped by the BFF)
//   GET /__log                -> every request received so far (for "never reached upstream" checks)
import { createServer } from "node:http";

const port = Number(process.env.STUB_PORT ?? 23301);
const log = [];

const server = createServer((req, res) => {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    if (url.pathname === "/__log") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(log));
      return;
    }
    const entry = {
      method: req.method,
      path: url.pathname,
      search: url.search,
      headers: req.headers,
      body: Buffer.concat(chunks).toString("utf8"),
    };
    log.push(entry);
    if (url.pathname === "/v1/force-401") {
      res.writeHead(401, { "content-type": "application/json", "www-authenticate": 'Bearer error="invalid_token"' });
      res.end(JSON.stringify({ detail: "invalid_token" }));
      return;
    }
    const headers = { "content-type": "application/json", "x-stub": "1" };
    if (url.pathname === "/v1/set-cookie") headers["set-cookie"] = "api_cookie=evil; Path=/";
    res.writeHead(200, headers);
    res.end(JSON.stringify(entry));
  });
});

server.listen(port, "127.0.0.1", () => console.log(`stub-api listening on 127.0.0.1:${port}`));
