// Image imports resolve through Next's loader at build time. Next writes an equivalent
// declaration into the generated `next-env.d.ts`, which is not committed, so declaring them
// here is what lets a bare `tsc` typecheck the project without a build having run first.

declare module "*.png" {
  const content: { src: string; height: number; width: number; blurDataURL?: string };
  export default content;
}

declare module "*.svg" {
  const content: { src: string; height: number; width: number };
  export default content;
}
