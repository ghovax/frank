/**
 * Assistant prose.
 *
 * `useMarkdown` rather than the `<Markdown>` component, because that component renders into a
 * FlatList and this content already lives inside the transcript's scroll view — a list inside a
 * list scrolls neither well nor predictably. The hook hands back plain elements instead.
 *
 * Parsing is throttled while text is arriving. Markdown is re-parsed from the whole string on
 * every change, and a turn's prose changes once per token; at sixty of those a second the parser
 * is the frame budget. The desktop solves the same problem the same way and calls it
 * `STREAMING_RENDER_INTERVAL_MS`.
 */

import { Fragment, memo, useEffect, useRef, useState } from "react";
import { View } from "react-native";
import { useMarkdown } from "react-native-marked";

import { useTheme } from "../theme";

const RENDER_INTERVAL_MS = 120;

function useThrottled(value: string, active: boolean): string {
  const [settled, setSettled] = useState(value);
  const latest = useRef(value);

  // The newest text, parked where the interval can reach it. Written in an effect rather than
  // during render: a ref assigned by a render that React may discard is a value nobody can
  // reason about, and nothing here reads it while rendering.
  useEffect(() => { latest.current = value; }, [value]);

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setSettled(latest.current), RENDER_INTERVAL_MS);
    return () => {
      clearInterval(timer);
      // The last flush is not optional: without it the tail of a turn — often the sentence that
      // answers the question — would sit in the buffer until something else re-rendered.
      setSettled(latest.current);
    };
  }, [active]);

  // Throttled only while text is still arriving. Once it has stopped the value itself is the
  // answer, which is also why the settled copy needs no update of its own on the way out.
  return active ? settled : value;
}

export const Markdown = memo(function Markdown({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const theme = useTheme();
  const shown = useThrottled(text, streaming);

  const elements = useMarkdown(shown, {
    colorScheme: theme.scheme,
    theme: {
      colors: {
        text: theme.colors.fg,
        link: theme.colors.blueFg,
        code: theme.colors.bgSubtle,
        border: theme.colors.borderMuted,
      },
    },
    styles: {
      text: { ...theme.text.body, color: theme.colors.fg },
      paragraph: { paddingVertical: theme.space[1] },
      strong: { fontFamily: theme.fonts.sansSemibold },
      em: { fontStyle: "italic" },
      link: { color: theme.colors.blueFg },
      h1: { fontFamily: theme.fonts.display, fontSize: theme.fontSizes.xl, color: theme.colors.fg },
      h2: { fontFamily: theme.fonts.sansBold, fontSize: theme.fontSizes.lg, color: theme.colors.fg },
      h3: { fontFamily: theme.fonts.sansSemibold, fontSize: theme.fontSizes.md, color: theme.colors.fg },
      h4: { fontFamily: theme.fonts.sansSemibold, fontSize: theme.fontSizes.sm, color: theme.colors.fg },
      codespan: {
        fontFamily: theme.fonts.mono,
        fontSize: theme.fontSizes.xs,
        color: theme.colors.fg,
        backgroundColor: theme.colors.bgMuted,
      },
      code: {
        backgroundColor: theme.colors.bgSubtle,
        borderColor: theme.colors.borderMuted,
        borderWidth: 1,
        borderRadius: theme.radii.md,
        padding: theme.space[2.5],
        marginVertical: theme.space[1],
      },
      blockquote: {
        borderLeftColor: theme.colors.borderEmphasized,
        borderLeftWidth: 2,
        paddingLeft: theme.space[3],
      },
      li: { ...theme.text.body, color: theme.colors.fg },
      hr: { backgroundColor: theme.colors.borderMuted, height: 1, marginVertical: theme.space[2] },
      table: { borderColor: theme.colors.borderMuted },
      tableCell: { borderColor: theme.colors.borderMuted },
    },
  });

  return (
    <View>
      {elements.map((element, index) => (
        <Fragment key={index}>{element}</Fragment>
      ))}
    </View>
  );
});
