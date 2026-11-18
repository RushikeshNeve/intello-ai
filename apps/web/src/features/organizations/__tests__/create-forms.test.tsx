// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { queryKeys } from "@/lib/query/keys";
import { OrgDirectory } from "../components/org-directory";
import { OrgScopeProvider } from "../components/org-scope";
import { WorkspaceOverview } from "@/features/workspaces/components/workspace-overview";
import { fakeFetcher, jsonResponse, ORG_A, ORG_B, renderWithQuery, ws } from "./helpers";
import { isValidSlug, slugify } from "../slug";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/home",
  useParams: () => ({}),
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

afterEach(cleanup);

describe("slug helpers", () => {
  it("validates and suggests slugs", () => {
    expect(isValidSlug("acme-lab")).toBe(true);
    expect(isValidSlug("a")).toBe(false);
    expect(isValidSlug("-acme")).toBe(false);
    expect(isValidSlug("Acme")).toBe(false);
    expect(slugify("  Café Research Lab! ")).toBe("cafe-research-lab");
  });
});

describe("/home org directory", () => {
  it("shows an empty state, then lists orgs linking to /o/{slug}", () => {
    const { fetcher } = fakeFetcher({});
    const first = renderWithQuery(<OrgDirectory initialOrgs={[]} navigate={vi.fn()} />, { fetcher });
    expect(screen.getByTestId("orgs-empty").textContent).toMatch(/not a member of any organization/);
    first.unmount();

    renderWithQuery(<OrgDirectory initialOrgs={[ORG_A, ORG_B]} navigate={vi.fn()} />, { fetcher });
    const list = screen.getByTestId("org-list");
    expect(within(list).getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["/o/acme", "/o/globex"]);
    expect(screen.getByRole("heading", { level: 2, name: "Your organizations" })).toBeTruthy();
  });

  it("creates an org: 409 slug_taken is shown inline on the slug field, success navigates to the org", async () => {
    const user = userEvent.setup();
    let taken = true;
    const { fetcher, calls } = fakeFetcher({
      "POST /v1/orgs": (init) => {
        if (taken) return jsonResponse(409, { detail: "slug_taken" });
        const body = JSON.parse(String(init?.body));
        return jsonResponse(201, { id: "33333333-3333-4333-8333-333333333333", ...body, created_at: "2026-11-16T00:00:00Z", role: "owner" });
      },
    });
    const navigate = vi.fn();
    const { client } = renderWithQuery(<OrgDirectory initialOrgs={[ORG_A]} navigate={navigate} />, { fetcher });

    const form = screen.getByRole("form", { name: "Create organization" });
    await user.type(within(form).getByLabelText("Name"), "Acme Research");
    const slug = within(form).getByLabelText("Slug") as HTMLInputElement;
    expect(slug.value).toBe("acme-research"); // suggested from the name
    await user.click(within(form).getByRole("button", { name: "Create organization" }));

    const alert = await within(form).findByRole("alert");
    expect(alert.textContent).toMatch(/slug is already in use/);
    expect(slug.getAttribute("aria-invalid")).toBe("true");
    expect(slug.getAttribute("aria-describedby")).toContain(alert.id);
    await waitFor(() => expect(document.activeElement).toBe(slug));
    expect(navigate).not.toHaveBeenCalled();

    taken = false;
    await user.clear(slug);
    await user.type(slug, "acme-2");
    await user.click(within(form).getByRole("button", { name: "Create organization" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/o/acme-2"));
    expect(JSON.parse(calls.at(-1)!.body!)).toEqual({ name: "Acme Research", slug: "acme-2" });
    expect(client.getQueryData<typeof ORG_A[]>(queryKeys.orgs())?.map((o) => o.slug)).toEqual(["acme", "acme-2"]);
  });

  it("validates client-side before calling the API", async () => {
    const user = userEvent.setup();
    const { fetcher } = fakeFetcher({});
    renderWithQuery(<OrgDirectory initialOrgs={[]} navigate={vi.fn()} />, { fetcher });
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(screen.getByText("Enter a name.")).toBeTruthy();
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("org page: create workspace", () => {
  const A_WS = [ws(ORG_A, "notes", "Acme Notes")];

  it("is hidden for members", () => {
    const { fetcher } = fakeFetcher({});
    renderWithQuery(
      <OrgScopeProvider org={{ ...ORG_A, role: "member" }} orgs={[ORG_A]}>
        <WorkspaceOverview initialWorkspaces={A_WS} />
      </OrgScopeProvider>,
      { fetcher },
    );
    expect(screen.queryByRole("form", { name: "Create workspace" })).toBeNull();
    expect(screen.getByRole("link", { name: /Acme Notes/ }).getAttribute("href")).toBe("/o/acme/w/notes");
  });

  it("owner/admin can create; 409 inline; 403 from the API is handled; success updates the shared list", async () => {
    const user = userEvent.setup();
    let mode: "taken" | "forbidden" | "ok" = "taken";
    const { fetcher } = fakeFetcher({
      [`POST /v1/orgs/${ORG_A.id}/workspaces`]: () =>
        mode === "taken"
          ? jsonResponse(409, { detail: "slug_taken" })
          : mode === "forbidden"
            ? jsonResponse(403, { detail: "forbidden" })
            : jsonResponse(201, ws(ORG_A, "papers", "Papers")),
    });
    const { client } = renderWithQuery(
      <OrgScopeProvider org={{ ...ORG_A, role: "admin" }} orgs={[ORG_A]}>
        <WorkspaceOverview initialWorkspaces={A_WS} />
      </OrgScopeProvider>,
      { fetcher },
    );
    const form = screen.getByRole("form", { name: "Create workspace" });
    await user.type(within(form).getByLabelText("Name"), "Papers");
    const submit = within(form).getByRole("button", { name: "Create workspace" });

    await user.click(submit);
    expect((await within(form).findByRole("alert")).textContent).toMatch(/slug is already in use/);

    mode = "forbidden";
    await user.click(submit);
    await waitFor(() => expect(within(form).getByRole("alert").textContent).toMatch(/don't have permission/));

    mode = "ok";
    await user.click(submit);
    await screen.findByText("Workspace “Papers” created.");
    expect(screen.getByRole("link", { name: /Papers/ }).getAttribute("href")).toBe("/o/acme/w/papers");
    expect(client.getQueryData<unknown[]>(queryKeys.workspaces(ORG_A.id))).toHaveLength(2);
  });
});
