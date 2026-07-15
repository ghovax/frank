import { chakra } from "@chakra-ui/react";

// Semantic elements that Chakra does not expose as named components. Keeping the
// factory in this one root module makes every call site consume configured,
// theme-aware components instead of constructing ad-hoc primitives.
export const Canvas = chakra("canvas");
export const DeletedText = chakra("del");
export const Emphasis = chakra("em");
export const Frame = chakra("iframe");
export const Pre = chakra("pre");
export const Section = chakra("section");
export const Strong = chakra("strong");
