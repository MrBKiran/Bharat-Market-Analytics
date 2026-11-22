import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useHealth } from "../api/hooks";
import { getApiKey } from "../api/client";
import { useTheme } from "../theme";
import { ApiKeyDialog } from "./ApiKeyDialog";
import { BrandMark, IconChart, IconCompare, IconData, IconFlask, IconHome, IconKey, IconMenu, IconMoon, IconSearch, IconSectors, IconSun } from "./icons";

const NAV = [
  { to: "/", label: "Overview", icon: <IconHome />, end: true },
  { to: "/stock", label: "Stock chart", icon: <IconChart /> },
  { to: "/compare", label: "Compare", icon: <IconCompare /> },
  { to: "/sectors", label: "Sectors", icon: <IconSectors /> },
  { to: "/search", label: "Search", icon: <IconSearch /> },
  { to: "/backtest", label: "Backtesting", icon: <IconFlask /> },
  { to: "/data", label: "Data & imports", icon: <IconData /> },
];

function ApiStatus() {
  const { data, isError, isLoading } = useHealth();
  const ok = !isError && data?.status === "ok";
  const text = isLoading ? "Checking API…" : ok ? "API online" : data ? "Database down" : "API offline";
  return (
    <span className="row muted" role="status" style={{ gap: "0.4rem" }} title={ok ? `MongoDB ${data?.database.server_version ?? ""}` : "The API or MongoDB is not reachable"}>
      <span className={`dot ${isLoading ? "" : ok ? "ok" : "bad"}`} aria-hidden="true" />
      <span>{text}</span>
    </span>
  );
}

export function Layout() {
  const [open, setOpen] = useState(false);
  const [keyOpen, setKeyOpen] = useState(false);
  const { theme, toggle } = useTheme();
  const location = useLocation();
  useEffect(() => setOpen(false), [location.pathname]);          // close the drawer after navigating on a phone
  const hasKey = !!getApiKey();

  return (
    <div className="app">
      <div className={`scrim ${open ? "open" : ""}`} onClick={() => setOpen(false)} aria-hidden="true" />
      <aside className={`sidebar ${open ? "open" : ""}`} aria-label="Main navigation">
        <div className="brand">
          <span className="brand-mark"><BrandMark /></span>
          <span>BharatMarket<small>Analytics</small></span>
        </div>
        <nav className="nav">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}>
              {item.icon}
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          NSE &amp; BSE · prices in ₹<br />
          Split-adjusted, not dividend-adjusted.
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <button className="btn btn-ghost menu-btn" aria-label="Open menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}><IconMenu /></button>
          <span className="grow"><ApiStatus /></span>
          <button className="btn btn-sm" onClick={() => setKeyOpen(true)} title="Needed only for actions that change data">
            <IconKey /> API key{hasKey ? " ✓" : ""}
          </button>
          <button className="btn btn-sm" onClick={toggle} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
            {theme === "dark" ? <IconSun /> : <IconMoon />}
            <span>{theme === "dark" ? "Light" : "Dark"}</span>
          </button>
        </header>
        <main className="page" id="content">
          <Outlet />
        </main>
      </div>
      <ApiKeyDialog open={keyOpen} onClose={() => setKeyOpen(false)} />
    </div>
  );
}
