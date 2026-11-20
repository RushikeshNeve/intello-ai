/** First focusable element on every shell page: jumps to `<main id="main">`. Visible only when focused. */
export function SkipLink() {
  return (
    <a
      href="#main"
      className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[70] focus:rounded-control focus:bg-primary focus:px-4 focus:py-2 focus:text-sm focus:font-semibold focus:text-primary-foreground focus:outline-2 focus:outline-offset-2 focus:outline-ring"
    >
      Skip to content
    </a>
  );
}
