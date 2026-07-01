"use client";

import { Box, Code, Heading, Link, Text } from "@chakra-ui/react";
import { memo, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { xcode, atomOneDark } from "react-syntax-highlighter/dist/esm/styles/hljs";
import type { Components } from "react-markdown";
import type { Element } from "hast";
import { useColorModeValue } from "./ui/color-mode";

interface MarkdownContentProps {
  content: string;
  // Base font size for body text; headings keep their own semantic sizes. Body
  // elements inherit this from the wrapper so callers can match their context
  // (e.g. "xs" inside compact tool-call fields).
  fontSize?: string;
}

const blockGap = "0.625rem";
const headingGap = "0.375rem";
const sectionGap = "1rem";

// A single-line `$$...$$` is parsed by remark-math as *inline* math, so it lands
// inside a paragraph without KaTeX's `katex-display` wrapper and never centers.
// Detect the case where a paragraph's only child is the KaTeX span and render it
// as centered display math instead.
function isDisplayMathParagraph(node: Element | undefined): boolean {
  if (!node || node.children.length !== 1) return false;
  const [child] = node.children;
  return (
    child.type === "element" &&
    Array.isArray(child.properties?.className) &&
    child.properties.className.includes("katex")
  );
}

export const MarkdownContent = memo(function MarkdownContent({ content, fontSize = "sm" }: MarkdownContentProps) {
  const syntaxTheme = useColorModeValue(xcode, atomOneDark);

  const markdownComponents = useMemo<Components>(() => ({
    img({ src, alt }) {
      if (!src || (typeof src === "string" && !src.trim())) return null;
      return <img src={typeof src === "string" ? src : undefined} alt={alt ?? ""} style={{ maxWidth: "100%" }} />;
    },
    p({ node, children }) {
      if (isDisplayMathParagraph(node)) {
        return <Box textAlign="center" fontSize="inherit">{children}</Box>;
      }
      return <Text fontSize="inherit" lineHeight="1.65">{children}</Text>;
    },
    h1({ children }) {
      return <Heading as="h1" fontSize="lg" fontWeight="bold" lineHeight="1.3">{children}</Heading>;
    },
    h2({ children }) {
      return <Heading as="h2" fontSize="md" fontWeight="bold" lineHeight="1.3">{children}</Heading>;
    },
    h3({ children }) {
      return <Heading as="h3" fontSize="sm" fontWeight="bold" lineHeight="1.4">{children}</Heading>;
    },
    h4({ children }) {
      return <Heading as="h4" fontSize="sm" fontWeight="semibold" color="fg.muted">{children}</Heading>;
    },
    a({ href, children }) {
      return (
        <Link href={href} colorPalette="blue" fontSize="inherit" target="_blank" rel="noopener noreferrer">
          {children}
        </Link>
      );
    },
    ul({ children }) {
      return <Box as="ul" pl={5} fontSize="inherit" listStyleType="disc" lineHeight="1.5">{children}</Box>;
    },
    ol({ children }) {
      return <Box as="ol" pl={5} fontSize="inherit" listStyleType="decimal" lineHeight="1.5">{children}</Box>;
    },
    li({ children }) {
      return <Box as="li" mb={0.5} fontSize="inherit" display="list-item" _last={{ mb: 0 }}>{children}</Box>;
    },
    blockquote({ children }) {
      return (
        <Box borderLeft="2px solid" borderColor="border" pl={2} color="fg.muted" fontSize="inherit">
          {children}
        </Box>
      );
    },
    code({ className, children }) {
      const languageMatch = /language-(\w+)/.exec(className || "");
      const codeString = String(children).replace(/\n$/, "");
      const isBlock = !!languageMatch || codeString.includes("\n");

      if (isBlock) {
        return (
          <Box
            borderRadius="sm"
            overflow="auto"
            border="1px solid"
            borderColor="border"
            fontSize="xs"
            my={2}
            maxW="100%"
            maxH="420px"
            bg="bg.subtle"
          >
            <SyntaxHighlighter
              style={syntaxTheme}
              language={languageMatch ? languageMatch[1] : "text"}
              PreTag="div"
              customStyle={{
                margin: 0,
                borderRadius: "var(--chakra-radii-none)",
                fontFamily: "var(--app-font-mono)",
                fontSize: "inherit",
                background: "var(--chakra-colors-bg-subtle)",
                minWidth: "max-content",
              }}
              codeTagProps={{ style: { fontFamily: "inherit", fontSize: "inherit", whiteSpace: "pre" } }}
            >
              {codeString}
            </SyntaxHighlighter>
          </Box>
        );
      }

      return (
        <Code fontFamily="var(--app-font-mono)" lineHeight="inherit" px={1} bg="bg.subtle">
          {children}
        </Code>
      );
    },
    pre({ children }) {
      return <>{children}</>;
    },
    table({ children }) {
      return (
        <Box borderRadius="md" border="1px solid" borderColor="border" overflow="hidden">
          <Box overflowX="auto">
            <Box as="table" w="100%" fontSize="inherit" borderCollapse="collapse">
              {children}
            </Box>
          </Box>
        </Box>
      );
    },
    tr({ children }) {
      return <Box as="tr" _notLast={{ borderBottom: "1px solid", borderColor: "border" }}>{children}</Box>;
    },
    th({ children }) {
      return (
        <Box as="th" textAlign="left" px={2.5} py={1.5} fontWeight="semibold" bg="bg.emphasized" color="fg" whiteSpace="nowrap">
          {children}
        </Box>
      );
    },
    td({ children }) {
      return (
        <Box as="td" px={2.5} py={1.5} verticalAlign="top">
          {children}
        </Box>
      );
    },
    hr() {
      return <Box as="hr" border="none" borderTop="1px solid" borderColor="border" opacity={0.6} />;
    },
    strong({ children }) {
      return <Text as="strong" fontSize="inherit" lineHeight="inherit" fontWeight="bold">{children}</Text>;
    },
    em({ children }) {
      return <Text as="em" fontSize="inherit" lineHeight="inherit" fontStyle="italic">{children}</Text>;
    },
  }), [syntaxTheme]);

  return (
    <Box
      fontSize={fontSize}
      css={{
        "& > *": {
          marginBlock: 0,
        },
        "& > * + *": {
          marginBlockStart: blockGap,
        },
        "& > * + :is(h1, h2, h3, h4)": {
          marginBlockStart: sectionGap,
        },
        "& > :is(h1, h2, h3, h4) + *": {
          marginBlockStart: headingGap,
        },
        "& li > p": {
          marginBlock: 0,
        },
        "& li > p + p, & li > ul, & li > ol": {
          marginBlockStart: headingGap,
        },
        // Inline code is sized relative to its surrounding text (monospace runs
        // visually larger than proportional text at the same px, so 0.9em keeps
        // it from looking oversized in prose, headings, and tables alike).
        "& :not(pre) > code": {
          fontSize: "0.9em",
          lineHeight: "inherit",
        },
        "& strong, & em, & a": {
          fontSize: "inherit",
          lineHeight: "inherit",
        },
        "& code, & pre, & kbd, & samp": {
          fontFamily: "var(--app-font-mono)",
        },
      }}
    >
      {/* Inline math uses single `$…$` (the prompt instructs the model to emit it
          that way), display math uses `$$…$$`. remark-math's single-dollar heuristic
          only matches a `$` immediately followed by non-space text and a matching
          closing `$`, so bare currency ("$5", "~$9–16", "€50") does not misfire.
          `strict: false` keeps KaTeX from spamming warnings on the rest. */}
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
        rehypePlugins={[[rehypeKatex, { strict: false }]]}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </Box>
  );
});
