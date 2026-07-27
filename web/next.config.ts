import type { NextConfig } from "next";

// The desktop app (Tauri) bundles the UI as a static export — Tauri serves the
// pre-built `out/` directory and cannot run a Node server, so SSR/route handlers
// are off. The UI is already a pure client-side SPA (all data comes from the
// harness server over HTTP/SSE), so static export changes nothing at runtime.
const isProduction = process.env.NODE_ENV === "production";
// Tauri sets TAURI_DEV_HOST when serving the dev UI to a device on the LAN
// (e.g. mobile); assets must then resolve against that host rather than localhost.
// FRANK_PORT lets a developer run the dev server on a different port (default
// 3000); assets must use the same port the server is bound to.
const internalHost = process.env.TAURI_DEV_HOST || "localhost";
const devPort = process.env.FRANK_PORT || "3000";

const nextConfig: NextConfig = {
  output: "export",
  // Emit each route as `<route>/index.html` (not `<route>.html`) so a plain file server
  // — including Tauri's asset server — resolves a bare `/projects` (or a deep link / hard
  // reload to `/projects/?id=…`) to `projects/index.html`. Without this a deep link 404s
  // in the packaged app even though it works under `next dev`.
  trailingSlash: true,
  // next/image optimization needs a server; static export requires unoptimized.
  images: {
    unoptimized: true,
  },
  assetPrefix: isProduction ? undefined : `http://${internalHost}:${devPort}`,
  experimental: {
    optimizePackageImports: ["@chakra-ui/react"],
  },
};

export default nextConfig;
