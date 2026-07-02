import type { IconType } from "react-icons";
import {
  LuFile,
  LuFileCode,
  LuFileImage,
  LuFileJson,
  LuFileTerminal,
  LuFileText,
  LuFileType,
  LuFileSpreadsheet,
} from "react-icons/lu";

export interface FileIconInfo {
  icon: IconType;
  iconColor: string;
}

const FILE_ICON_MAP: Record<string, FileIconInfo> = {
  // Code
  ".ts": { icon: LuFileCode, iconColor: "blue.fg" },
  ".tsx": { icon: LuFileCode, iconColor: "blue.fg" },
  ".js": { icon: LuFileCode, iconColor: "yellow.fg" },
  ".jsx": { icon: LuFileCode, iconColor: "yellow.fg" },
  ".mjs": { icon: LuFileCode, iconColor: "yellow.fg" },
  ".cjs": { icon: LuFileCode, iconColor: "yellow.fg" },
  ".py": { icon: LuFileCode, iconColor: "green.fg" },
  ".rs": { icon: LuFileCode, iconColor: "orange.fg" },
  ".go": { icon: LuFileCode, iconColor: "cyan.fg" },
  ".java": { icon: LuFileCode, iconColor: "red.fg" },
  ".kt": { icon: LuFileCode, iconColor: "purple.fg" },
  ".swift": { icon: LuFileCode, iconColor: "red.fg" },
  ".rb": { icon: LuFileCode, iconColor: "red.fg" },
  // Data / config
  ".json": { icon: LuFileJson, iconColor: "orange.fg" },
  ".jsonc": { icon: LuFileJson, iconColor: "orange.fg" },
  ".toml": { icon: LuFileJson, iconColor: "fg.muted" },
  ".yaml": { icon: LuFileJson, iconColor: "fg.muted" },
  ".yml": { icon: LuFileJson, iconColor: "fg.muted" },
  ".xml": { icon: LuFileCode, iconColor: "fg.muted" },
  ".plist": { icon: LuFileCode, iconColor: "fg.muted" },
  ".env": { icon: LuFile, iconColor: "fg.muted" },
  // Markup / styles
  ".html": { icon: LuFileType, iconColor: "red.fg" },
  ".htm": { icon: LuFileType, iconColor: "red.fg" },
  ".css": { icon: LuFileType, iconColor: "pink.fg" },
  ".scss": { icon: LuFileType, iconColor: "pink.fg" },
  ".less": { icon: LuFileType, iconColor: "pink.fg" },
  ".svg": { icon: LuFileImage, iconColor: "fg.muted" },
  // Docs
  ".md": { icon: LuFileText, iconColor: "fg.muted" },
  ".mdx": { icon: LuFileText, iconColor: "fg.muted" },
  ".txt": { icon: LuFileText, iconColor: "fg.muted" },
  // Images
  ".png": { icon: LuFileImage, iconColor: "fg.muted" },
  ".jpg": { icon: LuFileImage, iconColor: "fg.muted" },
  ".jpeg": { icon: LuFileImage, iconColor: "fg.muted" },
  ".gif": { icon: LuFileImage, iconColor: "fg.muted" },
  ".webp": { icon: LuFileImage, iconColor: "fg.muted" },
  ".ico": { icon: LuFileImage, iconColor: "fg.muted" },
  // Shell
  ".sh": { icon: LuFileTerminal, iconColor: "green.fg" },
  ".bash": { icon: LuFileTerminal, iconColor: "green.fg" },
  ".zsh": { icon: LuFileTerminal, iconColor: "green.fg" },
  // Data tables
  ".csv": { icon: LuFileSpreadsheet, iconColor: "green.fg" },
  ".tsv": { icon: LuFileSpreadsheet, iconColor: "green.fg" },
};

export function iconForFilePath(filePath: string): FileIconInfo {
  const match = filePath.match(/\.([^./]+)$/);
  if (!match) return { icon: LuFile, iconColor: "fg.muted" };
  const ext = `.${match[1]}`;
  return FILE_ICON_MAP[ext] ?? { icon: LuFile, iconColor: "fg.muted" };
}
