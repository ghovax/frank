import type { Preview } from "@storybook/react";
import { ChakraProvider, defaultSystem } from "@chakra-ui/react";
import "katex/dist/katex.min.css";

// Mirror the app's provider (src/components/ui/provider.tsx) so components render
// with the real Chakra v3 system + tokens. ClientOnly/next-themes are SSR-only
// concerns and are skipped in the browser-only Storybook canvas.
const preview: Preview = {
  parameters: {
    layout: "padded",
    backgrounds: {
      default: "canvas",
      values: [
        { name: "canvas", value: "#F5F5F5" },
        { name: "white", value: "#FFFFFF" },
      ],
    },
  },
  decorators: [
    (Story) => (
      <ChakraProvider value={defaultSystem}>
        <Story />
      </ChakraProvider>
    ),
  ],
};

export default preview;
