// Иконки — встроенный SVG (CSP: только свои ресурсы). Декоративные: aria-hidden.
import type { SVGProps } from "react";

function Icon({ children, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor" aria-hidden="true" focusable="false" {...props}>
      {children}
    </svg>
  );
}

export const MenuIcon = () => (
  <Icon>
    <path d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z" />
  </Icon>
);
export const SearchIcon = () => (
  <Icon>
    <path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16a6.47 6.47 0 0 0 4.23-1.57l.27.28v.79l5 4.99L20.49 19zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14" />
  </Icon>
);
export const ClearIcon = () => (
  <Icon>
    <path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
  </Icon>
);
export const HomeIcon = () => (
  <Icon>
    <path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z" />
  </Icon>
);
export const VideosIcon = () => (
  <Icon>
    <path d="M4 6H2v14a2 2 0 0 0 2 2h14v-2H4zm16-4H8a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2m-8 12.5v-9l6 4.5z" />
  </Icon>
);
export const UploadIcon = () => (
  <Icon>
    <path d="M17 10.5V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11zM14 13h-3v3H9v-3H6v-2h3V8h2v3h3z" />
  </Icon>
);
export const LogoIcon = () => (
  <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true" focusable="false">
    <circle cx="12" cy="12" r="11" fill="var(--brand)" />
    <path d="M9.5 7.5v9l7-4.5z" fill="#fff" />
  </svg>
);
