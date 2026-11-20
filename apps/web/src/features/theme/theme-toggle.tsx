"use client";

import { useEffect, useState } from "react";
import { applyTheme, readTheme, THEMES, writeTheme, type ThemePref } from "./theme";

const LABEL: Record<ThemePref, string> = { light: "Light", dark: "Dark", system: "System" };

function store(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** Light / dark / system switch. Follows `prefers-color-scheme` while on "system" and `prefers-reduced-motion` always. */
export function ThemeToggle() {
  const [pref, setPref] = useState<ThemePref>("system");

  useEffect(() => {
    const initial = readTheme(store());
    setPref(initial); // eslint-disable-line react-hooks/set-state-in-effect -- storage is only readable after mount
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => applyTheme(document.documentElement, readTheme(store()), scheme.matches, motion.matches);
    sync();
    scheme.addEventListener("change", sync);
    motion.addEventListener("change", sync);
    return () => {
      scheme.removeEventListener("change", sync);
      motion.removeEventListener("change", sync);
    };
  }, []);

  function choose(next: ThemePref) {
    writeTheme(store(), next);
    setPref(next);
    applyTheme(
      document.documentElement,
      next,
      window.matchMedia("(prefers-color-scheme: dark)").matches,
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    );
  }

  return (
    <label className="flex items-center gap-1 text-xs text-muted-foreground">
      <span className="sr-only sm:not-sr-only">Theme</span>
      <select
        className="h-7 rounded-md border border-input bg-transparent px-1 text-xs text-foreground"
        value={pref}
        onChange={(e) => choose(e.target.value as ThemePref)}
        data-testid="theme-select"
        aria-label="Theme"
      >
        {THEMES.map((t) => (
          <option key={t} value={t}>
            {LABEL[t]}
          </option>
        ))}
      </select>
    </label>
  );
}
