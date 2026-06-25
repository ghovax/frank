"use client";

import { Box, Code, Heading, Link, Text } from "@chakra-ui/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import type { Components } from "react-markdown";

interface MarkdownContentProps {
  content: string;
}

const markdownComponents: Components = {
  p({ children }) {
    return <Text mb={0} fontSize="sm" lineHeight="base" _notLast={{ mb: 1 }}>{children}</Text>;
  },
  h1({ children }) {
    return <Heading as="h1" size="md" mb={1.5} mt={2} borderBottom="1px solid" borderColor="border" pb={1}>{children}</Heading>;
  },
  h2({ children }) {
    return <Heading as="h2" size="sm" mb={1} mt={2} borderBottom="1px solid" borderColor="border" pb={0.5}>{children}</Heading>;
  },
  h3({ children }) {
    return <Heading as="h3" fontSize="sm" fontWeight="bold" mb={1} mt={1.5}>{children}</Heading>;
  },
  a({ href, children }) {
    return (
      <Link href={href} colorPalette="blue" fontSize="sm" target="_blank" rel="noopener noreferrer">
        {children}
      </Link>
    );
  },
  ul({ children }) {
    return <Box as="ul" pl={5} mb={1} fontSize="sm" listStyleType="disc">{children}</Box>;
  },
  ol({ children }) {
    return <Box as="ol" pl={5} mb={1} fontSize="sm" listStyleType="decimal">{children}</Box>;
  },
  li({ children }) {
    return <Box as="li" mb={0.5} fontSize="sm" display="list-item">{children}</Box>;
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
            customStyle={{ margin: 0, borderRadius: "var(--chakra-radii-sm)", fontSize: "0.8em" }}
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
      <Box overflowX="auto" my={1} borderRadius="sm" border="1px solid" borderColor="border">
        <Box as="table" w="100%" fontSize="xs" borderCollapse="collapse">
          {children}
        </Box>
      </Box>
    );
  },
  thead({ children }) {
    return <Box as="thead" bg="bg.emphasized">{children}</Box>;
  },
  tr({ children }) {
    return <Box as="tr" _even={{ bg: "bg.muted" }}>{children}</Box>;
  },
  th({ children }) {
    return (
      <Box as="th" textAlign="left" px={2} py={1} borderBottom="2px solid" borderColor="border" fontWeight="bold">
        {children}
      </Box>
    );
  },
  td({ children }) {
    return (
      <Box as="td" px={2} py={1} borderBottom="1px solid" borderColor="border">
        {children}
      </Box>
    );
  },
  hr() {
    return <Box as="hr" my={2} borderColor="border" />;
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
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
      {content}
    </ReactMarkdown>
  );
}
