// Metro's defaults, plus the typefaces, which live outside this project.
const path = require("node:path");

const { getDefaultConfig } = require("expo/metro-config");

const projectRoot = __dirname;
const config = getDefaultConfig(projectRoot);

// Both the shared decisions and the shared typefaces live outside the project, so both must be named.
config.watchFolders = [
  path.resolve(projectRoot, "../shared"),
  path.resolve(projectRoot, "../web/public/fonts"),
];

// Metro does not read the TypeScript configuration, so the alias is stated again here.
config.resolver.extraNodeModules = {
  ...config.resolver.extraNodeModules,
  "@shared": path.resolve(projectRoot, "../shared"),
};

module.exports = config;
