import type { Metadata } from "next";
import "./globals.css";
import { THEME_INIT_SCRIPT } from "@/features/theme/theme";

export const metadata: Metadata = {
  title: { default: "Intello", template: "%s | Intello" },
  description: "Intello AI: a knowledge workspace that answers from your own sources, with evidence.",
  // Canonical brand mark (public/brand). The monochrome file is for contexts that need a single colour.
  icons: { icon: [{ url: "/brand/intello-prism.svg", type: "image/svg+xml" }] },
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="flex min-h-full flex-col bg-background text-foreground">{children}</body>
    </html>
  );
}
