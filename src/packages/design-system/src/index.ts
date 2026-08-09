export const color = {
  canvas: "#f7f2e9",
  surface: "#fbf8f2",
  surfaceMuted: "#f0e8dc",
  ink: "#2f2119",
  inkMuted: "#74675d",
  border: "#d9cdbf",
  borderStrong: "#b9a897",
  accent: "#a94325",
  accentHover: "#a63f22",
  accentSoft: "#f2d9cc",
  weak: "#3f6688",
  weakSoft: "#dce8f1",
  strong: "#486a43",
  strongSoft: "#dfe9dc",
  evidenceB: "#516540",
  evidenceC: "#6f4b10",
  evidenceD: "#75431e",
  evidenceE: "#85392d",
  warning: "#9b5b1d",
  danger: "#a3382a",
  focus: "#c14f2e",
  white: "#ffffff",
} as const;

export const typography = {
  displayFamily: '"Noto Serif SC", "Source Han Serif SC", Georgia, serif',
  uiFamily: 'Inter, "Noto Sans SC", "Source Han Sans SC", system-ui, sans-serif',
  monoFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, monospace',
  size: { xs: 12, sm: 14, md: 16, lg: 20, xl: 28, display: 48 },
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

export const radius = { none: 0, sm: 4, md: 8, lg: 12, round: 999 } as const;
export const elevation = {
  none: "none",
  raised: "0 8px 28px rgb(47 33 25 / 0.08)",
  overlay: "0 16px 48px rgb(47 33 25 / 0.14)",
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
  intervalFill: "rgb(189 77 44 / 0.16)",
  grid: "rgb(116 103 93 / 0.18)",
  today: color.inkMuted,
} as const;

export const semanticPair = {
  action: { foreground: color.white, background: color.accent },
  weak: { foreground: "#355c7d", background: color.weakSoft },
  strong: { foreground: "#3e6039", background: color.strongSoft },
  evidenceA: { foreground: "#3e6039", background: "#e2eadf" },
  evidenceB: { foreground: color.evidenceB, background: "#e6eadf" },
  evidenceC: { foreground: color.evidenceC, background: "#f0e3c8" },
  evidenceD: { foreground: color.evidenceD, background: "#f0ded0" },
  evidenceE: { foreground: color.evidenceE, background: "#efd9d3" },
  warning: { foreground: "#6d3c0d", background: "#f4e5cf" },
  danger: { foreground: "#812b20", background: "#f2d9d3" },
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
