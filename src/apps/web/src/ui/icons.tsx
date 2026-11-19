import type { LucideIcon, LucideProps } from "lucide-react";
import {
  ArrowUp,
  BookOpen,
  Check,
  ChevronRight,
  Download,
  Ellipsis,
  Globe,
  LineChart,
  LogOut,
  PanelLeft,
  PanelRight,
  PenLine,
  Plus,
  Search,
  Settings,
  Sigma,
  SlidersHorizontal,
  SquarePen,
  X,
} from "lucide-react";

// Spec §2.4: 24 grid, stroke 1.6, displayed at 18 (16 in menus and steps).
export const Icons = {
  sidebar: PanelLeft,
  compose: SquarePen,
  plus: Plus,
  send: ArrowUp,
  close: X,
  more: Ellipsis,
  tune: SlidersHorizontal,
  inspector: PanelRight,
  search: Search,
  settings: Settings,
  logout: LogOut,
  read: BookOpen,
  fetch: Download,
  pen: PenLine,
  sum: Sigma,
  check: Check,
  chev: ChevronRight,
  download: Download,
  globe: Globe,
  chart: LineChart,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof Icons;

export function Icon({ name, size = 18, ...rest }: { name: IconName; size?: number } & Omit<LucideProps, "ref">) {
  const Component = Icons[name];
  const strokeWidth = name === "send" ? 2.1 : 1.6;
  return <Component className="i" size={size} strokeWidth={strokeWidth} aria-hidden="true" focusable="false" {...rest} />;
}

const TOOL_ICON: Record<string, IconName> = {
  read_sources: "read",
  discover_sources: "globe",
  acquire_sources: "fetch",
  historical_prices: "chart",
  record_facts: "pen",
  calculate_financials: "sum",
  submit_research: "check",
};

export function toolIcon(tool: string | null): IconName {
  return (tool && TOOL_ICON[tool]) || "check";
}
