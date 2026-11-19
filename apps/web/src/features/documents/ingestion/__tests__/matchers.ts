import { expect } from "vitest";

/** The few DOM matchers these tests use (the repo does not ship jest-dom). */
type R = { pass: boolean; message: () => string };
const ok = (pass: boolean, message: string): R => ({ pass, message: () => message });

expect.extend({
  toHaveTextContent(el: Element, text: string) {
    const got = (el.textContent ?? "").replace(/\s+/g, " ");
    return ok(got.includes(text), `expected text "${got}" to include "${text}"`);
  },
  toHaveAttribute(el: Element, name: string, value?: string) {
    const got = el.getAttribute(name);
    return ok(value === undefined ? got !== null : got === value, `expected [${name}] = ${value}, got ${got}`);
  },
  toBeDisabled(el: HTMLButtonElement) {
    return ok(el.disabled, "expected element to be disabled");
  },
  toBeEnabled(el: HTMLButtonElement) {
    return ok(!el.disabled, "expected element to be enabled");
  },
  toBeInTheDocument(el: Element) {
    return ok(el.ownerDocument.contains(el), "expected element to be in the document");
  },
  toBeEmptyDOMElement(el: Element) {
    return ok(el.innerHTML === "", `expected empty element, got ${el.innerHTML}`);
  },
  toHaveFocus(el: Element) {
    return ok(el.ownerDocument.activeElement === el, "expected element to have focus");
  },
});

declare module "vitest" {
  interface Assertion {
    toHaveTextContent(text: string): void;
    toHaveAttribute(name: string, value?: string): void;
    toBeDisabled(): void;
    toBeEnabled(): void;
    toBeInTheDocument(): void;
    toBeEmptyDOMElement(): void;
    toHaveFocus(): void;
  }
}
