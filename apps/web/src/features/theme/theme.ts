/**
 * Theme preference (`light|dark|system`) kept in localStorage (every access guarded: storage can be blocked) and applied to
 * `<html>`: `data-theme` = the preference, class `dark` = the resolved scheme (the existing CSS variables key off `.dark`),
 * class `reduce-motion` when the user prefers reduced motion. Pure apart from the DOM/storage it is handed.
 */
export type ThemePref = "light" | "dark" | "system";
export const THEME_KEY = "intelo-theme";
export const THEMES: readonly ThemePref[] = ["light", "dark", "system"];

export function readTheme(storage: Pick<Storage, "getItem"> | null | undefined): ThemePref {
  try {
    const v = storage?.getItem(THEME_KEY);
    return v === "light" || v === "dark" || v === "system" ? v : "system";
  } catch {
    return "system";
  }
}

export function writeTheme(storage: Pick<Storage, "setItem"> | null | undefined, pref: ThemePref): void {
  try {
    storage?.setItem(THEME_KEY, pref);
  } catch {
    // storage unavailable: the choice lasts for this page view only
  }
}

export const resolveTheme = (pref: ThemePref, systemDark: boolean): "light" | "dark" => (pref === "system" ? (systemDark ? "dark" : "light") : pref);

export function applyTheme(root: HTMLElement, pref: ThemePref, systemDark: boolean, reducedMotion: boolean): void {
  root.dataset.theme = pref;
  const dark = resolveTheme(pref, systemDark) === "dark";
  root.classList.toggle("dark", dark);
  root.style.colorScheme = dark ? "dark" : "light";
  root.classList.toggle("reduce-motion", reducedMotion);
}

/** Inline script run before hydration so the first paint already has the right scheme (no flash). */
export const THEME_INIT_SCRIPT = `(function(){try{var p=localStorage.getItem(${JSON.stringify(THEME_KEY)});if(p!=="light"&&p!=="dark")p="system";var r=document.documentElement;var d=p==="dark"||(p==="system"&&matchMedia("(prefers-color-scheme: dark)").matches);r.dataset.theme=p;r.classList.toggle("dark",d);r.style.colorScheme=d?"dark":"light";r.classList.toggle("reduce-motion",matchMedia("(prefers-reduced-motion: reduce)").matches)}catch(e){}})();`;
