import { useSyncExternalStore } from "react";
import { vi } from "vitest";
import type { Document, Version } from "../types";

export function makeVersion(over: Partial<Version> = {}): Version {
  return {
    id: "v-1",
    document_id: "d-1",
    version_number: 1,
    original_filename: "report.pdf",
    content_type: "application/pdf",
    size_bytes: 2048,
    sha256: "a".repeat(64),
    created_by: "u-1",
    created_at: "2026-11-10T10:00:00Z",
    retention: { status: "current", purge_after: null },
    ...over,
  };
}

export function makeDoc(over: Partial<Document> = {}): Document {
  const id = over.id ?? "d-1";
  return {
    id,
    org_id: "o1",
    workspace_id: "w1",
    title: "Quarterly report",
    created_by: "u-1",
    created_at: "2026-11-10T10:00:00Z",
    updated_at: "2026-11-12T10:00:00Z",
    version_count: 1,
    latest_version: makeVersion({ document_id: id }),
    permissions: { write: true, manage: true },
    ...over,
  };
}

export const WS = "/v1/orgs/o1/workspaces/w1";
export const LIBRARY = "/o/acme/w/notes/documents";

// --- a fake `next/navigation`: a URL the components can push/replace and subscribe to ---

const listeners = new Set<() => void>();
let entries: string[] = [LIBRARY];
let index = 0;
let search = new URLSearchParams();

function parse(href: string) {
  const u = new URL(href, "http://app.invalid");
  return { path: u.pathname, search: new URLSearchParams(u.search) };
}
function apply() {
  search = parse(entries[index]).search;
  listeners.forEach((l) => l());
}

export const nav = {
  refresh: vi.fn(),
  pushes: [] as string[],
  replaces: [] as string[],
  reset(href = LIBRARY) {
    entries = [href];
    index = 0;
    nav.pushes = [];
    nav.replaces = [];
    nav.refresh.mockClear();
    apply();
  },
  /** What the address bar shows. */
  current: () => entries[index],
  push(href: string) {
    nav.pushes.push(href);
    entries = [...entries.slice(0, index + 1), href];
    index++;
    apply();
  },
  replace(href: string) {
    nav.replaces.push(href);
    entries[index] = href;
    apply();
  },
  back() {
    if (index > 0) index--;
    apply();
  },
  forward() {
    if (index < entries.length - 1) index++;
    apply();
  },
};

const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
};

export const navigationModule = {
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: nav.back, forward: nav.forward, refresh: nav.refresh, prefetch: vi.fn() }),
  usePathname: () => useSyncExternalStore(subscribe, () => parse(entries[index]).path),
  useSearchParams: () => useSyncExternalStore(subscribe, () => search),
};
