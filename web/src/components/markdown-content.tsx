"use client";

import { Box, Code, Heading, Image, Link, List, Separator, Span, Table, Text } from "@chakra-ui/react";
import { Children, memo, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type SyntheticEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import twemoji from "@twemoji/api";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { xcode, atomOneDark } from "react-syntax-highlighter/dist/esm/styles/hljs";
import type { Components } from "react-markdown";
import type { Element } from "hast";
import { useReducedMotion } from "motion/react";
// flowtoken only publicly exports AnimatedMarkdown, whose fixed pipeline (remark-gfm +
// rehype-raw, its own element map) can't host our KaTeX/GFM/twemoji/Prism/Chakra setup. Its
// real engine is the SplitText token animator: in `diff` mode it tracks a growing string and
// wraps only the newly-arrived text in animated spans. We use that primitive directly and drive
// it from our own react-markdown so the streaming animation layers onto the existing renderer.
import SplitText from "flowtoken/dist/components/SplitText";
import { useColorModeValue } from "./ui/color-mode";
import { MermaidDiagram } from "./mermaid-diagram";
import { DeletedText, Emphasis, Strong } from "./ui/semantic";

interface MarkdownContentProps {
  content: string;
  // Base font size for body text; headings keep their own semantic sizes. Body
  // elements inherit this from the wrapper so callers can match their context
  // (e.g. "xs" inside compact tool-call fields).
  fontSize?: string;
  // Animate newly-streamed text as it arrives. Set only for the one live, streaming
  // message; finalized messages and history render statically (see renderChildren).
  animate?: boolean;
}

// Vertical rhythm of the prose. Kept compact — the transcript is a working
// document, not an article — while sectionGap still gives headings room to
// mark a boundary.
const blockGap = "0.625rem";
const headingGap = "0.375rem";
const sectionGap = "0.875rem";
const variationSelectorPattern = /\uFE0F/g;
const zeroWidthJoiner = String.fromCharCode(0x200d);
const emojiMarkerPattern = /\uE000(\d+)\uE001/g;

const twemojiStyle: CSSProperties = {
  display: "inline-block",
  width: "1em",
  height: "1em",
  margin: "0 0.05em",
  verticalAlign: "-0.125em",
};

function twemojiCodePoint(rawEmoji: string): string {
  const normalizedEmoji = rawEmoji.includes(zeroWidthJoiner) ? rawEmoji : rawEmoji.replace(variationSelectorPattern, "");
  return twemoji.convert.toCodePoint(normalizedEmoji);
}

function twemojiSource(rawEmoji: string): string {
  return `${twemoji.base}svg/${twemojiCodePoint(rawEmoji)}.svg`;
}

function handleTwemojiError(event: SyntheticEvent<HTMLImageElement>) {
  twemoji.onerror.call(event.currentTarget);
}

function renderTextWithTwemoji(text: string): ReactNode {
  if (!twemoji.test(text)) return text;

  const emojis: string[] = [];
  const markedText = twemoji.replace(text, (rawEmoji) => {
    if (rawEmoji === "\uFE0F") return rawEmoji;
    const marker = `\uE000${emojis.length}\uE001`;
    emojis.push(rawEmoji);
    return marker;
  });

  if (emojis.length === 0) return markedText;

  const nodes: ReactNode[] = [];
  let previousIndex = 0;
  for (const match of markedText.matchAll(emojiMarkerPattern)) {
    const matchIndex = match.index ?? 0;
    if (matchIndex > previousIndex) {
      nodes.push(markedText.slice(previousIndex, matchIndex));
    }
    const emojiIndex = Number(match[1]);
    const rawEmoji = emojis[emojiIndex];
    nodes.push(
      <Image
        key={`emoji-${matchIndex}-${emojiIndex}`}
        className="twemoji"
        draggable={false}
        alt={rawEmoji}
        src={twemojiSource(rawEmoji)}
        decoding="async"
        loading="lazy"
        style={twemojiStyle}
        onError={handleTwemojiError}
      />
    );
    previousIndex = matchIndex + match[0].length;
  }
  if (previousIndex < markedText.length) {
    nodes.push(markedText.slice(previousIndex));
  }
  return nodes;
}

function renderEmojiChildren(children: ReactNode): ReactNode {
  return Children.map(children, (child) => typeof child === "string" ? renderTextWithTwemoji(child) : child);
}

// The streaming token animation is paint-only: layout and text shaping stay identical
// before and after the turn completes.
const TOKEN_ANIMATION = "token-fade-in";
const TOKEN_DURATION = "0.16s";
const TOKEN_TIMING = "cubic-bezier(0.2, 0.8, 0.2, 1)";

function AnimatedText({ text, animate }: { text: string; animate: boolean }): ReactNode {
  return Children.toArray(renderTextWithTwemoji(text)).map((segment, segmentIndex) => {
    if (typeof segment !== "string") return segment;
    return (
      <Span className="flowtoken-text-leaf" key={`text-${segmentIndex}`}>
        <SplitText
          input={segment}
          sep="diff"
          animation={TOKEN_ANIMATION}
          animationDuration={animate ? TOKEN_DURATION : "0s"}
          animationTimingFunction={TOKEN_TIMING}
          animationIterationCount={1}
        />
      </Span>
    );
  });
}

// Every text leaf keeps the same Flowtoken and Twemoji structure in both states. Only the
// animation duration changes, so completing a turn cannot change wrapping or image metrics.
function renderChildren(children: ReactNode, animate: boolean): ReactNode {
  return Children.map(children, (child) => typeof child === "string" ? <AnimatedText text={child} animate={animate} /> : child);
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

// Coalesce a rapidly-growing string (a streaming message) down to ~15 renders/sec.
// Re-parsing the whole Markdown document — remark + rehype + KaTeX + Prism — on every
// animation frame is O(document) per frame, so a long streaming answer degrades to
// O(n²) and drops frames. Throttling is loss-free: the value only ever lags the input
// by the interval, and a trailing timer guarantees the final content always renders.
// A value that never changes (a finalized message) passes straight through — the
// first render already holds it and the effect schedules nothing new.
const STREAMING_RENDER_INTERVAL_MS = 66;

function useThrottledText(text: string, intervalMs: number, enabled: boolean): string {
  const [throttled, setThrottled] = useState(text);
  const lastRenderedAt = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!enabled) {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      return;
    }
    if (text === throttled) return;
    const elapsed = performance.now() - lastRenderedAt.current;
    const commit = () => {
      lastRenderedAt.current = performance.now();
      setThrottled(text);
    };
    if (elapsed >= intervalMs) {
      commit();
      return;
    }
    timerRef.current = setTimeout(commit, intervalMs - elapsed);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [text, throttled, intervalMs, enabled]);

  return enabled ? throttled : text;
}

