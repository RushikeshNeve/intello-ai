/**
 * Sidebar navigation model (pure, unit-tested). Entries map to EXISTING routes:
 * org-level (`/o/{org}/...`): Home, Search, Ask, Research, Memory;
 * workspace-level (`/o/{org}/w/{ws}/...`): Library, Graph and the management placeholders.
 * Workspace-level entries need a "current workspace" (see workspace-memory.ts); without one they are disabled.
 */

/**
 * Management screens (Integrations, Activity, People, Settings) are built by later tracks. While this is true the
 * entries link to honest "coming in this redesign" placeholder routes; set false to hide them and 404 the routes.
 */
export const SHOW_REDESIGN_PLACEHOLDERS = true;

export type NavIcon = "home" | "search" | "ask" | "library" | "graph" | "research" | "memory" | "integrations" | "activity" | "people" | "settings";
export type NavEntry = {
  id: string;
  label: string;
  icon: NavIcon;
  group: "knowledge" | "manage";
  /** null: cannot be resolved (workspace-level entry with no workspace) -> render disabled. */
  href: string | null;
  /** Path prefix that marks the entry active (exact match when `exact`). */
  match: string | null;
  exact?: boolean;
  placeholder?: boolean;
};

export function buildNav(orgSlug: string, workspaceSlug: string | null, showPlaceholders = SHOW_REDESIGN_PLACEHOLDERS): NavEntry[] {
  const org = `/o/${encodeURIComponent(orgSlug)}`;
  const ws = workspaceSlug ? `${org}/w/${encodeURIComponent(workspaceSlug)}` : null;
  const wsEntry = (id: string, label: string, icon: NavIcon, group: NavEntry["group"], seg: string, placeholder = false): NavEntry => ({
    id,
    label,
    icon,
    group,
    href: ws ? `${ws}/${seg}` : null,
    match: ws ? `${ws}/${seg}` : null,
    placeholder,
  });
  const entries: NavEntry[] = [
    { id: "home", label: "Home", icon: "home", group: "knowledge", href: org, match: org, exact: true },
    { id: "search", label: "Search", icon: "search", group: "knowledge", href: `${org}/search`, match: `${org}/search` },
    { id: "ask", label: "Ask", icon: "ask", group: "knowledge", href: `${org}/chat`, match: `${org}/chat` },
    wsEntry("library", "Library", "library", "knowledge", "documents"),
    wsEntry("graph", "Graph", "graph", "knowledge", "graph"),
    { id: "research", label: "Research", icon: "research", group: "knowledge", href: `${org}/research`, match: `${org}/research` },
    { id: "memory", label: "Memory", icon: "memory", group: "knowledge", href: `${org}/memory`, match: `${org}/memory` },
  ];
  if (showPlaceholders) {
    entries.push(
      wsEntry("integrations", "Integrations", "integrations", "manage", "integrations", true),
      wsEntry("activity", "Activity", "activity", "manage", "activity", true),
      wsEntry("people", "People", "people", "manage", "team", true),
      wsEntry("settings", "Settings", "settings", "manage", "settings", true),
    );
  }
  return entries;
}

/** Active when the path equals the entry's path or is below it (segment-aware: `/chat` does not match `/chatter`). */
export function isActive(entry: NavEntry, pathname: string): boolean {
  if (!entry.match) return false;
  if (pathname === entry.match) return true;
  return !entry.exact && pathname.startsWith(`${entry.match}/`);
}
