"use client";

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";

/**
 * After a client-side navigation, move focus to the new page's main heading (or `<main>`), so keyboard and
 * screen-reader users start at the new content instead of a stale link. The first load is left alone.
 */
export function RouteFocus() {
  const pathname = usePathname();
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const target =
      document.querySelector<HTMLElement>("main h1[tabindex]") ?? document.querySelector<HTMLElement>("main");
    if (target) {
      if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
      target.focus({ preventScroll: false });
    }
  }, [pathname]);
  return null;
}
