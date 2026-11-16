import { connection } from "next/server";
import { handleCallback } from "@/features/auth/handlers";
import { defaultAuthDeps } from "@/features/auth/session";

export async function GET(request: Request): Promise<Response> {
  // Request-time only (cacheComponents): never prerendered at build time.
  await connection();
  return handleCallback(defaultAuthDeps(), request);
}
