/**
 * Single source of the product palette, mirrored from the Web workbench tokens
 * (apps/web/src/styles/index.css :root, light theme). Android reads these values; keep both in step.
 */
export const color = {
  canvas: "#ffffff",
  surface: "#ffffff",
  surfaceMuted: "#f5f6f8",
  ink: "#1d1f24",
  inkMuted: "#5d636e",
  border: "#e4e6ea",
  borderStrong: "#c9cdd4",
  accent: "#000000",
  accentHover: "#2a2c31",
  accentSoft: "#eef0f3",
  weak: "#2b5aa8",
  weakSoft: "#e6edf8",
  strong: "#2f7a42",
  strongSoft: "#e3f0e6",
  evidenceB: "#2f7a42",
  evidenceC: "#9a6200",
  evidenceD: "#9a6200",
  evidenceE: "#c0262d",
  warning: "#9a6200",
  danger: "#c0262d",
  focus: "#000000",
  white: "#ffffff",
} as const;

/** One sans family; Android resolves the platform system font from these names. */
export const typography = {
  displayFamily: "sans-serif",
  uiFamily: "sans-serif",
  monoFamily: "monospace",
  size: { xs: 12, sm: 14, md: 16, lg: 20, xl: 28, display: 28 },
  lineHeight: { compact: 1.25, body: 1.6, reading: 1.75 },
} as const;

export const space = {
  0: 0,
  1: 4,
  2: 8,
  3: 12,
  4: 16,
  5: 20,
  6: 24,
  8: 32,
  10: 40,
  12: 48,
  16: 64,
  20: 80,
} as const;

export const radius = { none: 0, sm: 6, md: 10, lg: 14, xl: 22, round: 999 } as const;
export const elevation = {
  none: "none",
  raised: "0 6px 20px rgb(20 24 33 / 0.06), 0 1px 2px rgb(20 24 33 / 0.04)",
  overlay: "0 12px 40px rgb(20 24 33 / 0.08), 0 2px 6px rgb(20 24 33 / 0.04)",
} as const;
export const motion = {
  fastMs: 120,
  normalMs: 180,
  deliberateMs: 240,
  easing: "cubic-bezier(0.2, 0.8, 0.2, 1)",
} as const;
export const layout = {
  webSidebar: 248,
  webInspector: 320,
  readingMeasure: 760,
  touchWeb: 44,
  touchAndroid: 48,
  breakpointNarrow: 900,
  breakpointCompact: 640,
} as const;
export const chart = {
  history: { color: color.ink, dash: [] as readonly number[], marker: "none" },
  main: { color: color.accent, dash: [] as readonly number[], marker: "circle" },
  weak: { color: color.weak, dash: [8, 5] as readonly number[], marker: "triangle-down" },
  strong: { color: color.strong, dash: [12, 5, 3, 5] as readonly number[], marker: "triangle-up" },
  intervalFill: "rgb(43 90 168 / 0.14)",
  grid: "rgb(60 66 80 / 0.10)",
  today: color.inkMuted,
} as const;

export const semanticPair = {
  action: { foreground: color.white, background: color.accent },
  weak: { foreground: color.weak, background: color.weakSoft },
  strong: { foreground: color.strong, background: color.strongSoft },
  evidenceA: { foreground: color.strong, background: color.strongSoft },
  evidenceB: { foreground: color.evidenceB, background: color.strongSoft },
  evidenceC: { foreground: color.evidenceC, background: "#fbf1e1" },
  evidenceD: { foreground: color.evidenceD, background: "#fbf1e1" },
  evidenceE: { foreground: color.evidenceE, background: "#fbe7e8" },
  warning: { foreground: color.warning, background: "#fbf1e1" },
  danger: { foreground: color.danger, background: "#fbe7e8" },
} as const;

export const component = {
  button: {
    variants: ["primary", "secondary", "quiet", "danger", "caution"] as const,
    sizes: ["compact", "default", "touch"] as const,
    states: ["rest", "hover", "focus", "pressed", "disabled", "loading"] as const,
  },
  evidenceGrade: {
    variants: ["A", "B", "C", "D", "E"] as const,
    slots: ["grade", "title", "sourceKind", "verification", "provenance"] as const,
  },
  probabilityRange: {
    variants: ["premise", "conditional", "joint"] as const,
    slots: ["label", "low", "high", "confidence", "calibration"] as const,
  },
  worldlinePath: {
    variants: ["weak", "main", "strong"] as const,
    slots: ["label", "range", "nodes", "invalidation", "provenance"] as const,
  },
  portfolioMode: {
    variants: ["raw", "public"] as const,
    slots: ["weights", "amounts", "deltas", "constraintReasons", "methodHash"] as const,
  },
  operationStatus: {
    variants: ["idle", "streaming", "paused", "cancelled", "recovering", "failed", "complete"] as const,
    slots: ["label", "progress", "message", "retry", "cancel"] as const,
  },
} as const;

export const tokens = {
  color,
  typography,
  space,
  radius,
  elevation,
  motion,
  layout,
  chart,
  semanticPair,
  component,
} as const;
export type DesignTokens = typeof tokens;
