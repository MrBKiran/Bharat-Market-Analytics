import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useState, type ReactNode } from "react";

export type Theme = "light" | "dark";
const STORAGE = "bm-theme";

interface ThemeContextValue {
  theme: Theme;
  toggle: () => void;
}
const ThemeContext = createContext<ThemeContextValue>({ theme: "light", toggle: () => {} });

function initialTheme(): Theme {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "dark" || attr === "light") return attr;      // already decided by the inline script in index.html
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  // A layout effect runs before the charts' own effects in the same commit, so they read the NEW theme's colours.
  useLayoutEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(STORAGE, theme);
    } catch {
      /* private mode: the choice just is not remembered */
    }
  }, [theme]);

  const toggle = useCallback(() => setTheme((t) => (t === "dark" ? "light" : "dark")), []);
  const value = useMemo(() => ({ theme, toggle }), [theme, toggle]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export const useTheme = () => useContext(ThemeContext);

/** Read the chart colours from the CSS tokens, so charts follow the theme without a second palette in code. */
export function chartColors() {
  const css = getComputedStyle(document.documentElement);
  const get = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return {
    bg: get("--chart-bg", "#ffffff"),
    grid: get("--chart-grid", "#e8edf1"),
    text: get("--chart-text", "#4a5a69"),
    border: get("--border", "#d9e0e7"),
    up: get("--up", "#0a7a4d"),
    down: get("--down", "#c62828"),
    series: ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"].map((v, i) => get(v, ["#0b6b4f", "#1a56a8", "#b45309", "#7c3aed", "#be185d", "#0e7490"][i])),
  };
}
