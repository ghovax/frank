import path from "node:path";

import type { NextConfig } from "next";

// The desktop app bundles the interface as a static export, so server rendering and route handlers are off.
const isProduction = process.env.NODE_ENV === "production";
// Assets must resolve against the development host when the interface is served to a device on the network.
const internalHost = process.env.TAURI_DEV_HOST || "localhost";
const devPort = process.env.DEV_PORT || "3000";

const nextConfig: NextConfig = {
  output: "export",
  // Emit each route as its own `index.html`, so a plain file server resolves a bare path.
  trailingSlash: true,
  // next/image optimization needs a server; static export requires unoptimized.
  images: {
    unoptimized: true,
  },
  // Absolute in development so a window loading from a custom scheme finds the assets, but never behind a proxy.
  assetPrefix: isProduction || process.env.PROXY_ENABLED ? undefined : `http://${internalHost}:${devPort}`,
  // `shared/` sits beside `web/` because the phone imports it too, so both halves of the resolution are needed.
  turbopack: {
    root: path.resolve(__dirname, ".."),
    resolveAlias: {
      "@shared": path.resolve(__dirname, "../shared"),
    },
  },
  // The development badge is a floating button that would sit over the interface on a phone.
  devIndicators: false,
  experimental: {
    optimizePackageImports: ["@chakra-ui/react"],
  },
};

export default nextConfig;