export const MarkdownContent = memo(function MarkdownContent({ content, fontSize = "sm", animate = false }: MarkdownContentProps) {
  const syntaxTheme = useColorModeValue(xcode, atomOneDark);
  // Reduced-motion readers keep the stable token structure but receive a zero-duration fade.
  const reduceMotion = useReducedMotion();
  const animating = animate && !reduceMotion;
  const renderedContent = useThrottledText(content, STREAMING_RENDER_INTERVAL_MS, animating);

  const markdownComponents = useMemo<Components>(() => ({
    img({ src, alt }) {
      if (!src || (typeof src === "string" && !src.trim())) return null;
      return <Image src={typeof src === "string" ? src : undefined} alt={alt ?? ""} maxW="100%" />;
    },
    p({ node, children }) {
      if (isDisplayMathParagraph(node)) {
        return <Box textAlign="center" fontSize="inherit">{children}</Box>;
      }
      return <Text fontSize="inherit" lineHeight="1.55">{renderChildren(children, animating)}</Text>;
    },
    h1({ children }) {
      return <Heading as="h1" fontSize="lg" fontWeight="bold">{renderChildren(children, animating)}</Heading>;
    },
    h2({ children }) {
      return <Heading as="h2" fontSize="md" fontWeight="bold">{renderChildren(children, animating)}</Heading>;
    },
    h3({ children }) {
      return <Heading as="h3" fontSize="sm" fontWeight="bold">{renderChildren(children, animating)}</Heading>;
    },
    h4({ children }) {
      return <Heading as="h4" fontSize="sm" fontWeight="semibold" color="fg.muted">{renderChildren(children, animating)}</Heading>;
    },
    a({ href, children }) {
      // Editorial underline like the reference prose: offset from the baseline and
      // faint until hover/focus, rather than a heavy solid rule.
      return (
        <Link
          href={href}
          colorPalette="blue"
          fontSize="inherit"
          textUnderlineOffset="2px"
          textDecorationThickness="1px"
          target="_blank"
          rel="noopener noreferrer"
        >
          {renderChildren(children, animating)}
        </Link>
      );
    },
    ul({ children }) {
      return <List.Root pl={6} fontSize="inherit" listStyleType="disc">{children}</List.Root>;
    },
    ol({ children }) {
      return <List.Root as="ol" pl={6} fontSize="inherit" listStyleType="decimal">{children}</List.Root>;
    },
    li({ children }) {
      return <List.Item mb={0.5} fontSize="inherit" lineHeight="1.55" _last={{ mb: 0 }}>{renderChildren(children, animating)}</List.Item>;
    },
    blockquote({ children }) {
      // 2px rule + pl=3: the exact grammar of an expanded tool call's detail rail
      // (tool-call.tsx), so quoted prose and quoted activity read as one language.
      return (
        <Box borderLeftWidth="2px" borderColor="border.muted" pl={3} py={0.5} color="fg.muted" fontSize="inherit">
          {renderChildren(children, animating)}
        </Box>
      );
    },
    code({ className, children }) {
      const languageMatch = /language-(\w+)/.exec(className || "");
      const codeString = String(children).replace(/\n$/, "");
      const isBlock = !!languageMatch || codeString.includes("\n");

      if (isBlock) {
        const block = (
          <Box
            borderRadius="md"
            overflow="auto"
            borderWidth="1px"
            borderColor="border.muted"
            fontSize="xs"
            my={1.5}
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
                padding: "0.5rem 0.625rem",
                lineHeight: 1.5,
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
        // A ```mermaid fence renders as an inline diagram once its source parses;
        // the plain code block serves as the fallback until then (and forever, if
        // the source never parses).
        if (languageMatch?.[1] === "mermaid") {
          return <MermaidDiagram code={codeString} fallback={block} />;
        }
        return block;
      }

      // A subtle chip — faint fill, hairline border, rounded — mirroring the reference
      // prose. Vertical padding stays at 0 so inline code doesn't disturb line rhythm.
      return (
        <Code
          fontFamily="var(--app-font-mono)"
          px={1.5}
          py={0}
          borderRadius="sm"
          borderWidth="1px"
          borderColor="border.muted"
          bg="bg.subtle"
          color="fg"
        >
          {children}
        </Code>
      );
    },
    pre({ children }) {
      return <>{children}</>;
    },
    // Borderless table, mirroring the reference prose: no outer card or vertical rules —
    // just a stronger rule under the header row and a fainter one under each body row,
    // left-aligned with generous right padding. Scrolls horizontally when it overflows.
    table({ children }) {
      return (
        <Box overflowX="auto" maxW="100%" my={2}>
          <Table.Root minW="100%" fontSize="inherit" lineHeight="1.55" borderCollapse="collapse">
            {renderChildren(children, animating)}
          </Table.Root>
        </Box>
      );
    },
    tr({ children }) {
      return <Table.Row>{renderChildren(children, animating)}</Table.Row>;
    },
    th({ children }) {
      return (
        <Table.ColumnHeader textAlign="left" pr={3} pl={0} py={1.5} fontWeight="semibold" color="fg" borderBottom="1px solid" borderColor="border" verticalAlign="top" overflowWrap="break-word">
          {renderChildren(children, animating)}
        </Table.ColumnHeader>
      );
    },
    td({ children }) {
      return (
        <Table.Cell pr={3} pl={0} py={1.5} borderBottom="1px solid" borderColor="border.muted" verticalAlign="top" overflowWrap="break-word">
          {renderChildren(children, animating)}
        </Table.Cell>
      );
    },
    hr() {
      return <Separator borderColor="border.muted" my={1} />;
    },
    strong({ children }) {
      return <Strong fontSize="inherit" fontWeight="bold">{renderChildren(children, animating)}</Strong>;
    },
    em({ children }) {
      return <Emphasis fontSize="inherit" fontStyle="italic">{renderChildren(children, animating)}</Emphasis>;
    },
    del({ children }) {
      return <DeletedText fontSize="inherit">{renderChildren(children, animating)}</DeletedText>;
    },
  }), [syntaxTheme, animating]);

  return (
    <Box
      minW={0}
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
          fontSize: "0.9125em",
        },
        "& strong, & em, & a": {
          fontSize: "inherit",
        },
        "& code, & pre, & kbd, & samp": {
          fontFamily: "var(--app-font-mono)",
        },
        "& .twemoji": {
          display: "inline-block",
          width: "1em",
          height: "1em",
          margin: "0 0.05em",
          verticalAlign: "-0.125em",
        },
        "& .flowtoken-text-leaf > span": {
          display: "inline !important",
          whiteSpace: "inherit !important",
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
        {renderedContent}
      </ReactMarkdown>
    </Box>
  );
});

// A stripped-down, single-line Markdown renderer for places where a label must
// render inline and stay on one line: tool-call headings (the justification).
// Every block element collapses to its inline children so nothing forces a line
// break or block margin — only code spans, emphasis, links, and inline math keep
// their formatting. Pairs with a parent that owns the truncation/ellipsis.
const passthrough = ({ children }: { children?: ReactNode }) => <>{renderEmojiChildren(children)}</>;

const inlineMarkdownComponents: Components = {
  p: passthrough,
  h1: passthrough, h2: passthrough, h3: passthrough, h4: passthrough, h5: passthrough, h6: passthrough,
  ul: passthrough, ol: passthrough, li: passthrough, pre: passthrough, blockquote: passthrough,
  hr: () => null,
  img: () => null,
  br: () => <> </>,
  a({ href, children }) {
    return (
      <Link href={typeof href === "string" ? href : undefined} colorPalette="blue" fontSize="inherit" target="_blank" rel="noopener noreferrer">
        {renderEmojiChildren(children)}
      </Link>
    );
  },
  code({ children }) {
    return (
      <Code
        fontFamily="var(--app-font-mono)"
        fontSize="0.9em"
        px={1.5}
        py={0}
        borderRadius="sm"
        borderWidth="1px"
        borderColor="border.muted"
        bg="bg.subtle"
        color="fg"
        whiteSpace="nowrap"
      >
        {children}
      </Code>
    );
  },
  strong({ children }) {
    return <Strong fontSize="inherit" fontWeight="bold">{renderEmojiChildren(children)}</Strong>;
  },
  em({ children }) {
    return <Emphasis fontSize="inherit" fontStyle="italic">{renderEmojiChildren(children)}</Emphasis>;
  },
  del({ children }) {
    return <DeletedText fontSize="inherit">{renderEmojiChildren(children)}</DeletedText>;
  },
};

export const InlineMarkdown = memo(function InlineMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
      rehypePlugins={[[rehypeKatex, { strict: false }]]}
      components={inlineMarkdownComponents}
    >
      {content}
    </ReactMarkdown>
  );
});
