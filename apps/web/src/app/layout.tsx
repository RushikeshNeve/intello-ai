import type { Metadata } from "next";
import "./globals.css";
import { THEME_INIT_SCRIPT } from "@/features/theme/theme";

export const metadata: Metadata = {
  title: "Universal Knowledge OS",
  description: "Local-first knowledge management.",
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
