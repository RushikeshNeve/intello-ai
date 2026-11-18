"use client";

import { createContext, useContext, useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError, createBrowserFetcher, type Fetcher } from "@/features/organizations/api/http";
import { orgIdOfKey } from "./keys";

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Pages seed queries with server-rendered data; avoid an immediate refetch on hydration.
        staleTime: 30_000,
        retry: (failureCount, error) => {
          if (error instanceof ApiError && !error.isUnavailable) return false; // 4xx: retrying cannot help
          return failureCount < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}

let browserQueryClient: QueryClient | undefined;

function getQueryClient(): QueryClient {
  // A fresh client per server render (never shared between users); one client for the browser tab.
  if (typeof window === "undefined") return makeQueryClient();
  browserQueryClient ??= makeQueryClient();
  return browserQueryClient;
}

const FetcherContext = createContext<Fetcher | null>(null);

/** The browser transport (same-origin BFF). Tests inject a fake through `QueryProvider`. */
export function useApiFetcher(): Fetcher {
  const f = useContext(FetcherContext);
  if (!f) throw new Error("useApiFetcher must be used inside <QueryProvider>");
  return f;
}

export function QueryProvider({
  children,
  client,
  fetcher,
}: {
  children: ReactNode;
  client?: QueryClient;
  fetcher?: Fetcher;
}) {
  const [qc] = useState(() => client ?? getQueryClient());
  const [f] = useState(() => fetcher ?? createBrowserFetcher());
  return (
    <QueryClientProvider client={qc}>
      <FetcherContext.Provider value={f}>{children}</FetcherContext.Provider>
    </QueryClientProvider>
  );
}

/** Removes every org-scoped query that does not belong to `orgId` (cancelling in-flight fetches first). */
export function dropOtherOrgQueries(qc: QueryClient, orgId: string): void {
  const predicate = (q: { queryKey: readonly unknown[] }) => {
    const owner = orgIdOfKey(q.queryKey);
    return owner !== null && owner !== orgId;
  };
  void qc.cancelQueries({ predicate });
  qc.removeQueries({ predicate });
}
