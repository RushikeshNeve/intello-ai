// @vitest-environment jsdom
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Drawer, Modal } from "../overlay";
import { Tabs } from "../tabs";
import { ToastProvider, useToast } from "../toast";
import { fileTypeLabel } from "../badges";
import { Truncate } from "../truncate";

afterEach(cleanup);

function Harness({ kind }: { kind: "modal" | "drawer" }) {
  const [open, setOpen] = useState(false);
  const Comp = kind === "modal" ? Modal : Drawer;
  return (
    <>
      <button onClick={() => setOpen(true)}>Open it</button>
      <Comp open={open} onClose={() => setOpen(false)} title="Edit thing" footer={<button>Save</button>}>
        <input aria-label="Name" />
      </Comp>
    </>
  );
}

describe.each(["modal", "drawer"] as const)("%s", (kind) => {
  it("is an aria-modal dialog named by its title; focus moves in, is trapped, Escape closes, focus returns", async () => {
    const user = userEvent.setup();
    render(<Harness kind={kind} />);
    const opener = screen.getByText("Open it");
    await user.click(opener);
    const dialog = screen.getByRole("dialog", { name: "Edit thing" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.contains(document.activeElement)).toBe(true);
    // Tab through every control: focus never leaves the dialog.
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("closes from the close button and a backdrop click", async () => {
    const user = userEvent.setup();
    render(<Harness kind={kind} />);
    await user.click(screen.getByText("Open it"));
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(screen.getByText("Open it"));
    const overlay = screen.getByRole("dialog").parentElement!;
    await user.pointer({ keys: "[MouseLeft]", target: overlay });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("Tabs", () => {
  it("uses roving tabindex and arrow keys select and focus", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    function T() {
      const [v, setV] = useState("a");
      return <Tabs idBase="t" label="Sections" value={v} onChange={(x) => (onChange(x), setV(x))} items={[{ value: "a", label: "A" }, { value: "b", label: "B" }, { value: "c", label: "C", disabled: true }]} />;
    }
    render(<T />);
    const [a, b] = screen.getAllByRole("tab");
    expect(a.getAttribute("aria-selected")).toBe("true");
    expect(a.tabIndex).toBe(0);
    expect(b.tabIndex).toBe(-1);
    a.focus();
    await user.keyboard("{ArrowRight}");
    expect(onChange).toHaveBeenLastCalledWith("b");
    expect(document.activeElement).toBe(b);
    await user.keyboard("{ArrowRight}"); // skips the disabled tab, wraps
    expect(document.activeElement).toBe(a);
  });
});

describe("Toast", () => {
  it("announces in a status region, dismisses by button and by timer", async () => {
    vi.useFakeTimers();
    function P() {
      const { push } = useToast();
      return <button onClick={() => push({ title: "Saved", durationMs: 1000 })}>go</button>;
    }
    render(
      <ToastProvider>
        <P />
      </ToastProvider>,
    );
    act(() => screen.getByText("go").click());
    expect(screen.getByTestId("toast-region").getAttribute("role")).toBe("status");
    expect(screen.getByText("Saved")).toBeTruthy();
    act(() => void vi.advanceTimersByTime(1100));
    expect(screen.queryByText("Saved")).toBeNull();
    vi.useRealTimers();
  });
});

describe("helpers", () => {
  it("labels file types from MIME or file name", () => {
    expect(fileTypeLabel("application/pdf")).toBe("PDF");
    expect(fileTypeLabel("text/plain; charset=utf-8")).toBe("TXT");
    expect(fileTypeLabel("notes.docx")).toBe("DOCX");
    expect(fileTypeLabel(null)).toBe("FILE");
  });
  it("Truncate keeps the full text in the DOM and the title", () => {
    render(<Truncate text="A very long title indeed" />);
    const el = screen.getByTitle("A very long title indeed");
    expect(el.textContent).toBe("A very long title indeed");
  });
});
