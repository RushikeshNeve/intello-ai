import { Suspense, type ReactNode } from "react";
import { QueryProvider } from "@/lib/query/provider";
import { RouteFocus } from "@/features/shell/route-focus";

/** Signed-in app: one TanStack Query client per browser tab (ADR-047), focus management on navigation. */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <QueryProvider>
      {/* Reads the URL (runtime data): streamed behind its own boundary so it never blocks the shell. */}
      <Suspense fallback={null}>
        <RouteFocus />
      </Suspense>
      {children}
    </QueryProvider>
  );
}
