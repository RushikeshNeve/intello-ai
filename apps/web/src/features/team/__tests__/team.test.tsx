// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TeamApp } from "../team-app";
import { assignableOrgRoles, parseMember } from "../api";
import { jsonResponse, mount, O, orgWith, W, WS_ADMIN, WS_VIEWER } from "@/features/settings/__tests__/helpers";

afterEach(cleanup);

const members = [
  { user_id: "u-me", email: "me@x.io", display_name: "Me", role: "admin" },
  { user_id: "u-own", email: "own@x.io", display_name: "Olive Owner", role: "owner" },
  { user_id: "u-mem", email: "mem@x.io", display_name: "Mem Ber", role: "member" },
];
const base = (extra: Record<string, () => Response> = {}) => ({
  [`GET ${O}/members`]: () => jsonResponse(200, { items: members }),
  "GET /v1/me": () => jsonResponse(200, { id: "u-me", email: "me@x.io", display_name: "Me" }),
  ...extra,
});

describe("team api", () => {
  it("parses members and computes assignable roles", () => {
    expect(parseMember(members[0]).role).toBe("admin");
    expect(() => parseMember({ user_id: "u", role: "god" })).toThrow();
    expect(assignableOrgRoles("owner", "admin")).toEqual(["owner", "admin", "member"]);
    expect(assignableOrgRoles("admin", "owner")).toEqual([]);
    expect(assignableOrgRoles("admin", "member")).toEqual(["admin", "member"]);
    expect(assignableOrgRoles("member", "member")).toEqual([]);
  });
});

describe("TeamApp", () => {
  it("lists members; an admin can edit members but not owners or self", async () => {
    mount(<TeamApp org={orgWith("admin")} workspace={WS_ADMIN} />, base());
    expect(screen.getByRole("status", { name: /loading members/i })).toBeTruthy();
    const rows = await screen.findAllByTestId("member-row");
    expect(rows).toHaveLength(3);
    await waitFor(() => expect(within(rows[0]).getByText("(you)")).toBeTruthy());
    expect(within(rows[0]).queryByRole("combobox")).toBeNull(); // self
    expect(within(rows[1]).queryByRole("combobox")).toBeNull(); // owner, actor admin
    expect(within(rows[1]).getByText("Owner")).toBeTruthy();
    expect(within(rows[2]).getByRole("combobox", { name: /role of mem ber/i })).toBeTruthy();
  });

  it("changes a role with PUT and removes after confirmation", async () => {
    const put = vi.fn(() => jsonResponse(200, { role: "admin" }));
    const del = vi.fn(() => jsonResponse(204, null));
    mount(<TeamApp org={orgWith("owner")} workspace={WS_ADMIN} />, base({ [`PUT ${O}/members/u-mem`]: put, [`DELETE ${O}/members/u-mem`]: del }));
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: /role of mem ber/i }), "admin");
    await waitFor(() => expect(put).toHaveBeenCalled());
    await userEvent.click(screen.getByRole("button", { name: /remove mem ber/i }));
    expect(del).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId("confirm-remove-member"));
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
  });

  it("a plain member sees a read-only list and no workspace controls", async () => {
    mount(<TeamApp org={orgWith("member")} workspace={WS_VIEWER} />, base());
    await screen.findAllByTestId("member-row");
    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
    expect(screen.getByTestId("members-readonly")).toBeTruthy();
    expect(screen.getByTestId("workspace-readonly")).toBeTruthy();
  });

  it("shows the error state with retry", async () => {
    mount(<TeamApp org={orgWith("owner")} workspace={WS_ADMIN} />, { [`GET ${O}/members`]: () => jsonResponse(503, { detail: "x" }) });
    expect(await screen.findByTestId("members-error")).toBeTruthy();
  });

  it("sets a workspace role by user id", async () => {
    const put = vi.fn(() => jsonResponse(200, { role: "editor" }));
    mount(<TeamApp org={orgWith("owner")} workspace={WS_ADMIN} />, base({ [`PUT ${W}/members/u-mem`]: put }));
    await screen.findAllByTestId("member-row");
    await userEvent.selectOptions(screen.getByLabelText("Person"), "u-mem");
    await userEvent.selectOptions(screen.getByLabelText("Workspace role"), "editor");
    await userEvent.click(screen.getByTestId("ws-set-role"));
    await waitFor(() => expect(put).toHaveBeenCalled());
  });

  it("pre-approves by email, lists the pending entry and removes it", async () => {
    let pending: unknown[] = [];
    const post = vi.fn(() => {
      pending = [{ id: "p-1", email: "New.Person@x.io", role: "member", created_at: "2026-11-20T00:00:00Z" }];
      return jsonResponse(201, { status: "pending", member: null, preapproval: pending[0] });
    });
    const del = vi.fn(() => {
      pending = [];
      return jsonResponse(204, null);
    });
    const api = mount(
      <TeamApp org={orgWith("admin")} workspace={WS_ADMIN} />,
      base({
        [`GET ${O}/members/preapprovals`]: () => jsonResponse(200, { items: pending }),
        [`POST ${O}/members/preapprovals`]: post,
        [`DELETE ${O}/members/preapprovals/p-1`]: del,
      }),
    );
    await userEvent.click(screen.getByTestId("invite-open"));
    const dialog = screen.getByRole("dialog", { name: /invite a member/i });
    expect(within(dialog).getByText(/no email is sent/i)).toBeTruthy();
    expect(within(dialog).queryByRole("option", { name: "Owner" })).toBeNull();
    await userEvent.type(within(dialog).getByLabelText("Email address"), "New.Person@x.io");
    await userEvent.click(within(dialog).getByTestId("invite-send"));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(api.calls.find((c) => c.method === "POST")?.body).toBe(JSON.stringify({ email: "New.Person@x.io", role: "member" }));
    await within(dialog).findByText("New.Person@x.io");
    await userEvent.click(within(dialog).getByRole("button", { name: /remove pending access for new.person@x.io/i }));
    await waitFor(() => expect(del).toHaveBeenCalled());
    await waitFor(() => expect(within(dialog).queryByText("New.Person@x.io")).toBeNull());
  });
});
