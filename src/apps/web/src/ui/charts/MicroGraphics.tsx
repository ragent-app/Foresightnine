interface SparklineProps {
  values: readonly number[];
  ariaLabel: string;
  tone?: "positive" | "negative" | "neutral";
  width?: number;
  height?: number;
  className?: string;
}

/** Zero-dependency pure SVG inline sparkline controlled entirely via CSS classes (CSP-safe). */
export function Sparkline({
  values,
  ariaLabel,
  tone = "neutral",
  width = 56,
  height = 18,
  className,
}: SparklineProps) {
  const clean = values.filter((v) => Number.isFinite(v));
  const padX = 2.5;
  const padY = 2.5;
  const innerW = Math.max(1, width - padX * 2);
  const innerH = Math.max(1, height - padY * 2);

  let points = "";
  let endX = padX;
  let endY = padY + innerH / 2;

  if (clean.length === 1) {
    endX = width - padX;
    points = `${padX},${endY.toFixed(2)} ${endX.toFixed(2)},${endY.toFixed(2)}`;
  } else if (clean.length >= 2) {
    const min = Math.min(...clean);
    const max = Math.max(...clean);
    const span = max - min;
    const coords = clean.map((v, idx) => {
      const x = padX + (idx / (clean.length - 1)) * innerW;
      const y = span === 0 ? padY + innerH / 2 : padY + (1 - (v - min) / span) * innerH;
      return [x, y] as const;
    });
    points = coords.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
    const last = coords[coords.length - 1]!;
    endX = last[0];
    endY = last[1];
  }

  const classes = ["micro-sparkline", `is-${tone}`, className ?? ""].filter(Boolean).join(" ");

  return (
    <svg
      className={classes}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={ariaLabel}
    >
      <title>{ariaLabel}</title>
      {points ? (
        <>
          <polyline className="micro-sparkline-line" fill="none" points={points} />
          <circle className="micro-sparkline-dot" cx={endX.toFixed(2)} cy={endY.toFixed(2)} r={1.8} />
        </>
      ) : (
        <line
          className="micro-sparkline-empty"
          x1={padX}
          y1={height / 2}
          x2={width - padX}
          y2={height / 2}
        />
      )}
    </svg>
  );
}

interface RangeBarProps {
  min: number;
  max: number;
  low: number;
  high: number;
  marker?: number;
  ariaLabel: string;
  tone?: "positive" | "negative" | "neutral";
  width?: number;
  height?: number;
  className?: string;
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value));
}

/** Zero-dependency pure SVG interval/quantile/probability range bar (no single-point precision illusion). */
export function RangeBar({
  min,
  max,
  low,
  high,
  marker,
  ariaLabel,
  tone = "neutral",
  width = 96,
  height = 14,
  className,
}: RangeBarProps) {
  const safeMin = Number.isFinite(min) ? min : 0;
  const safeMax = Number.isFinite(max) && max > safeMin ? max : safeMin + 1;
  const span = safeMax - safeMin;

  const rawLo = Number.isFinite(low) ? low : safeMin;
  const rawHi = Number.isFinite(high) ? high : safeMax;
  const lo = clamp(Math.min(rawLo, rawHi), safeMin, safeMax);
  const hi = clamp(Math.max(rawLo, rawHi), safeMin, safeMax);

  const trackX = 2;
  const trackW = Math.max(4, width - trackX * 2);
  const trackY = (height - 4) / 2;

  const xLo = trackX + ((lo - safeMin) / span) * trackW;
  const xHi = trackX + ((hi - safeMin) / span) * trackW;
  const bandW = Math.max(4, xHi - xLo);
  const bandX = clamp(xLo, trackX, trackX + trackW - bandW);

  const hasMarker = typeof marker === "number" && Number.isFinite(marker);
  const markerX = hasMarker ? trackX + ((clamp(marker, safeMin, safeMax) - safeMin) / span) * trackW : null;

  const classes = ["micro-range", `is-${tone}`, className ?? ""].filter(Boolean).join(" ");

  return (
    <svg
      className={classes}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={ariaLabel}
    >
      <title>{ariaLabel}</title>
      <rect className="micro-range-track" x={trackX} y={trackY} width={trackW} height={4} rx={2} />
      <rect className="micro-range-band" x={bandX.toFixed(2)} y={trackY} width={bandW.toFixed(2)} height={4} rx={2} />
      {markerX !== null ? (
        <circle className="micro-range-marker" cx={markerX.toFixed(2)} cy={height / 2} r={3.2} />
      ) : null}
    </svg>
  );
}
