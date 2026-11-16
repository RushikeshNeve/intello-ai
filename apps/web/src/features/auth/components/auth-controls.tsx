import { buttonVariants } from "@/components/ui/button";

/** Full-page navigation to the login route (not a client-side `Link`: it leaves the app for the IdP). */
export function SignInLink({ returnTo = "/home" }: { returnTo?: string }) {
  return (
    <a href={`/api/auth/login?returnTo=${encodeURIComponent(returnTo)}`} className={buttonVariants()}>
      Sign in
    </a>
  );
}

/** Plain HTML POST form: works without client JS and carries the browser's Origin for the CSRF check. */
export function SignOutButton() {
  return (
    <form method="post" action="/api/auth/logout">
      <button type="submit" className={buttonVariants({ variant: "outline" })}>
        Sign out
      </button>
    </form>
  );
}

const AUTH_ERROR_MESSAGES: Record<string, string> = {
  missing_transaction: "Your sign-in attempt expired. Please try again.",
  invalid_state: "The sign-in response did not match your request. Please try again.",
  replayed: "That sign-in link was already used. Please sign in again.",
  provider_error: "The identity provider rejected the sign-in.",
  provider_unavailable: "The identity provider is unavailable right now.",
  exchange_failed: "Sign-in could not be completed. Please try again.",
  invalid_id_token: "The identity provider returned an invalid token.",
  audience_missing: "The identity provider did not grant API access.",
};

export function AuthErrorNotice({ code }: { code: string | undefined }) {
  if (!code) return null;
  const message = AUTH_ERROR_MESSAGES[code] ?? "Sign-in failed. Please try again.";
  return (
    <p role="alert" className="text-sm text-destructive">
      {message}
    </p>
  );
}
