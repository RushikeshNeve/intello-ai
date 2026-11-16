import { Suspense } from "react";
import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";
import { AuthErrorNotice, SignInLink, SignOutButton } from "@/features/auth/components/auth-controls";
import { getSessionUser } from "@/features/auth/server";

export default function Home({ searchParams }: PageProps<"/">) {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">Universal Knowledge OS</h1>
      <p className="text-sm text-muted-foreground">Local-first knowledge management.</p>
      {/* Reads the session cookie (request-time data), so it streams in behind a boundary. */}
      <Suspense fallback={<p className="text-sm text-muted-foreground">Checking your session…</p>}>
        <AuthStatus searchParams={searchParams} />
      </Suspense>
    </main>
  );
}

async function AuthStatus({ searchParams }: { searchParams: PageProps<"/">["searchParams"] }) {
  const [user, params] = await Promise.all([getSessionUser(), searchParams]);
  const error = typeof params.auth_error === "string" ? params.auth_error : undefined;
  if (!user) {
    return (
      <div className="flex flex-col items-center gap-3">
        <AuthErrorNotice code={error} />
        <SignInLink />
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center gap-3">
      <p className="text-sm">
        Signed in as <span className="font-medium">{user.email ?? user.name ?? "unknown user"}</span>
      </p>
      <div className="flex gap-2">
        <Link href="/home" className={buttonVariants()}>
          Open workspace
        </Link>
        <SignOutButton />
      </div>
    </div>
  );
}
