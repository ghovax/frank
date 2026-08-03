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

// `shared/` is where the two clients' decisions live and `web/public/fonts` is where their
// typefaces do. Metro resolves only inside the project by default, so both have to be named or
// an import of `@shared/...` is a module it cannot find.
config.watchFolders = [
  path.resolve(projectRoot, "../shared"),
  path.resolve(projectRoot, "../web/public/fonts"),
];

// Metro does not read `tsconfig.json`, so the alias is stated again here. Two places, one value
// — the alternative is `../../shared/...` in every import, which is how a move breaks a client.
config.resolver.extraNodeModules = {
  ...config.resolver.extraNodeModules,
  "@shared": path.resolve(projectRoot, "../shared"),
};

module.exports = config;
