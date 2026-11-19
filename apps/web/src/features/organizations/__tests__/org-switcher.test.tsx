// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeQueryClient } from "@/lib/query/provider";
import { queryKeys } from "@/lib/query/keys";
import { OrgScopeProvider, ScopedContent } from "../components/org-scope";
import { OrgSwitcher } from "../components/org-switcher";
import { WorkspaceOverview } from "@/features/workspaces/components/workspace-overview";
import { WorkspaceSidebar } from "@/features/workspaces/components/workspace-sidebar";
import { fakeFetcher, jsonResponse, ORG_A, ORG_B, renderWithQuery, ws } from "./helpers";

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

const A_WS = [ws(ORG_A, "notes", "Acme Notes"), ws(ORG_A, "papers", "Acme Papers")];
const B_WS = [ws(ORG_B, "lab", "Globex Bench")];
const A_SECRETS = ["Acme Notes", "Acme Papers"];

afterEach(() => {
  cleanup();
  nav.push.mockReset();
});

function Shell({ org, workspaces, navigate }: { org: typeof ORG_A; workspaces: typeof A_WS; navigate?: (h: string) => void }) {
  return (
    <OrgScopeProvider org={org} orgs={[ORG_A, ORG_B]} navigate={navigate}>
      <header>
        <OrgSwitcher />
      </header>
      <ScopedContent>
        <WorkspaceSidebar initialWorkspaces={workspaces} />
        <main>
          <WorkspaceOverview initialWorkspaces={workspaces} />
        </main>
      </ScopedContent>
    </OrgScopeProvider>
  );
}

function routes() {
  return fakeFetcher({
    [`GET /v1/orgs/${ORG_A.id}/workspaces`]: () => jsonResponse(200, { items: A_WS }),
    [`GET /v1/orgs/${ORG_B.id}/workspaces`]: () => jsonResponse(200, { items: B_WS }),
  });
}

