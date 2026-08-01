// Metro's defaults, plus one thing: the typefaces.
//
// The desktop client's fonts live in `web/public/fonts`, which is outside this project and
// therefore outside what Metro will resolve. Copying them here would be the obvious fix and the
// wrong one — the two clients are meant to *look* the same, and two copies of a typeface is how
// that quietly stops being true. Watching the directory instead means there is one set of font
// files in the repository and both clients bundle it.
const path = require("node:path");

const { getDefaultConfig } = require("expo/metro-config");

const projectRoot = __dirname;
const config = getDefaultConfig(projectRoot);

config.watchFolders = [path.resolve(projectRoot, "../web/public/fonts")];

module.exports = config;
