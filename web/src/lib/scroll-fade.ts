// Soft edge fades for scrollable regions, replacing the hard divider borders that used
// to separate a panel's header/footer from its content. Applied via a CSS mask (black =
// visible, transparent = hidden).
//
// `scrollFade` fades only the TOP: content dissolves as it scrolls up under the header.
// A permanent bottom fade is deliberately avoided here because it would dim the last line
// of content once it is scrolled fully into view.
//
// `scrollFadeTopBottom` fades BOTH edges. Use it only while there is more content below
// the fold (i.e. NOT scrolled to the bottom) — e.g. the chat transcript toggles to this
// variant when scrolled up so the content softly fades above the composer, and back to the
// top-only variant at the bottom so the assistant's final words stay crisp.
const TOP = 14;
const BOTTOM = 28;
const topGradient = `linear-gradient(to bottom, transparent 0, #000 ${TOP}px, #000 100%)`;
const topBottomGradient = `linear-gradient(to bottom, transparent 0, #000 ${TOP}px, #000 calc(100% - ${BOTTOM}px), transparent 100%)`;

export const scrollFade = {
  maskImage: topGradient,
  WebkitMaskImage: topGradient,
} as const;

export const scrollFadeTopBottom = {
  maskImage: topBottomGradient,
  WebkitMaskImage: topBottomGradient,
} as const;
