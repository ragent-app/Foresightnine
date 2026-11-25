import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { usePeerCompare, usePeerSuggest, type PeerComparisonMatrix } from "../../api/peers";

interface PeerCoordinateBarProps {
  symbol?: string | null;
}

/**
 * Compact 1-line relative peer coordinate strip mounted in TopicPage next to ThesisLedgerBar.
 * Defaults strictly to `cache_only: true` on mount so opening a Topic never triggers live SEC fan-out.
 */
export function PeerCoordinateBar({ symbol }: PeerCoordinateBarProps) {
  const sym = (symbol || "").trim().toUpperCase();
  const [matrix, setMatrix] = useState<PeerComparisonMatrix | null>(null);
  const [checkedCache, setCheckedCache] = useState(false);
  const suggest = usePeerSuggest(sym, Boolean(sym));
  const compare = usePeerCompare();

  const recommendedPeers = suggest.data?.recommended_peers ?? [];
  const allSymbols = sym ? [sym, ...recommendedPeers].slice(0, 5) : [];

  useEffect(() => {
    if (!sym || allSymbols.length < 2 || checkedCache) return;
    setCheckedCache(true);
    compare.mutate(
      { primary_symbol: sym, symbols: allSymbols, cache_only: true },
      {
        onSuccess: (data) => {
          if (data.companies.length >= 2) {
            setMatrix(data);
          }
        },
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sym, recommendedPeers.join(",")]);

  if (!sym) return null;

  const compareHref = `/compare?primary=${encodeURIComponent(sym)}&peers=${encodeURIComponent(recommendedPeers.slice(0, 4).join(","))}`;

  return (
    <div className="peer-coord-bar" role="region" aria-label="同业相对坐标">
      <div className="peer-coord-left">
        <span className="peer-coord-tag">同业坐标</span>
        {matrix && matrix.companies.length >= 2 ? (
          <span className="peer-coord-summary">{matrix.primary_coordinate_summary}</span>
        ) : (
          <span className="peer-coord-muted">
            {compare.isPending ? "正在计算同业坐标…" : `对比组：${allSymbols.join(" / ") || sym}`}
          </span>
        )}
      </div>
      <div className="peer-coord-actions">
        {(!matrix || matrix.companies.length < 2) && allSymbols.length >= 2 ? (
          <button
            type="button"
            className="btn peer-coord-btn"
            disabled={compare.isPending}
            onClick={() =>
              compare.mutate(
                { primary_symbol: sym, symbols: allSymbols, cache_only: false },
                { onSuccess: (data) => setMatrix(data) },
              )
            }
          >
            {compare.isPending ? "加载中…" : "加载同业坐标 (0 LLM)"}
          </button>
        ) : null}
        <Link className="peer-coord-link" to={compareHref}>
          展开完整同业矩阵 →
        </Link>
      </div>
    </div>
  );
}
