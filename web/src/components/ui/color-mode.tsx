"use client"

import type { IconButtonProps, SpanProps } from "@chakra-ui/react"
import { ClientOnly, IconButton, Skeleton, Span } from "@chakra-ui/react"
import * as React from "react"
import { LuMoon, LuSun } from "react-icons/lu"
import { getAppState, isTauri, setAppState } from "@/lib/connection-store"

export type ColorMode = "light" | "dark"

export interface ColorModeProviderProps extends React.PropsWithChildren {
  defaultTheme?: ColorMode | "system"
  forcedTheme?: ColorMode
  storageKey?: string
}

export interface UseColorModeReturn {
  // The user's chosen preference, including "system" (follow the OS). `colorMode` is the
  // resolved light/dark this yields right now.
  theme: ColorMode | "system"
  colorMode: ColorMode
  setTheme: (theme: ColorMode | "system") => void
  setColorMode: (colorMode: ColorMode) => void
  toggleColorMode: () => void
}

const ColorModeContext = React.createContext<UseColorModeReturn | null>(null)
const colorModeStorageKey = "theme"

function isStoredTheme(value: string | null): value is ColorMode | "system" {
  return value === "light" || value === "dark" || value === "system"
}

export function ColorModeProvider({
  children,
  defaultTheme = "system",
  forcedTheme,
  storageKey = colorModeStorageKey,
}: ColorModeProviderProps) {
  const [theme, setThemeState] = React.useState<ColorMode | "system">(() => {
    if (typeof window === "undefined") return forcedTheme ?? defaultTheme
    if (isTauri()) return forcedTheme ?? defaultTheme

    try {
      const storedTheme = localStorage.getItem(storageKey)
      if (!forcedTheme && isStoredTheme(storedTheme)) return storedTheme
    } catch {
      // localStorage can be unavailable in restricted browser contexts.
    }

    return forcedTheme ?? defaultTheme
  })
  const [systemColorMode, setSystemColorMode] = React.useState<ColorMode>("light")

  React.useEffect(() => {
    if (forcedTheme || !isTauri()) return
    let cancelled = false
    getAppState(storageKey)
      .then((storedTheme) => {
        if (!cancelled && isStoredTheme(storedTheme)) setThemeState(storedTheme)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [forcedTheme, storageKey])

  React.useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: dark)")
    const syncSystemColorMode = () => setSystemColorMode(query.matches ? "dark" : "light")
    syncSystemColorMode()

    query.addEventListener("change", syncSystemColorMode)
    return () => query.removeEventListener("change", syncSystemColorMode)
  }, [])

  const colorMode = forcedTheme ?? (theme === "system" ? systemColorMode : theme)

  React.useEffect(() => {
    document.documentElement.classList.remove("light", "dark")
    document.documentElement.classList.add(colorMode)
    document.documentElement.style.colorScheme = colorMode
  }, [colorMode])

  // The general setter: persists any preference, "system" included. `setColorMode` is the
  // narrower light/dark form kept for the toggle button and existing callers.
  const setTheme = React.useCallback(
    (nextTheme: ColorMode | "system") => {
      if (forcedTheme) return

      setThemeState(nextTheme)
      if (isTauri()) {
        void setAppState(storageKey, nextTheme)
        return
      }
      try {
        localStorage.setItem(storageKey, nextTheme)
      } catch {
        // localStorage can be unavailable in restricted browser contexts.
      }
    },
    [forcedTheme, storageKey],
  )

  const setColorMode = React.useCallback(
    (nextColorMode: ColorMode) => setTheme(nextColorMode),
    [setTheme],
  )

  const toggleColorMode = React.useCallback(() => {
    setColorMode(colorMode === "dark" ? "light" : "dark")
  }, [colorMode, setColorMode])

  const value = React.useMemo(
    () => ({ theme, colorMode, setTheme, setColorMode, toggleColorMode }),
    [theme, colorMode, setTheme, setColorMode, toggleColorMode],
  )

  return (
    <ColorModeContext.Provider value={value}>
      {children}
    </ColorModeContext.Provider>
  )
}

export function useColorMode(): UseColorModeReturn {
  const context = React.useContext(ColorModeContext)
  if (!context) {
    return {
      theme: "system",
      colorMode: "light",
      setTheme: () => {},
      setColorMode: () => {},
      toggleColorMode: () => {},
    }
  }
  return context
}

export function useColorModeValue<T>(light: T, dark: T) {
  const { colorMode } = useColorMode()
  return colorMode === "dark" ? dark : light
}

export function ColorModeIcon() {
  const { colorMode } = useColorMode()
  return colorMode === "dark" ? <LuMoon /> : <LuSun />
}

type ColorModeButtonProps = Omit<IconButtonProps, "aria-label">

export const ColorModeButton = React.forwardRef<
  HTMLButtonElement,
  ColorModeButtonProps
>(function ColorModeButton(props, ref) {
  const { toggleColorMode } = useColorMode()
  return (
    <ClientOnly fallback={<Skeleton boxSize="9" />}>
      <IconButton
        onClick={toggleColorMode}
        variant="ghost"
        aria-label="Toggle color mode"
        size="sm"
        ref={ref}
        {...props}
        css={{
          _icon: {
            width: "5",
            height: "5",
          },
        }}
      >
        <ColorModeIcon />
      </IconButton>
    </ClientOnly>
  )
})

export const LightMode = React.forwardRef<HTMLSpanElement, SpanProps>(
  function LightMode(props, ref) {
    return (
      <Span
        color="fg"
        display="contents"
        className="chakra-theme light"
        colorPalette="gray"
        colorScheme="light"
        ref={ref}
        {...props}
      />
    )
  },
)

export const DarkMode = React.forwardRef<HTMLSpanElement, SpanProps>(
  function DarkMode(props, ref) {
    return (
      <Span
        color="fg"
        display="contents"
        className="chakra-theme dark"
        colorPalette="gray"
        colorScheme="dark"
        ref={ref}
        {...props}
      />
    )
  },
)
