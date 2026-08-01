import path from "node:path";

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
  // Absolute in dev so a Tauri window loading from `tauri://` can find the assets — but *not*
  // when something is proxying this, where the page is already on one origin and an absolute
  // `localhost:3000` is a machine the phone holding it does not have. `frank reach --interface`
  // sets this.
  assetPrefix: isProduction || process.env.FRANK_PROXY_ENABLED ? undefined : `http://${internalHost}:${devPort}`,
  // `shared/` sits beside `web/`, not inside it, because the phone imports it too. Next resolves
  // modules from the project directory down, so both halves of this are needed: the root widens
  // what the bundler will look at, and the alias is what `@shared/...` means. TypeScript is told
  // separately, in `tsconfig.json` — the two have to agree, and neither reads the other.
  turbopack: {
    root: path.resolve(__dirname, ".."),
    resolveAlias: {
      "@shared": path.resolve(__dirname, "../shared"),
    },
  },
  experimental: {
    optimizePackageImports: ["@chakra-ui/react"],
  },
};

export default nextConfig;
