import { connection } from "next/server";
import { handleSession } from "@/features/auth/handlers";
import { defaultAuthDeps } from "@/features/auth/session";

/** Browser-safe session status (`{authenticated, user}`); never includes tokens. */
export async function GET(request: Request): Promise<Response> {
  // Request-time only (cacheComponents): never prerendered at build time.
  await connection();
  return handleSession(defaultAuthDeps(), request);
}
