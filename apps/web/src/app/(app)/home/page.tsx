import { Suspense } from "react";
import { redirect } from "next/navigation";
import { getSessionUser } from "@/features/auth/server";
import { loginUrl } from "@/features/organizations/api/http";
import { OrgDirectory } from "@/features/organizations/components/org-directory";
import { getMyOrgs } from "@/features/organizations/server";
import { AppHeader, userLabelOf } from "@/features/shell/app-header";
import { SessionRedirect } from "@/features/shell/session-redirect";
import { PageLoading, ServiceUnavailable } from "@/features/shell/states";

/**
 * `/home`: the user's organizations. `src/proxy.ts` already 307s unauthenticated requests to the login
 * route; the DAL check below is the defence-in-depth re-check close to the data.
 */
export default function HomePage() {
  return (
    <Suspense fallback={<PageLoading />}>
      <HomeContent />
    </Suspense>
  );
}

async function HomeContent() {
  const user = await getSessionUser();
  if (!user) redirect(loginUrl("/home"));
  const orgs = await getMyOrgs();
  if (!orgs.ok && orgs.reason === "unauthenticated") return <SessionRedirect fallbackPath="/home" />;
  return (
    <>
      <AppHeader userLabel={userLabelOf(user)} />
      {orgs.ok ? (
        <main id="main" className="mx-auto grid w-full max-w-3xl flex-1 content-start gap-6 px-4 py-8">
          <h1 tabIndex={-1} className="text-2xl font-semibold tracking-tight outline-none">
            Home
          </h1>
          <OrgDirectory initialOrgs={orgs.data} />
        </main>
      ) : (
        <ServiceUnavailable />
      )}
    </>
  );
}
