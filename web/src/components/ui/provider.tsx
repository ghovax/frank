"use client"

import { ChakraProvider, ClientOnly, createSystem, defaultConfig, defineConfig } from "@chakra-ui/react"
import {
  ColorModeProvider,
  type ColorModeProviderProps,
} from "./color-mode"
import { LocaleProvider } from "@/lib/i18n/locale-provider"

// The app's design language lives here as theme-level defaults, so component call sites
// stay lean and only pass size/radius when they intentionally deviate:
//
//   • Radius — Chakra maps a control's corner to the semantic radii `l1/l2/l3` (xs/sm/md).
//     Most controls default to `l2` (= sm). We remap `l1` and `l2` to `md` so `md` is the
//     universal default corner (`l3` is already `md`); only `sm` (tiny chips), `full`
//     (dots/pills/avatars) and `none` (joined segmented buttons) are ever set explicitly.
//   • Size — the compact `xs` control height is the house default, set once via each
//     control recipe's `defaultVariants`, instead of `size="xs"` on every button/input.
//   • Dialogs — the stock dialog recipe is roomy (px6/pt6 headers, l3 corners); tightened
//     here to compact padding + md corners so every dialog inherits it.
//   • Elevation — the `panel` shadow is a soft, ring-free drop shadow for the floating
//     cards. Stock Chakra shadow tokens bake in a crisp `0 0 1px` hairline ring which,
//     layered on a card's own 1px border, renders as a doubled border line; this token is
//     pure blur so the real border stays the only crisp edge.
const config = defineConfig({
  theme: {
    // Named typography roles, so the recurring text combos are written once here and used
    // as `textStyle="…"` instead of repeating fontSize/fontWeight/color at each call site.
    // (Chakra ships a `label` role at sm/medium — these are app-specific compact variants.)
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
      // Tab triggers (e.g. the Settings left nav) are clamped to the same 32px as the app's
      // xs buttons/inputs, so they read as one control family rather than a taller variant.
      // Done here — not per Tabs.Trigger — so every tab list stays in step automatically.
      tabs: {
        slots: ["root", "list", "trigger", "content", "indicator"],
        base: {
          trigger: { minH: "8", maxH: "8", py: "1", fontSize: "sm", fontWeight: "medium" },
        },
      },
      dialog: {
        slots: ["backdrop", "positioner", "content", "title", "description", "header", "body", "footer", "closeTrigger"],
        base: {
          content: { borderRadius: "md" },
          header: { px: "4", pt: "4", pb: "3", gap: "2" },
          body: { px: "4", pt: "1", pb: "4" },
          footer: { px: "4", pt: "1", pb: "4", gap: "2" },
        },
      },
      // One dropdown row for the whole app. Menus (size sm — the DropdownMenu wrapper) and
      // Selects (size xs — SimpleSelect, model/agent pickers, session controls) are the same
      // UI to the user, but their stock recipes disagree: menu-sm rows are tighter (6px side
      // padding, 4px icon gap, regular weight, no inter-row gap) while select-xs rows are
      // roomier (8px/8px, hand-medium at every call site, plus a 4px flex gap between rows).
      // Both size variants are overridden here to one scale — 28px medium-weight rows, 8px
      // side padding and icon gap, rhythm from item padding alone — so no call site needs
      // its own py/fontWeight/fontSize on items. The slot lists mirror each recipe's anatomy
      // order exactly: config arrays merge index-wise, so a partial or reordered list would
      // corrupt the recipe's slot mapping.
      menu: {
        slots: ["arrow", "arrowTip", "content", "contextTrigger", "indicator", "item", "itemGroup", "itemGroupLabel", "itemIndicator", "itemText", "positioner", "separator", "trigger", "triggerItem", "itemCommand"],
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
        slots: ["label", "positioner", "trigger", "indicator", "clearTrigger", "item", "itemText", "itemIndicator", "itemGroup", "itemGroupLabel", "list", "content", "root", "control", "valueText", "indicatorGroup"],
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
})

const system = createSystem(defaultConfig, config)

export function Provider(props: ColorModeProviderProps) {
  return (
    <ClientOnly fallback={null}>
      <ChakraProvider value={system}>
        <LocaleProvider>
          <ColorModeProvider {...props} />
        </LocaleProvider>
      </ChakraProvider>
    </ClientOnly>
  )
}
