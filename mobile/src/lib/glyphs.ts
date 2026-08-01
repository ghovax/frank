import {
  CircleAlert, CircleX, Download, FilePen, FilePlus, FileText, Globe, ListChecks,
  MessageCircleQuestion, Moon, MousePointerClick, Plug, SearchCode, Server, Sparkles, Target,
  Terminal, UserSearch, Wrench, type LucideIcon,
} from "lucide-react-native";

/**
 * The one table turning a shared glyph name into something this client can draw.
 *
 * Which glyph means `bash` is decided in `@shared/tools`, alongside the desktop. *What a glyph
 * is* cannot be: this is `lucide-react-native` and the desktop is `react-icons`, which are
 * different packages exporting different objects. So the decision is shared and the resolution
 * is not — the same seam, mirrored, in `web/src/lib/glyphs.ts`.
 */
export const GLYPHS: Record<string, LucideIcon> = {
  "globe": Globe,
  "terminal": Terminal,
  "file-text": FileText,
  "search-code": SearchCode,
  "mouse-pointer-click": MousePointerClick,
  "file-pen": FilePen,
  "file-plus": FilePlus,
  "download": Download,
  "message-circle-question": MessageCircleQuestion,
  "target": Target,
  "user-search": UserSearch,
  "sparkles": Sparkles,
  "plug": Plug,
  "list-checks": ListChecks,
  "server": Server,
  "wrench": Wrench,
  "circle-alert": CircleAlert,
  "circle-x": CircleX,
  "moon": Moon,
};

/** A glyph by name, falling back to the one that means "not a tool I know". */
export function glyph(name: string | undefined): LucideIcon {
  return (name && GLYPHS[name]) || Wrench;
}

/**
 * A shared tint token (`green.fg`, `fg.muted`) as a key into this client's palette.
 *
 * The desktop hands `green.fg` straight to Chakra, which resolves it. React Native has no such
 * resolver, so the token is translated here — one table, rather than a second set of colour
 * decisions.
 */
export function tintKey(token: string): string {
  const [family, role] = token.split(".");
  if (family === "fg") return role === "muted" ? "fgMuted" : role === "subtle" ? "fgSubtle" : "fg";
  if (!role) return family;
  return `${family}${role.charAt(0).toUpperCase()}${role.slice(1)}`;
}
