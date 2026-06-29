// Lightweight stand-in for react-syntax-highlighter, used ONLY in Storybook. The
// real library (Prism build + refractor language tree) does not bundle cleanly
// under Vite (it is fine under Next's webpack, which is why the app works). This
// stub renders code as plain <pre><code> so markdown stories still show their
// content and shape without pulling the highlighter into the Vite bundle.
import React from "react";

type Props = {
  language?: string;
  style?: unknown;
  children?: React.ReactNode;
  [key: string]: unknown;
};

export function Prism({ children }: Props) {
  return React.createElement(
    "pre",
    {
      style: {
        background: "#f4f4f5",
        padding: "8px 10px",
        borderRadius: "4px",
        overflowX: "auto",
        fontSize: "12px",
      },
    },
    React.createElement("code", null, children),
  );
}

export const Light = Prism;
export default { Prism, Light };
