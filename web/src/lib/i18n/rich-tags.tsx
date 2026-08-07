/** The tags a translated string may carry, rendered the same way wherever one is read. */

import { Span } from "@chakra-ui/react";
import type { ReactNode } from "react";

import { Strong } from "@/components/ui/semantic";

/** Pass to `translation.rich(key, richTags)`, so a message's markup renders rather than showing as punctuation. */
export const richTags = {
  code: (chunks: ReactNode) => <Span fontFamily="var(--app-font-mono)">{chunks}</Span>,
  b: (chunks: ReactNode) => <Strong>{chunks}</Strong>,
};
