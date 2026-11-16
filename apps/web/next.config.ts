import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle (`.next/standalone/server.js`) for the container image.
  output: "standalone",
  cacheComponents: true,
  partialPrefetching: true,
  poweredByHeader: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
