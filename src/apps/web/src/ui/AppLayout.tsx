import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { MEDIA, useMedia } from "./media";
import { Sidebar } from "./Sidebar";

const SIDE_KEY = "foresightnine.web.sidebar.v1";

interface LayoutState {
  /** Sidebar collapsed on wide screens. */
  collapsed: boolean;
  toggleSide: () => void;
  /** Pages report an open level-2 panel so the inspector can push content at ≥1280. */
  setPanelOpen: (open: boolean) => void;
}

const LayoutContext = createContext<LayoutState>({ collapsed: false, toggleSide: () => undefined, setPanelOpen: () => undefined });

export function useLayout(): LayoutState {
  return useContext(LayoutContext);
}

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDE_KEY) === "collapsed";
  } catch {
    return false;
  }
}

export function AppLayout({ children }: { children: ReactNode }) {
  const wide = useMedia(MEDIA.wide);
  const inspector = useMedia(MEDIA.inspector);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const location = useLocation();

  useEffect(() => setDrawerOpen(false), [location.pathname, wide]);

  const toggleSide = useCallback(() => {
    if (!wide) {
      setDrawerOpen((open) => !open);
      return;
    }
    setCollapsed((value) => {
      const next = !value;
      try {
        localStorage.setItem(SIDE_KEY, next ? "collapsed" : "open");
      } catch {
        // Preference is then kept for this page load only.
      }
      return next;
    });
  }, [wide]);

  const value = useMemo(() => ({ collapsed: wide ? collapsed : true, toggleSide, setPanelOpen }), [wide, collapsed, toggleSide]);
  const classes = ["app"];
  if (wide && collapsed) classes.push("side-collapsed");
  if (panelOpen && inspector) classes.push("with-panel");

  return (
    <LayoutContext.Provider value={value}>
      <div className={classes.join(" ")} data-layout="app">
        {!wide && drawerOpen ? <div className="side-scrim" onClick={() => setDrawerOpen(false)} aria-hidden="true" /> : null}
        <Sidebar drawer={!wide} open={wide ? !collapsed : drawerOpen} onClose={() => setDrawerOpen(false)} onToggle={toggleSide} />
        <main className="main">{children}</main>
      </div>
    </LayoutContext.Provider>
  );
}
