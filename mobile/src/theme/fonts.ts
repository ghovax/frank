/**
 * The typefaces, loaded from the same files the desktop client serves.
 *
 * A browser takes one family and picks a weight out of it; React Native takes a *file* per
 * weight and calls each one a family. So the six entries below are six OTFs rather than three,
 * and `tokens.ts` names them the way a component will ask for them.
 *
 * Italics are deliberately absent. The desktop carries four of them for prose; on a phone the
 * only italic text is emphasis inside markdown, and RN will synthesise that from the regular
 * face convincingly enough to not be worth four more files in the bundle.
 */
export const FONT_SOURCES = {
  FrankSans: require("../../../web/public/fonts/sans/regular.otf"),
  "FrankSans-Medium": require("../../../web/public/fonts/sans/medium.otf"),
  "FrankSans-Semibold": require("../../../web/public/fonts/sans/semibold.otf"),
  "FrankSans-Bold": require("../../../web/public/fonts/sans/bold.otf"),
  "FrankDisplay-Bold": require("../../../web/public/fonts/display/bold.otf"),
  FrankMono: require("../../../web/public/fonts/mono/regular.otf"),
};
