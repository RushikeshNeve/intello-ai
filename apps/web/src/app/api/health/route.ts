import { connection } from "next/server";

/**
 * Liveness probe for the web server. Makes no dependency calls.
 *
 * `cacheComponents` is enabled, which rejects the legacy `export const dynamic = "force-dynamic"`
 * segment config. `await connection()` is its replacement: it opts this handler out of build-time
 * prerendering so every request is answered by the running server.
 */
export async function GET(): Promise<Response> {
  await connection();
  return Response.json(
    { status: "ok" },
    { status: 200, headers: { "Cache-Control": "no-store" } },
  );
}
