import type { IconType } from "react-icons";
import {
  LuBadgeCheck, LuBox, LuCircleAlert, LuCircleSlash, LuCircleX, LuCopy, LuDownload, LuEye,
  LuFilePen, LuFilePlus, LuFileText, LuFolder, LuGitBranch, LuGlobe, LuHand, LuListChecks,
  LuMessageCircleQuestion, LuMic, LuMicOff, LuMoon, LuMousePointerClick, LuPlug, LuSearchCode,
  LuServer, LuSparkles, LuTarget, LuTerminal, LuUserRoundX, LuUserSearch, LuWrench, LuZap,
} from "react-icons/lu";

/**
 * The one table turning a shared glyph name into something this client can draw.
 *
 * Which glyph means `bash` is decided in `@shared/tools`, because the phone has to agree with it
 * and drifted when it did not. *What a glyph is* cannot be shared: this is `react-icons`, the
 * phone is `lucide-react-native`, and they are different packages exporting different objects.
 * So the decision is shared and the resolution is not — which is the whole of the seam between
 * the two clients, in two small tables.
 */
export const GLYPHS: Record<string, IconType> = {
  "globe": LuGlobe,
  "terminal": LuTerminal,
  "file-text": LuFileText,
  "search-code": LuSearchCode,
  "mouse-pointer-click": LuMousePointerClick,
  "file-pen": LuFilePen,
  "file-plus": LuFilePlus,
  "download": LuDownload,
  "message-circle-question": LuMessageCircleQuestion,
  "target": LuTarget,
  "user-search": LuUserSearch,
  "sparkles": LuSparkles,
  "plug": LuPlug,
  "list-checks": LuListChecks,
  "server": LuServer,
  "wrench": LuWrench,
  "circle-alert": LuCircleAlert,
  "circle-x": LuCircleX,
  "moon": LuMoon,
  "hand": LuHand,
  "badge-check": LuBadgeCheck,
  "eye": LuEye,
  "box": LuBox,
  "folder": LuFolder,
  "git-branch": LuGitBranch,
  "copy": LuCopy,
  "zap": LuZap,
  "circle-slash": LuCircleSlash,
  "user-round-x": LuUserRoundX,
  "mic": LuMic,
  "mic-off": LuMicOff,
};

/** A glyph by name, falling back to the one that means "not a tool I know". */
export function glyph(name: string | undefined): IconType {
  return (name && GLYPHS[name]) || LuWrench;
}
