"use client";

import { Badge } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { ActivityIcon } from "./activity-icon";

// The one small labeled badge/chip across the app: size="sm", variant="subtle", borderRadius="sm", non-shrinking.
export function Pill({
  children,
  colorPalette = "gray",
  icon,
  title,
}: {
  children?: ReactNode;
  colorPalette?: string;
  icon?: ReactNode;
  title?: string;
}) {
  return (
    <Badge
      size="sm"
      variant="subtle"
      colorPalette={colorPalette}
      borderRadius="sm"
      flexShrink={0}
      fontWeight="normal"
      title={title}
      display="inline-flex"
      alignItems="center"
      gap={1}
    >
      {icon ? <ActivityIcon>{icon}</ActivityIcon> : null}
      {children}
    </Badge>
  );
}
