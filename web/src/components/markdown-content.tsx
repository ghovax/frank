"use client";

import { Box, Code, Heading, Link, Text } from "@chakra-ui/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import type { Components } from "react-markdown";
import type { Element } from "hast";

interface MarkdownContentProps {
  content: string;
}

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

const markdownComponents: Components = {
  p({ node, children }) {
    if (isDisplayMathParagraph(node)) {
      return <Box py={2.5} textAlign="center" fontSize="sm">{children}</Box>;
    }
    return <Text fontSize="sm" lineHeight="1.65" mb={0} _notLast={{ mb: 2.5 }}>{children}</Text>;
  },
  h1({ children }) {
    return <Heading as="h1" fontSize="lg" fontWeight="bold" lineHeight="1.3" mt={5} mb={2} _first={{ mt: 0 }}>{children}</Heading>;
  },
  h2({ children }) {
    return <Heading as="h2" fontSize="md" fontWeight="bold" lineHeight="1.3" mt={4} mb={1.5} _first={{ mt: 0 }}>{children}</Heading>;
  },
  h3({ children }) {
    return <Heading as="h3" fontSize="sm" fontWeight="bold" lineHeight="1.4" mt={3} mb={1} _first={{ mt: 0 }}>{children}</Heading>;
  },
  h4({ children }) {
    return <Heading as="h4" fontSize="sm" fontWeight="semibold" color="fg.muted" mt={2.5} mb={1} _first={{ mt: 0 }}>{children}</Heading>;
  },
  a({ href, children }) {
    return (
      <Link href={href} colorPalette="blue" fontSize="sm" target="_blank" rel="noopener noreferrer">
        {children}
      </Link>
    );
  },
  ul({ children }) {
    return <Box as="ul" pl={5} mb={2.5} fontSize="sm" listStyleType="disc" lineHeight="1.5">{children}</Box>;
  },
  ol({ children }) {
    return <Box as="ol" pl={5} mb={2.5} fontSize="sm" listStyleType="decimal" lineHeight="1.5">{children}</Box>;
  },
  li({ children }) {
    return <Box as="li" mb={0.5} fontSize="sm" display="list-item" _last={{ mb: 0 }}>{children}</Box>;
  },
  blockquote({ children }) {
    return (
      <Box borderLeft="2px solid" borderColor="border" pl={2} my={1} color="fg.muted" fontSize="sm">
        {children}
      </Box>
    );
  },
  code({ className, children }) {
    const languageMatch = /language-(\w+)/.exec(className || "");
    const codeString = String(children).replace(/\n$/, "");

    if (languageMatch) {
      return (
        <Box my={1} borderRadius="sm" overflow="hidden" border="1px solid" borderColor="border">
          <SyntaxHighlighter
            style={oneDark}
            language={languageMatch[1]}
            PreTag="div"
            customStyle={{ margin: 0, borderRadius: "var(--chakra-radii-sm)" }}
          >
            {codeString}
          </SyntaxHighlighter>
        </Box>
      );
    }

    return <Code fontSize="xs" px={1} bg="bg.subtle">{children}</Code>;
  },
  table({ children }) {
    return (
      <Box my={2.5} borderRadius="md" border="1px solid" borderColor="border" overflow="hidden">
        <Box overflowX="auto">
          <Box as="table" w="100%" fontSize="xs" borderCollapse="collapse">
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
    return <Box as="hr" my={4} border="none" borderTop="1px solid" borderColor="border" opacity={0.6} />;
  },
  strong({ children }) {
    return <Text as="strong" fontWeight="bold">{children}</Text>;
  },
  em({ children }) {
    return <Text as="em" fontStyle="italic">{children}</Text>;
  },
};

export function MarkdownContent({ content }: MarkdownContentProps) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={markdownComponents}>
      {content}
    </ReactMarkdown>
  );
}
