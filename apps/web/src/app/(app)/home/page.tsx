import { Suspense } from "react";
import { redirect } from "next/navigation";
import { SignOutButton } from "@/features/auth/components/auth-controls";
import { getSessionUser } from "@/features/auth/server";

/**
 * Protected placeholder. `src/proxy.ts` already 307s unauthenticated requests to the login route; the
 * DAL check below is the defence-in-depth re-check close to the data.
 */
export default function HomePage() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">Home</h1>
      <Suspense fallback={<p className="text-sm text-muted-foreground">Loading…</p>}>
        <SignedInPanel />
      </Suspense>
    </main>
  );
}

async function SignedInPanel() {
  const user = await getSessionUser();
  if (!user) redirect("/api/auth/login?returnTo=%2Fhome");
  return (
    <div className="flex flex-col items-center gap-3">
      <p className="text-sm" data-testid="session-user">
        Signed in as <span className="font-medium">{user.email ?? user.name ?? "unknown user"}</span>
      </p>
      <p className="text-sm text-muted-foreground">Organizations and workspaces arrive in M01.02.</p>
      <SignOutButton />
    </div>
  );
}
