// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { applyTheme, readTheme, resolveTheme, THEME_KEY, writeTheme } from "../theme";
import { ThemeToggle } from "../theme-toggle";

function mockMedia(dark: boolean, reduced: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: q.includes("prefers-color-scheme") ? dark : q.includes("prefers-reduced-motion") ? reduced : false,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}
beforeEach(() => {
  localStorage.clear();
  document.documentElement.className = "";
  delete document.documentElement.dataset.theme;
});
afterEach(cleanup);

describe("theme helpers", () => {
  it("reads defensively and resolves 'system'", () => {
    expect(readTheme(null)).toBe("system");
    expect(readTheme({ getItem: () => "dark" })).toBe("dark");
    expect(readTheme({ getItem: () => "neon" })).toBe("system");
    expect(readTheme({ getItem: () => { throw new Error("blocked"); } })).toBe("system");
    expect(() => writeTheme({ setItem: () => { throw new Error("blocked"); } }, "dark")).not.toThrow();
    expect([resolveTheme("system", true), resolveTheme("light", true)]).toEqual(["dark", "light"]);
  });
  it("applyTheme sets data-theme, the dark class and the reduce-motion class", () => {
    applyTheme(document.documentElement, "system", true, true);
    expect(document.documentElement.dataset.theme).toBe("system");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.classList.contains("reduce-motion")).toBe(true);
    applyTheme(document.documentElement, "light", true, false);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.classList.contains("reduce-motion")).toBe(false);
  });
});

describe("ThemeToggle", () => {
  it("persists the choice and applies it; restores it on the next mount; honours reduced motion", async () => {
    mockMedia(false, true);
    const { unmount } = render(<ThemeToggle />);
    expect(document.documentElement.classList.contains("reduce-motion")).toBe(true);
    await userEvent.selectOptions(screen.getByTestId("theme-select"), "dark");
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    unmount();
    document.documentElement.className = "";
    render(<ThemeToggle />);
    expect((screen.getByTestId("theme-select") as HTMLSelectElement).value).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
  it("works when storage throws", async () => {
    mockMedia(true, false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<ThemeToggle />);
    expect(document.documentElement.classList.contains("dark")).toBe(true); // system + dark scheme
    await userEvent.selectOptions(screen.getByTestId("theme-select"), "light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    vi.restoreAllMocks();
  });
});
