// Image imports resolve through Next's loader at build time.

declare module "*.png" {
  const content: { src: string; height: number; width: number; blurDataURL?: string };
  export default content;
}

declare module "*.svg" {
  const content: { src: string; height: number; width: number };
  export default content;
}
