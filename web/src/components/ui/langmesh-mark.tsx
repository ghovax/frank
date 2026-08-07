import type { SVGProps } from "react";

/** The LangMesh mark on its own, without the container an app icon needs. */
export function LangMeshMark({
  size = "1em",
  ...props
}: { size?: string | number } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 100 100"
      width={size}
      height={size}
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
      xmlns="http://www.w3.org/2000/svg"
      {...props}
    >
      <path
        d="M17 28 C17 16 30 9 50 9 C70 9 83 16 83 28 C85 47 85 69 82 79 C79 88 66 93 50 93 C34 93 21 88 18 79 C15 69 15 47 17 28 Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="8"
        strokeLinejoin="round"
      />
      <path d="M18 31 C33 22 68 21 82 30 C83 19 80 12 76 9 C61 2 39 2 24 9 C20 12 17 20 18 31 Z" />
      <path d="M6 58 C12 56 16 58 16 63 C16 69 12 71 6 69 Z" />
      <path d="M94 56 C88 54 84 56 84 61 C84 67 88 69 94 67 Z" />
      <ellipse cx="38" cy="51" rx="6.2" ry="7.4" />
      <path
        d="M55 52 C59 46 68 46 71 51"
        fill="none"
        stroke="currentColor"
        strokeWidth="6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M35 69 C44 80 58 80 67 66"
        fill="none"
        stroke="currentColor"
        strokeWidth="7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
