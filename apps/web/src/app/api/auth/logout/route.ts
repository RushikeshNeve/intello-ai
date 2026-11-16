import { connection } from "next/server";
import { handleLogout } from "@/features/auth/handlers";
import { defaultAuthDeps } from "@/features/auth/session";

/** POST only: logout changes state, so it is CSRF-checked and never triggered by a link or prefetch. */
export async function POST(request: Request): Promise<Response> {
  // Request-time only (cacheComponents): never prerendered at build time.
  await connection();
  return handleLogout(defaultAuthDeps(), request);
}