describe("org switcher", () => {
  it("is a keyboard-operable menu button with radio items for each org", async () => {
    const user = userEvent.setup();
    const { fetcher } = routes();
    renderWithQuery(<Shell org={ORG_A} workspaces={A_WS} navigate={vi.fn()} />, { fetcher });

    const trigger = screen.getByTestId("org-switcher");
    expect(trigger.tagName).toBe("BUTTON");
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(trigger.textContent).toContain("Current organization:");
    expect(screen.getByTestId("current-org-name").textContent).toBe("Acme Research");
    expect(screen.getByTestId("current-org-role").textContent).toContain("Owner");

    trigger.focus();
    await user.keyboard("{Enter}");
    const menu = await screen.findByRole("menu");
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const items = within(menu).getAllByRole("menuitemradio");
    expect(items.map((i) => i.textContent)).toEqual(["Acme ResearchOwner", "Globex LabMember"]);
    expect(items[0].getAttribute("aria-checked")).toBe("true");
    expect(items[1].getAttribute("aria-checked")).toBe("false");
    expect(within(menu).getAllByRole("group").length).toBeGreaterThan(0);
    expect(within(menu).getByRole("menuitem", { name: "All organizations" })).toBeTruthy();

    // Arrow keys move focus between items.
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(document.activeElement).toBe(items[1]));

    // Escape closes and returns focus to the trigger.
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("switching org clears the query cache before navigating and never shows the previous org's data", async () => {
    const user = userEvent.setup();
    const { fetcher, calls } = routes();
    const client = makeQueryClient();
    const clearSpy = vi.spyOn(client, "clear");

    // Record every DOM state after the switch starts: none may contain org A's workspace names.
    const seen: string[] = [];
    let recording = false;
    const observer = new MutationObserver(() => {
      if (recording) seen.push(document.body.textContent ?? "");
    });

    const events: string[] = [];
    clearSpy.mockImplementation(function (this: typeof client) {
      events.push("clear");
      return Object.getPrototypeOf(client).clear.call(client);
    });
    const navigate = vi.fn((href: string) => {
      events.push(`navigate:${href}`);
      // At navigation time: A's queries are gone and A's data is not rendered.
      expect(client.getQueryCache().findAll({ queryKey: queryKeys.org(ORG_A.id) })).toHaveLength(0);
      expect(client.getQueryCache().getAll()).toHaveLength(0);
      for (const name of A_SECRETS) expect(document.body.textContent).not.toContain(name);
      expect(screen.getByTestId("current-org-name").textContent).toBe("Globex Lab");
    });

    const view = renderWithQuery(<Shell org={ORG_A} workspaces={A_WS} navigate={navigate} />, { fetcher, client });
    for (const name of A_SECRETS) expect(screen.getAllByText(name).length).toBeGreaterThan(0);
    expect(client.getQueryData(queryKeys.workspaces(ORG_A.id))).toEqual(A_WS);

    observer.observe(document.body, { subtree: true, childList: true, characterData: true });
    screen.getByTestId("org-switcher").focus();
    await user.keyboard("{Enter}");
    await screen.findByRole("menu");
    await waitFor(() => expect(screen.getByRole("menu").contains(document.activeElement)).toBe(true));
    await user.keyboard("{ArrowDown}");
    recording = true;
    await user.keyboard("{Enter}");

    expect(events).toEqual(["clear", "navigate:/o/globex"]);
    expect(navigate).toHaveBeenCalledTimes(1);
    // Scope changed visibly at once; scoped content is replaced by a status message.
    expect(screen.getByTestId("current-org-name").textContent).toBe("Globex Lab");
    expect(screen.getByRole("status").textContent).toContain("Switching to Globex Lab");

    // The router lands on org B: B's server-rendered data shows, A's never comes back.
    view.rerender(
      <Shell org={ORG_B} workspaces={B_WS} navigate={navigate} />,
    );
    await screen.findAllByText("Globex Bench");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    observer.disconnect();

    expect(seen.length).toBeGreaterThan(0);
    for (const snapshot of seen) for (const name of A_SECRETS) expect(snapshot).not.toContain(name);
    for (const name of A_SECRETS) expect(document.body.textContent).not.toContain(name);
    expect(client.getQueryCache().findAll({ queryKey: queryKeys.org(ORG_A.id) })).toHaveLength(0);
    expect(client.getQueryData(queryKeys.workspaces(ORG_B.id))).toEqual(B_WS);
    // Nothing refetched org A after the switch.
    expect(calls.filter((c) => c.path.includes(ORG_A.id))).toHaveLength(0);
    // Members don't get the create-workspace form.
    expect(screen.queryByRole("form", { name: "Create workspace" })).toBeNull();
  });

  it("arriving at another org by any other route (back button, typed URL) drops the other org's queries", async () => {
    const { fetcher } = routes();
    const client = makeQueryClient();
    client.setQueryData(queryKeys.workspaces(ORG_A.id), A_WS);
    client.setQueryData(queryKeys.orgs(), [ORG_A, ORG_B]);
    renderWithQuery(<Shell org={ORG_B} workspaces={B_WS} />, { fetcher, client });
    await screen.findAllByText("Globex Bench");
    expect(client.getQueryData(queryKeys.workspaces(ORG_A.id))).toBeUndefined();
    expect(client.getQueryData(queryKeys.orgs())).toEqual([ORG_A, ORG_B]); // not org-scoped
    for (const name of A_SECRETS) expect(document.body.textContent).not.toContain(name);
  });

  it("selecting the current org does nothing", async () => {
    const user = userEvent.setup();
    const { fetcher } = routes();
    const client = makeQueryClient();
    const clearSpy = vi.spyOn(client, "clear");
    const navigate = vi.fn();
    renderWithQuery(<Shell org={ORG_A} workspaces={A_WS} navigate={navigate} />, { fetcher, client });
    screen.getByTestId("org-switcher").focus();
    await user.keyboard("{Enter}");
    await screen.findByRole("menu");
    await waitFor(() => expect(screen.getByRole("menu").contains(document.activeElement)).toBe(true));
    await user.keyboard("{Enter}");
    expect(navigate).not.toHaveBeenCalled();
    expect(clearSpy).not.toHaveBeenCalled();
    expect(screen.getAllByText("Acme Notes").length).toBeGreaterThan(0);
  });
});

describe("workspace navigation", () => {
  it("marks the active workspace with aria-current=page", () => {
    nav.pathname = "/o/acme/w/papers";
    nav.params = { orgSlug: "acme", workspaceSlug: "papers" };
    const { fetcher } = routes();
    renderWithQuery(<Shell org={ORG_A} workspaces={A_WS} />, { fetcher });
    const navEl = screen.getByRole("navigation", { name: "Workspaces" });
    const links = within(navEl).getAllByRole("link");
    expect(links.map((l) => [l.textContent, l.getAttribute("href"), l.getAttribute("aria-current")])).toEqual([
      ["Overview", "/o/acme", null],
      ["Search", "/o/acme/search", null],
      ["Chat", "/o/acme/chat", null],
      ["Acme Notes", "/o/acme/w/notes", null],
      ["Acme Papers", "/o/acme/w/papers", "page"],
    ]);
  });

  it("marks Overview current on the org page; the mobile toggle exposes aria-expanded/controls", async () => {
    nav.pathname = "/o/acme";
    nav.params = { orgSlug: "acme" };
    const user = userEvent.setup();
    const { fetcher } = routes();
    renderWithQuery(<Shell org={ORG_A} workspaces={A_WS} />, { fetcher });
    const navEl = screen.getByRole("navigation", { name: "Workspaces" });
    expect(within(navEl).getByRole("link", { name: "Overview" }).getAttribute("aria-current")).toBe("page");
    expect(within(navEl).getAllByRole("link").filter((l) => l.getAttribute("aria-current"))).toHaveLength(1);
    const toggle = screen.getByRole("button", { name: "Workspaces" });
    expect(toggle.getAttribute("aria-controls")).toBe(navEl.id);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await user.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });
});
