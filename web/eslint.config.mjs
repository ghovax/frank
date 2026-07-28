import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "@chakra-ui/react",
              importNames: ["chakra"],
              message: "Use the configured Chakra component (for example Button or IconButton) instead of the raw chakra factory.",
            },
          ],
        },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXOpeningElement[name.name=/^(a|article|aside|b|button|canvas|code|del|div|em|footer|form|h1|h2|h3|h4|h5|h6|header|iframe|img|input|label|li|main|nav|ol|p|pre|section|select|span|strong|table|tbody|td|textarea|tfoot|th|thead|tr|ul)$/]",
          message: "Use the configured Chakra component or a shared UI root instead of a native semantic element.",
        },
        {
          selector: "JSXAttribute[name.name='as'][value.value=/^(a|article|aside|b|button|canvas|code|del|div|em|footer|form|iframe|img|input|label|li|main|nav|p|pre|section|select|span|strong|table|tbody|td|textarea|tfoot|th|thead|tr|ul)$/]",
          message: "Use the configured Chakra component or a shared UI root instead of changing another primitive's semantic element.",
        },
        {
          selector: "JSXAttribute[name.name='role'][value.value='button']",
          message: "Use the configured Chakra Button or IconButton component instead of recreating button semantics.",
        },
        {
          selector: "JSXAttribute[name.name='borderRadius'][value.value=/^(0|-?[0-9]+(\\.[0-9]+)?(px|rem|em))$/]",
          message: "Use a Chakra radius token such as md, sm, full, or none instead of a literal CSS radius.",
        },
        {
          selector: "JSXAttribute[name.name='borderRadius'] > JSXExpressionContainer > Literal[value=0]",
          message: "Use the Chakra none radius token instead of a numeric zero radius.",
        },
      ],
    },
  },
  {
    // An error that is caught and discarded leaves nothing behind — no log, no trace, no
    // way to find it without a reproduction. The interface accumulated 113 of these and
    // then a broken live stream cost days, because the browser was failing silently while
    // every server log said the work had succeeded. An empty catch must instead say which
    // it is, through `lib/swallowed`: `expected(why)` when silence is the right answer, or
    // `swallowed(context, error)` when it is not but the code can carry on.
    //
    // A warning rather than an error: the existing sites are being converted as they are
    // touched, and a wall of build failures would only get the rule turned off.
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "no-empty": ["warn", { allowEmptyCatch: false }],
    },
  },
  {
    // This is the sole factory boundary for semantic roots Chakra does not export.
    files: ["src/components/ui/semantic.tsx"],
    rules: {
      "no-restricted-imports": "off",
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "storybook-static/**",
    "src-tauri/server-bin/**",
    "src-tauri/target/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;
