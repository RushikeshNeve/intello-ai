// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OrgScopeProvider } from "@/features/organizations/components/org-scope";
import { fakeFetcher, jsonResponse, ORG_A, ORG_B, renderWithQuery, ws } from "@/features/organizations/__tests__/helpers";
import { AppShell } from "../app-shell";
import { buildNav, isActive } from "../nav-config";
import { lastWorkspaceKey, pickWorkspace } from "../current-workspace";
import { stubMatchMedia } from "./match-media";

const nav = vi.hoisted(() => ({ push: vi.fn(), pathname: "/o/acme", params: {} as Record<string, string> }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: vi.fn(), prefetch: vi.fn(), back: vi.fn(), refresh: vi.fn() }),
  usePathname: () => nav.pathname,
  useParams: () => nav.params,
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const WS = [ws(ORG_A, "notes", "Acme Notes"), ws(ORG_A, "papers", "Acme Papers")];

function renderShell(workspaces = WS) {
  const { fetcher } = fakeFetcher({ [`GET /v1/orgs/${ORG_A.id}/workspaces`]: () => jsonResponse(200, { items: workspaces }) });
  return renderWithQuery(
    <OrgScopeProvider org={ORG_A} orgs={[ORG_A, ORG_B]} navigate={vi.fn()}>
      <AppShell userLabel="me@example.com" initialWorkspaces={workspaces}>
        <h1>Page content</h1>
      </AppShell>
    </OrgScopeProvider>,
    { fetcher },
  );
}

beforeEach(() => {
  stubMatchMedia();
  window.localStorage.clear();
  nav.pathname = "/o/acme";
  nav.params = { orgSlug: "acme" };
});
afterEach(() => {
  cleanup();
  nav.push.mockReset();
});

describe("nav model", () => {
  it("maps to existing routes and disables workspace entries without a workspace", () => {
    const withWs = buildNav("acme", "notes");
    expect(withWs.map((e) => [e.id, e.href])).toEqual([
      ["home", "/o/acme"],
      ["search", "/o/acme/search"],
      ["ask", "/o/acme/chat"],
      ["library", "/o/acme/w/notes/documents"],
      ["graph", "/o/acme/w/notes/graph"],
      ["research", "/o/acme/research"],
      ["memory", "/o/acme/memory"],
      ["integrations", "/o/acme/w/notes/integrations"],
      ["activity", "/o/acme/w/notes/activity"],
      ["people", "/o/acme/w/notes/team"],
      ["settings", "/o/acme/w/notes/settings"],
    ]);
    expect(buildNav("acme", null).filter((e) => e.href === null).map((e) => e.id)).toEqual(["library", "graph", "integrations", "activity", "people", "settings"]);
    expect(buildNav("acme", "notes", false).some((e) => e.placeholder)).toBe(false);
  });

  it("matches by path segment: chat threads are Ask, Home is exact", () => {
    const [home, , ask] = buildNav("acme", "notes");
    expect(isActive(ask, "/o/acme/chat/t1")).toBe(true);
    expect(isActive(ask, "/o/acme/chatter")).toBe(false);
    expect(isActive(home, "/o/acme/search")).toBe(false);
    expect(isActive(home, "/o/acme")).toBe(true);
  });

  it("picks URL workspace, then a valid stored one, then the first", () => {
    expect(pickWorkspace(WS, "papers", "notes")?.slug).toBe("papers");
    expect(pickWorkspace(WS, null, "papers")?.slug).toBe("papers");
    expect(pickWorkspace(WS, null, "gone")?.slug).toBe("notes");
    expect(pickWorkspace([], null, null)).toBeNull();
  });
});

