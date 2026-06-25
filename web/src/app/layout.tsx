import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import { Provider } from "@/components/ui/provider";
import "katex/dist/katex.min.css";
import "./globals.css";

const sansFont = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
  display: "swap",
});

const monoFont = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Harness",
  description: "Agentic Harness UI",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${sansFont.className} ${sansFont.variable} ${monoFont.variable}`} suppressHydrationWarning>
        <Provider>{children}</Provider>
      </body>
    </html>
  );
}
