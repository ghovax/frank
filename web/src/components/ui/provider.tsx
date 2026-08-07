"use client";

import {
  ChakraProvider,
  ClientOnly,
  createSystem,
  defaultConfig,
  defineConfig,
} from "@chakra-ui/react";
import { ColorModeProvider, type ColorModeProviderProps } from "./color-mode";
import { LocaleProvider } from "@/lib/i18n/locale-provider";
import { PreferencesProvider } from "@/lib/preferences";

// The app's design language as theme-level defaults, so call sites pass a size only to deviate.
const config = defineConfig({
  theme: {
    // Named typography roles, written once and used as `textStyle` rather than repeated per call site.
    textStyles: {
      fieldLabel: { value: { fontSize: "xs", fontWeight: "medium" } },
      sectionLabel: { value: { fontSize: "xs", fontWeight: "semibold", color: "fg.muted" } },
      panelTitle: { value: { fontSize: "sm", fontWeight: "semibold" } },
    },
    semanticTokens: {
      radii: {
        l1: { value: "{radii.md}" },
        l2: { value: "{radii.md}" },
      },
      shadows: {
        panel: {
          value: {
            base: "0 1px 2px rgba(0, 0, 0, 0.04), 0 4px 12px rgba(0, 0, 0, 0.08)",
            _dark: "0 1px 2px rgba(0, 0, 0, 0.3), 0 6px 18px rgba(0, 0, 0, 0.45)",
          },
        },
      },
    },
    recipes: {
      button: { defaultVariants: { size: "xs" } },
      input: { defaultVariants: { size: "xs" } },
      textarea: {
        variants: {
          size: {
            sm: { py: "1.5", lineHeight: "1.375rem", scrollPaddingBottom: "1.5" },
          },
        },
        defaultVariants: { size: "xs" },
      },
    },
    slotRecipes: {
      // Tab triggers clamped to the app's 32px, so every tab list stays in one control family.
      tabs: {
        slots: ["root", "list", "trigger", "content", "indicator"],
        base: {
          trigger: { minH: "8", maxH: "8", py: "1", fontSize: "sm", fontWeight: "medium" },
        },
      },
      dialog: {
        slots: [
          "backdrop",
          "positioner",
          "content",
          "title",
          "description",
          "header",
          "body",
          "footer",
          "closeTrigger",
        ],
        base: {
          // A dialog is a card on a wide screen and a screen on a narrow one.
          positioner: {
            padding: { base: "0", sm: "4" },
            alignItems: { base: "stretch", sm: "center" },
          },
          content: {
            borderRadius: { base: "0", sm: "md" },
            maxWidth: { base: "100%", sm: undefined },
            width: { base: "100%", sm: undefined },
            height: { base: "100dvh", sm: "auto" },
            maxHeight: { base: "100dvh", sm: "85dvh" },
            display: "flex",
            flexDirection: "column",
          },
          // Header and footer stay put while the body scrolls, so the title never leaves with the content.
          header: {
            px: "4",
            pt: { base: "calc(env(safe-area-inset-top) + 1rem)", sm: "4" },
            pb: "3",
            gap: "2",
            flexShrink: 0,
          },
          body: { px: "4", pt: "1", pb: "4", flex: "1", minHeight: 0, overflowY: "auto" },
          footer: {
            px: "4",
            pt: "1",
            pb: { base: "calc(env(safe-area-inset-bottom) + 1rem)", sm: "4" },
            gap: "2",
            flexShrink: 0,
          },
        },
      },
      // One dropdown row for the whole app, since menus and selects read the same to a user.
      menu: {
        slots: [
          "arrow",
          "arrowTip",
          "content",
          "contextTrigger",
          "indicator",
          "item",
          "itemGroup",
          "itemGroupLabel",
          "itemIndicator",
          "itemText",
          "positioner",
          "separator",
          "trigger",
          "triggerItem",
          "itemCommand",
        ],
        variants: {
          size: {
            sm: {
              item: { gap: "2", py: "1.5", px: "2", fontWeight: "medium" },
              itemGroupLabel: { textStyle: "xs", color: "fg.muted" },
            },
          },
        },
      },
      select: {
        slots: [
          "label",
          "positioner",
          "trigger",
          "indicator",
          "clearTrigger",
          "item",
          "itemText",
          "itemIndicator",
          "itemGroup",
          "itemGroupLabel",
          "list",
          "content",
          "root",
          "control",
          "valueText",
          "indicatorGroup",
        ],
        variants: {
          size: {
            xs: {
              content: { gap: "0" },
              item: { py: "1.5", fontWeight: "medium" },
            },
          },
        },
      },
    },
  },
});

const system = createSystem(defaultConfig, config);

export function Provider(props: ColorModeProviderProps) {
  return (
    <ClientOnly fallback={null}>
      <ChakraProvider value={system}>
        {/* Nothing below renders until the daemon has said what it remembers, since the theme and locale come from it. */}
        <PreferencesProvider>
          <LocaleProvider>
            <ColorModeProvider {...props} />
          </LocaleProvider>
        </PreferencesProvider>
      </ChakraProvider>
    </ClientOnly>
  );
}
