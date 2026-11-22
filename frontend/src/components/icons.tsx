// A handful of inline icons (no icon library needed). Decorative: always hidden from screen readers.
import type { ReactNode } from "react";

function Icon({ children, size = 18 }: { children: ReactNode; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {children}
    </svg>
  );
}

export const IconHome = () => <Icon><path d="M3 11l9-8 9 8" /><path d="M5 10v10h14V10" /></Icon>;
export const IconChart = () => <Icon><path d="M3 3v18h18" /><path d="M7 15l4-5 3 3 5-7" /></Icon>;
export const IconCompare = () => <Icon><path d="M4 20V10" /><path d="M10 20V4" /><path d="M16 20v-8" /><path d="M22 20H2" /></Icon>;
export const IconSectors = () => <Icon><circle cx="12" cy="12" r="9" /><path d="M12 3v9l7 4" /></Icon>;
export const IconSearch = () => <Icon><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></Icon>;
export const IconFlask = () => <Icon><path d="M9 3h6" /><path d="M10 3v6L4.5 19a1.5 1.5 0 001.3 2.2h12.4a1.5 1.5 0 001.3-2.2L14 9V3" /></Icon>;
export const IconData = () => <Icon><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></Icon>;
export const IconReport = () => <Icon><path d="M6 2h9l5 5v15H6z" /><path d="M15 2v5h5" /><path d="M9 13h8M9 17h8M9 9h3" /></Icon>;
export const IconSun = () => <Icon><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></Icon>;
export const IconMoon = () => <Icon><path d="M21 13A9 9 0 1111 3a7 7 0 0010 10z" /></Icon>;
export const IconMenu = () => <Icon><path d="M3 6h18M3 12h18M3 18h18" /></Icon>;
export const IconKey = () => <Icon><circle cx="8" cy="15" r="4" /><path d="M10.8 12.2L21 2M16 7l3 3" /></Icon>;
export const BrandMark = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M3 17l6-7 5 4 7-9" />
  </svg>
);