describe("AppShell", () => {
  it("renders landmarks, skip link first, and marks exactly one nav item current", () => {
    nav.pathname = "/o/acme/search";
    renderShell();
    expect(document.querySelector("a[href='#main']")?.textContent).toBe("Skip to content");
    expect(screen.getByRole("banner")).toBeTruthy();
    expect(screen.getByRole("main").id).toBe("main");
    const primary = screen.getByRole("navigation", { name: "Primary" });
    const current = within(primary).getAllByRole("link").filter((l) => l.getAttribute("aria-current") === "page");
    expect(current.map((l) => l.textContent)).toEqual(["Search"]);
    expect(within(primary).getByRole("link", { name: "Library" }).getAttribute("href")).toBe("/o/acme/w/notes/documents");
    expect(screen.getByTestId("usage-block").getAttribute("aria-disabled")).toBe("true");
  });

  it("shows the workspace from the URL in the breadcrumb and remembers it per org", () => {
    nav.pathname = "/o/acme/w/papers/documents";
    nav.params = { orgSlug: "acme", workspaceSlug: "papers" };
    renderShell();
    expect(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText("Acme Papers")).toBeTruthy();
    expect(window.localStorage.getItem(lastWorkspaceKey(ORG_A.id))).toBe("papers");
    expect(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("link", { name: "Library" }).getAttribute("href")).toBe("/o/acme/w/papers/documents");
  });

  it("defaults to the first workspace, then uses the stored one after mount; ignores a stale stored slug", () => {
    renderShell();
    expect(screen.getByTestId("current-workspace-name").textContent).toBe("Acme Notes");
    cleanup();
    window.localStorage.setItem(lastWorkspaceKey(ORG_A.id), "papers");
    renderShell();
    expect(screen.getByTestId("current-workspace-name").textContent).toBe("Acme Papers");
    cleanup();
    window.localStorage.setItem(lastWorkspaceKey(ORG_A.id), "deleted-ws");
    renderShell();
    expect(screen.getByTestId("current-workspace-name").textContent).toBe("Acme Notes");
  });

  it("workspace switcher persists the choice and re-points Library/Graph", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByTestId("workspace-switcher"));
    await user.click(await screen.findByTestId("workspace-option-papers"));
    expect(window.localStorage.getItem(lastWorkspaceKey(ORG_A.id))).toBe("papers");
    expect(screen.getByTestId("current-workspace-name").textContent).toBe("Acme Papers");
    expect(screen.getByTestId("nav-graph").getAttribute("href")).toBe("/o/acme/w/papers/graph");
    expect(nav.push).not.toHaveBeenCalled(); // not on a workspace route: no navigation
  });

  it("on a workspace route the switcher keeps the section but drops deeper ids", async () => {
    nav.pathname = "/o/acme/w/notes/documents/doc-1";
    nav.params = { orgSlug: "acme", workspaceSlug: "notes" };
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByTestId("workspace-switcher"));
    await user.click(await screen.findByTestId("workspace-option-papers"));
    expect(nav.push).toHaveBeenCalledWith("/o/acme/w/papers/documents");
  });

  it("works when storage is blocked", () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    renderShell();
    expect(screen.getByTestId("current-workspace-name").textContent).toBe("Acme Notes");
    spy.mockRestore();
  });

  it("with no workspaces, workspace entries are disabled (not links)", () => {
    renderShell([]);
    const lib = screen.getByTestId("nav-library");
    expect(lib.tagName).toBe("SPAN");
    expect(lib.getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByTestId("nav-search").tagName).toBe("A");
  });

  it("mobile drawer: opens from the menu button, traps focus, closes on Escape and returns focus", async () => {
    const user = userEvent.setup();
    renderShell();
    const menu = screen.getByTestId("menu-button");
    const aside = document.querySelector("aside")!;
    expect(aside.getAttribute("data-open")).toBe("false");
    expect(menu.getAttribute("aria-expanded")).toBe("false");
    expect(menu.getAttribute("aria-controls")).toBe(aside.id);

    await user.click(menu);
    expect(aside.getAttribute("data-open")).toBe("true");
    expect(aside.getAttribute("role")).toBe("dialog");
    expect(aside.getAttribute("aria-modal")).toBe("true");
    expect(menu.getAttribute("aria-expanded")).toBe("true");
    expect(aside.contains(document.activeElement)).toBe(true);
    await user.tab({ shift: true });
    expect(aside.contains(document.activeElement)).toBe(true);

    await user.keyboard("{Escape}");
    expect(aside.getAttribute("data-open")).toBe("false");
    expect(document.activeElement).toBe(menu);
  });

  it("mobile drawer closes on backdrop click and on navigation", async () => {
    const user = userEvent.setup();
    const view = renderShell();
    const aside = document.querySelector("aside")!;
    await user.click(screen.getByTestId("menu-button"));
    await user.click(screen.getByTestId("sidebar-backdrop"));
    expect(aside.getAttribute("data-open")).toBe("false");

    await user.click(screen.getByTestId("menu-button"));
    expect(aside.getAttribute("data-open")).toBe("true");
    nav.pathname = "/o/acme/search";
    view.rerender(
      <OrgScopeProvider org={ORG_A} orgs={[ORG_A, ORG_B]} navigate={vi.fn()}>
        <AppShell userLabel="me@example.com" initialWorkspaces={WS}>
          <h1>Page content</h1>
        </AppShell>
      </OrgScopeProvider>,
    );
    expect(document.querySelector("aside")!.getAttribute("data-open")).toBe("false");
  });
});
