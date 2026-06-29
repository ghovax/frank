import type { StorybookConfig } from "@storybook/react-vite";
import { resolve } from "node:path";
import tsconfigPaths from "vite-tsconfig-paths";

const stub = (name: string) => resolve(process.cwd(), ".storybook/stubs", name);

const config: StorybookConfig = {
  framework: "@storybook/react-vite",
  stories: ["../src/**/*.stories.@(ts|tsx|mdx)"],
  addons: ["@storybook/addon-essentials"],
  viteFinal: async (config) => {
    // Resolve the `@/*` -> `./src/*` path alias the components use.
    config.plugins = [...((config.plugins as unknown[]) ?? []), tsconfigPaths()];

    // react-syntax-highlighter's Prism build and its deep style import bundle
    // fine under Next's webpack but break (or stall the optimizer) under Vite.
    // Alias both to trivial stubs in Storybook so markdown code blocks render as
    // plain <pre><code> — enough to inspect layout/behavior. The deep-path alias
    // must come before the package alias (Vite matches the first prefix hit).
    config.resolve = config.resolve ?? {};
    const existing = Array.isArray(config.resolve.alias) ? config.resolve.alias : [];
    config.resolve.alias = [
      { find: "react-syntax-highlighter/dist/esm/styles/hljs", replacement: stub("syntax-highlight-styles.ts") },
      { find: "react-syntax-highlighter", replacement: stub("syntax-highlighter.tsx") },
      ...existing,
    ];
    return config;
  },
};

export default config;
