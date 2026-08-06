import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import { Provider } from "@/components/ui/provider";
import { Toaster } from "@/components/ui/toaster";
import { DesktopChrome } from "@/components/desktop-chrome";
import "./globals.css";

const sansFont = localFont({
  src: [
    { path: "../../public/fonts/sans/light.otf", weight: "300", style: "normal" },
    { path: "../../public/fonts/sans/regular.otf", weight: "400", style: "normal" },
    { path: "../../public/fonts/sans/regular-italic.otf", weight: "400", style: "italic" },
    { path: "../../public/fonts/sans/medium.otf", weight: "500", style: "normal" },
    { path: "../../public/fonts/sans/medium-italic.otf", weight: "500", style: "italic" },
    { path: "../../public/fonts/sans/semibold.otf", weight: "600", style: "normal" },
    { path: "../../public/fonts/sans/bold.otf", weight: "700", style: "normal" },
    { path: "../../public/fonts/sans/bold-italic.otf", weight: "700", style: "italic" },
    { path: "../../public/fonts/sans/extrabold.otf", weight: "800", style: "normal" },
    { path: "../../public/fonts/sans/extrabold-italic.otf", weight: "800", style: "italic" },
  ],
  variable: "--font-sans",
  display: "swap",
});

const displayFont = localFont({
  src: [
    { path: "../../public/fonts/display/light.otf", weight: "300", style: "normal" },
    { path: "../../public/fonts/display/regular.otf", weight: "400", style: "normal" },
    { path: "../../public/fonts/display/regular-italic.otf", weight: "400", style: "italic" },
    { path: "../../public/fonts/display/medium.otf", weight: "500", style: "normal" },
    { path: "../../public/fonts/display/medium-italic.otf", weight: "500", style: "italic" },
    { path: "../../public/fonts/display/semibold.otf", weight: "600", style: "normal" },
    { path: "../../public/fonts/display/bold.otf", weight: "700", style: "normal" },
    { path: "../../public/fonts/display/bold-italic.otf", weight: "700", style: "italic" },
    { path: "../../public/fonts/display/extrabold.otf", weight: "800", style: "normal" },
    { path: "../../public/fonts/display/extrabold-italic.otf", weight: "800", style: "italic" },
  ],
  variable: "--font-display",
  display: "swap",
});

const monoFont = localFont({
  src: [
    { path: "../../public/fonts/mono/regular.otf", weight: "400", style: "normal" },
    { path: "../../public/fonts/mono/regular-italic.otf", weight: "400", style: "italic" },
  ],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Frank",
  description: "Frank GUI",
  // The favicon comes from the file conventions app/favicon.ico + app/icon.png —
  // the exact app icon (src-tauri/icons) so the browser tab matches the app icon.
};

/**
 * The viewport, which matters on exactly one surface and is inert on the others.
 *
 * `viewport-fit=cover` is what makes `env(safe-area-inset-*)` report anything but zero: without
 * it the page is laid out inside the safe area and told the insets are nothing, which reads as
 * "this device takes no space" rather than "this space is already reserved for you". The layout
 * then reserves it a second time, or — inside the phone app's webview, where the page really is
 * full-bleed — not at all.
 *
 * `userScalable: false` because this is an application rather than a document: a double-tap that
 * zooms the transcript to 200% is a gesture nobody meant, and the text is already sized to be
 * read. Pinch-zoom on a code block would be worth having and is not what this disables.
 */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  userScalable: false,
  // The bar behind the status text follows the interface rather than staying white in the dark.
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#000000" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${sansFont.className} ${sansFont.variable} ${displayFont.variable} ${monoFont.variable}`} suppressHydrationWarning>
        <Provider>
          <DesktopChrome />
          {children}
          <Toaster />
        </Provider>
      </body>
    </html>
  );
}
