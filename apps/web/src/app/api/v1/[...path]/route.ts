import { connection } from "next/server";
import { proxyToApi } from "@/features/auth/bff";
import { defaultAuthDeps } from "@/features/auth/session";

/** BFF: `/api/v1/*` → `${API_INTERNAL_BASE_URL}/v1/*` with the session's bearer token (contract §4, §6). */
async function handler(request: Request, ctx: RouteContext<"/api/v1/[...path]">): Promise<Response> {
  // Request-time only (cacheComponents): never prerendered at build time.
  await connection();
  const { path } = await ctx.params;
  return proxyToApi(defaultAuthDeps(), request, path);
}

export const GET = handler;
export const HEAD = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
