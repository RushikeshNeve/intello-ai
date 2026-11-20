// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { buildItems, moveActive } from "../model";
import { PaletteView } from "../command-palette";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
afterEach(cleanup);

const ctx = {
  orgSlug: "acme",
  orgs: [
    { slug: "acme", name: "Acme" },
    { slug: "globex", name: "Globex" },
  ],
  workspaces: [{ slug: "notes", name: "Notes" }],
};

describe("palette model", () => {
  it("lists navigation targets and 'New chat'; a query adds 'Search for' and filters", () => {
    const all = buildItems(ctx, "").map((i) => i.id);
    expect(all).toEqual(expect.arrayContaining(["new-chat", "nav-memory", "nav-research", "docs-notes", "graph-notes", "org-globex"]));
    expect(all).not.toContain("search-for");
    const q = buildItems(ctx, "gra");
    expect(q.map((i) => i.id)).toEqual(["search-for", "graph-notes"]);
    expect(q[0].href).toBe("/o/acme/search?q=gra");
  });
  it("arrow movement wraps", () => {
    expect([moveActive(0, -1, 3), moveActive(2, 1, 3), moveActive(0, 1, 0)]).toEqual([2, 0, -1]);
  });
});

describe("PaletteView keyboard", () => {
  it("opens with Ctrl+K, filters, navigates with arrows + Enter, traps Tab, closes on Esc and restores focus", async () => {
    const navigate = vi.fn();
    render(<PaletteView ctx={ctx} navigate={navigate} />);
    const trigger = screen.getByTestId("palette-trigger");
    trigger.focus();
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const input = await screen.findByTestId("palette-input");
    expect(document.activeElement).toBe(input);
    expect(screen.getByRole("dialog").getAttribute("aria-modal")).toBe("true");

    await userEvent.keyboard("{Tab}");
    expect(document.activeElement).toBe(input); // focus trap

    await userEvent.type(input, "mem");
    const opts = screen.getAllByTestId("palette-option");
    expect(opts.map((o) => o.textContent)).toEqual([expect.stringContaining('Search for "mem"'), expect.stringContaining("Memory")]);
    expect(opts[0].getAttribute("aria-selected")).toBe("true");
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getAllByTestId("palette-option")[1].getAttribute("aria-selected")).toBe("true");
    expect(input.getAttribute("aria-activedescendant")).toBe(screen.getAllByTestId("palette-option")[1].id);
    await userEvent.keyboard("{Enter}");
    expect(navigate).toHaveBeenCalledWith("/o/acme/memory");
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("'Search for' runs the typed query", async () => {
    const navigate = vi.fn();
    render(<PaletteView ctx={ctx} navigate={navigate} />);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    await userEvent.type(await screen.findByTestId("palette-input"), "q3 budget{Enter}");
    expect(navigate).toHaveBeenCalledWith("/o/acme/search?q=q3%20budget");
  });
});
