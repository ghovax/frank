// Stand-in style export so the deep `react-syntax-highlighter/dist/esm/styles/hljs`
// import resolves under Vite in Storybook. Returns an empty style object; the
// stub highlighter component ignores it anyway.
export const xcode = {};
export default xcode;
