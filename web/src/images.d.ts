// Declared here so a bare `tsc` typechecks without the generated `next-env.d.ts` a build would write.

declare module "*.png" {
  const content: { src: string; height: number; width: number; blurDataURL?: string };
  export default content;
}

declare module "*.svg" {
  const content: { src: string; height: number; width: number };
  export default content;
}
