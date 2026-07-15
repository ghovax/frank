import { Span, Spinner, type SpinnerProps } from "@chakra-ui/react";
import type { ReactNode } from "react";

const ACTIVITY_ICON_BOX_SIZE = "3.5";

export function ActivityIcon({ children }: { children: ReactNode }) {
  return (
    <Span
      boxSize={ACTIVITY_ICON_BOX_SIZE}
      display="inline-flex"
      alignItems="center"
      justifyContent="center"
      flexShrink={0}
      css={{
        "& > *": { width: "100%", height: "100%" },
        "& svg": { width: "100%", height: "100%" },
      }}
    >
      {children}
    </Span>
  );
}

export function ActivitySpinner(properties: SpinnerProps) {
  return <Spinner boxSize={ACTIVITY_ICON_BOX_SIZE} borderWidth="1.5px" {...properties} />;
}
