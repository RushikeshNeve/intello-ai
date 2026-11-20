/** Command palette items and filtering. Pure. */
export type PaletteItem = { id: string; label: string; group: "Actions" | "Go to" | "Organizations" | "Workspaces"; href: string };

export type PaletteCtx = {
  orgSlug: string;
  orgs: readonly { slug: string; name: string }[];
  workspaces: readonly { slug: string; name: string }[];
};

const enc = encodeURIComponent;

export function buildItems(ctx: PaletteCtx, rawQuery: string): PaletteItem[] {
  const base = `/o/${enc(ctx.orgSlug)}`;
  const q = rawQuery.trim();
  const items: PaletteItem[] = [];
  if (q) items.push({ id: "search-for", label: `Search for "${q}"`, group: "Actions", href: `${base}/search?q=${enc(q)}` });
  items.push({ id: "new-chat", label: "New chat", group: "Actions", href: `${base}/chat` });
  const go = (id: string, label: string, href: string) => items.push({ id, label, group: "Go to", href });
  go("nav-overview", "Overview", base);
  go("nav-search", "Search", `${base}/search`);
  go("nav-chat", "Chat", `${base}/chat`);
  go("nav-memory", "Memory", `${base}/memory`);
  go("nav-research", "Research", `${base}/research`);
  for (const w of ctx.workspaces) {
    const wb = `${base}/w/${enc(w.slug)}`;
    items.push({ id: `ws-${w.slug}`, label: `Workspace: ${w.name}`, group: "Workspaces", href: wb });
    items.push({ id: `docs-${w.slug}`, label: `Documents: ${w.name}`, group: "Workspaces", href: `${wb}/documents` });
    items.push({ id: `graph-${w.slug}`, label: `Graph: ${w.name}`, group: "Workspaces", href: `${wb}/graph` });
  }
  for (const o of ctx.orgs) {
    if (o.slug !== ctx.orgSlug) items.push({ id: `org-${o.slug}`, label: `Switch to ${o.name}`, group: "Organizations", href: `/o/${enc(o.slug)}` });
  }
  if (!q) return items;
  const needle = q.toLowerCase();
  // The "Search for" action always stays; the rest are filtered by substring on the label.
  return items.filter((i) => i.id === "search-for" || i.label.toLowerCase().includes(needle));
}

/** Arrow-key movement with wrap-around; -1 when there is nothing to select. */
export function moveActive(current: number, delta: 1 | -1, count: number): number {
  if (count <= 0) return -1;
  return (current + delta + count) % count;
}
