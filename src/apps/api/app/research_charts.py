"""Deterministic, evidence-bound chart specification and verification for research runs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import json
import re
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .research_calculations import is_percent_unit
from .research_source_policy import source_eligibility
from .research_sources import ResearchSource

ChartType = Literal[
    "bar",
    "grouped_bar",
    "stacked_bar",
    "line",
    "combo_bar_line",
    "waterfall",
    "scatter",
    "price_series",
    "causal_graph",
]
ChartMark = Literal["bar", "line", "area", "scatter", "waterfall"]
ChartAxisId = Literal["left", "right"]
ChartAxisScale = Literal["ones", "thousands", "millions", "billions", "trillions"]
CausalNodeCategory = Literal["macro", "industry", "company", "asset", "scenario"]
CausalNodeStatus = Literal["confirmed", "assumption", "invalidated"]
CausalEdgeDirection = Literal["positive", "negative", "uncertain", "conditional"]

_SCALE_EXPONENT: dict[ChartAxisScale, int] = {
    "ones": 0,
    "thousands": 3,
    "millions": 6,
    "billions": 9,
    "trillions": 12,
}
_ORDERED_SCALES: tuple[ChartAxisScale, ...] = ("ones", "thousands", "millions", "billions", "trillions")

_SCALE_WORD_MAP: dict[str, ChartAxisScale] = {
    "one": "ones",
    "ones": "ones",
    "1": "ones",
    "unit": "ones",
    "units": "ones",
    "thousand": "thousands",
    "thousands": "thousands",
    "k": "thousands",
    "千": "thousands",
    "million": "millions",
    "millions": "millions",
    "m": "millions",
    "百万": "millions",
    "百萬": "millions",
    "billion": "billions",
    "billions": "billions",
    "b": "billions",
    "十亿": "billions",
    "十億": "billions",
    "trillion": "trillions",
    "trillions": "trillions",
    "t": "trillions",
    "万亿": "trillions",
    "萬億": "trillions",
}

_ISO_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "KRW", "CNY", "RMB", "HKD", "TWD",
    "CHF", "CAD", "AUD", "NZD", "SGD", "INR", "BRL", "MXN", "SEK",
    "NOK", "DKK", "ZAR", "SAR", "AED",
})
_CURRENCY_WORDS: dict[str, str] = {
    "dollar": "USD",
    "dollars": "USD",
    "us dollar": "USD",
    "us dollars": "USD",
    "euro": "EUR",
    "euros": "EUR",
    "won": "KRW",
    "korean won": "KRW",
    "yen": "JPY",
    "japanese yen": "JPY",
    "yuan": "CNY",
    "renminbi": "CNY",
    "pound": "GBP",
    "pounds": "GBP",
    "sterling": "GBP",
}

_MAX_HISTORICAL_PRICE_POINTS = 1000
_BAR_INTERVALS = frozenset({"1min", "5min", "15min", "30min", "1h", "1day", "1week", "1month"})
_BAR_LINE_RE = re.compile(r"^O (\S+) H (\S+) L (\S+) C (\S+) V (\S+)(?: ([A-Za-z]{3}))?(?: ADJF (\S+))?$")
_HIST_PRICE_LINE_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s+(.+?)\s*$"
)


class ChartBuildError(ValueError):
    """Raised when chart arguments violate evidence, permission, or comparability rules."""


class CreateChartYAxis(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: ChartAxisId
    label: str = Field(min_length=1, max_length=80)
    unit_preference: str | None = Field(default=None, min_length=1, max_length=64)
    currency: str | None = Field(default=None, min_length=1, max_length=16)


class FactsChartBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["facts"]
    fact_ids: list[str] = Field(min_length=1, max_length=24)


class CalculationsChartBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["calculations"]
    calculation_ids: list[str] = Field(min_length=1, max_length=24)


class HistoricalPricesChartBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["historical_prices"]
    source_id: str = Field(min_length=1, max_length=128)
    normalize_base_100: bool = False


class SecConceptSeriesChartBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["sec_concept_series"]
    source_id: str = Field(min_length=1, max_length=128)
    form_filter: Literal["10-K", "10-Q", "all"] = "all"
    max_points: int = Field(default=20, ge=1, le=40)


ChartSeriesBinding = (
    FactsChartBinding
    | CalculationsChartBinding
    | HistoricalPricesChartBinding
    | SecConceptSeriesChartBinding
)


class CreateChartSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    mark: ChartMark
    y_axis_id: ChartAxisId = "left"
    binding: ChartSeriesBinding


class CausalGraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1, max_length=120)
    category: CausalNodeCategory
    status: CausalNodeStatus = "assumption"
    detail: str | None = Field(default=None, max_length=240)

    @field_validator("label")
    @classmethod
    def _nonblank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Node label must not be blank.")
        return value

    @field_validator("detail")
    @classmethod
    def _normalize_detail(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value if value.strip() else None


class CausalGraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    from_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    to_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    relation: str = Field(min_length=1, max_length=120)
    direction: CausalEdgeDirection = "uncertain"
    claim_ids: list[str] = Field(default_factory=list, max_length=10)
    unverified_assumption: bool = False

    @field_validator("relation")
    @classmethod
    def _nonblank_relation(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Edge relation must not be blank.")
        return value

    @field_validator("claim_ids")
    @classmethod
    def _validate_claim_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            cid = item.strip()
            if not cid:
                raise ValueError("Edge claim_ids entries must not be blank.")
            if cid not in seen:
                seen.add(cid)
                cleaned.append(cid)
        return cleaned


class CausalGraphSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    nodes: list[CausalGraphNode] = Field(min_length=2, max_length=20)
    edges: list[CausalGraphEdge] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def _validate_graph(self) -> CausalGraphSpec:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Causal graph node ids must be unique.")
        node_id_set = set(node_ids)
        seen_edges: set[tuple[str, str]] = set()
        touched_nodes: set[str] = set()
        for edge in self.edges:
            if edge.from_id == edge.to_id:
                raise ValueError(f"Self-loop edge {edge.from_id!r} -> {edge.to_id!r} is not allowed.")
            if edge.from_id not in node_id_set:
                raise ValueError(f"Edge from_id {edge.from_id!r} does not reference a defined node.")
            if edge.to_id not in node_id_set:
                raise ValueError(f"Edge to_id {edge.to_id!r} does not reference a defined node.")
            pair = (edge.from_id, edge.to_id)
            if pair in seen_edges:
                raise ValueError(f"Duplicate edge {edge.from_id!r} -> {edge.to_id!r} is not allowed.")
            seen_edges.add(pair)
            touched_nodes.add(edge.from_id)
            touched_nodes.add(edge.to_id)
        isolated = sorted(node_id_set - touched_nodes)
        if isolated:
            raise ValueError(f"Isolated nodes without edges are not allowed: {', '.join(isolated)}.")
        return self


class CreateChartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    chart_type: ChartType
    title: str = Field(min_length=1, max_length=200)
    x_label: str = Field(default="", max_length=80)
    y_axes: list[CreateChartYAxis] = Field(default_factory=list, max_length=2)
    series: list[CreateChartSeries] = Field(default_factory=list, max_length=6)
    causal_graph: CausalGraphSpec | None = None
    replaces_chart_id: str | None = Field(default=None, min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=300)

    @field_validator("title", "rationale")
    @classmethod
    def _nonblank_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Field must not be blank.")
        return value

    @model_validator(mode="after")
    def _validate_structure(self) -> CreateChartRequest:
        if self.chart_type == "causal_graph":
            if self.causal_graph is None:
                raise ValueError("causal_graph is required when chart_type is 'causal_graph'.")
            if self.y_axes or self.series:
                raise ValueError("y_axes and series must be empty when chart_type is 'causal_graph'.")
            return self
        if self.causal_graph is not None:
            raise ValueError("causal_graph is only allowed when chart_type is 'causal_graph'.")
        if not (1 <= len(self.y_axes) <= 2):
            raise ValueError("y_axes must contain between 1 and 2 items.")
        if not (1 <= len(self.series) <= 6):
            raise ValueError("series must contain between 1 and 6 items.")
        axis_ids = [ax.id for ax in self.y_axes]
        if len(axis_ids) != len(set(axis_ids)):
            raise ValueError("y_axes ids must be distinct.")
        axis_id_set = set(axis_ids)
        series_names = [item.name for item in self.series]
        if len(series_names) != len(set(series_names)):
            raise ValueError("series names must be distinct.")
        bound_axes: set[ChartAxisId] = set()
        for item in self.series:
            if item.y_axis_id not in axis_id_set:
                raise ValueError(f"Series {item.name!r} references undefined y_axis_id {item.y_axis_id!r}.")
            bound_axes.add(item.y_axis_id)
        if bound_axes != axis_id_set:
            raise ValueError("Every declared y_axis must be bound by at least one series.")
        return self


class ChartAxis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ChartAxisId
    label: str
    unit: str
    currency: str | None = None
    scale: ChartAxisScale = "ones"


class ChartProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str | None = None
    calculation_id: str | None = None
    source_id: str
    character_start: int | None = Field(default=None, ge=0)
    character_end: int | None = Field(default=None, gt=0)


class ChartPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: str
    y: str
    display_value: str
    is_projected: bool = False
    provenance: ChartProvenance


class ChartSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    mark: ChartMark
    y_axis_id: ChartAxisId = "left"
    is_projected: bool = False
    points: list[ChartPoint]


class ResearchChart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_id: str
    turn_id: str | None = None
    chart_type: ChartType
    title: str
    subtitle: str = ""
    x_label: str = ""
    axes: list[ChartAxis] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)
    causal_graph: CausalGraphSpec | None = None
    is_simulated: bool = False
    has_projected_points: bool = False
    replaces_chart_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    normalization_base: str | None = None

    @model_validator(mode="after")
    def _validate_mode(self) -> ResearchChart:
        if self.chart_type == "causal_graph":
            if self.causal_graph is None:
                raise ValueError("causal_graph is required when chart_type is 'causal_graph'.")
            if self.axes or self.series:
                raise ValueError("axes and series must be empty when chart_type is 'causal_graph'.")
        else:
            if self.causal_graph is not None:
                raise ValueError("causal_graph is only allowed when chart_type is 'causal_graph'.")
            if not self.axes or not self.series:
                raise ValueError("axes and series must be non-empty for numeric charts.")
        return self


@dataclass(frozen=True)
class _UnitSpec:
    family: str
    scale: ChartAxisScale
    currency: str | None
    canonical_unit: str


@dataclass
class _RawPoint:
    x: str
    value: Decimal
    unit_spec: _UnitSpec
    provenance: ChartProvenance
    is_projected: bool = False
    normalized_base_100: bool = False


def _to_decimal(raw: Any, *, what: str) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise ChartBuildError(f"Invalid numeric value for {what}.")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ChartBuildError(f"Invalid numeric value for {what}.") from error
    if not value.is_finite():
        raise ChartBuildError(f"Non-finite numeric value for {what}.")
    return value


def _format_decimal_plain(value: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = 100
        normalized = value.normalize()
        if normalized == 0:
            return "0"
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"


def _format_grouped_number(text: str) -> str:
    sign = ""
    body = text
    if body.startswith("-"):
        sign = "-"
        body = body[1:]
    if "." in body:
        whole, frac = body.split(".", 1)
        return f"{sign}{int(whole):,}.{frac}"
    return f"{sign}{int(body):,}"


def _classify_unit(
    unit: str,
    currency: str | None = None,
    *,
    is_historical_price: bool = False,
    normalized_base_100: bool = False,
) -> _UnitSpec:
    norm = " ".join(str(unit or "").strip().split())
    if not norm:
        raise ChartBuildError("Observation unit must not be empty.")
    norm_curr = currency.strip().upper() if isinstance(currency, str) and currency.strip() else None

    if normalized_base_100:
        return _UnitSpec(
            family="index_base_100",
            scale="ones",
            currency=None,
            canonical_unit="index (base=100)",
        )

    if is_percent_unit(norm):
        if norm_curr is not None:
            raise ChartBuildError("Percentage units must not carry a monetary currency.")
        return _UnitSpec(family="percent", scale="ones", currency=None, canonical_unit="percent")

    lowered = norm.casefold()
    if re.fullmatch(r"percentage\s+points?|百分点|百分點|pp", lowered):
        if norm_curr is not None:
            raise ChartBuildError("Percentage-point units must not carry a monetary currency.")
        return _UnitSpec(family="percentage_points", scale="ones", currency=None, canonical_unit="percentage points")

    if re.fullmatch(r"basis\s+points?|\bbps\b|基点|基點", lowered):
        if norm_curr is not None:
            raise ChartBuildError("Basis-point units must not carry a monetary currency.")
        return _UnitSpec(family="bps", scale="ones", currency=None, canonical_unit="bps")

    if is_historical_price:
        resolved_curr = norm_curr
        if resolved_curr is None:
            paren_match = re.search(r"\(([A-Za-z]{3})\)\s*$", norm)
            if paren_match and paren_match.group(1).upper() in _ISO_CURRENCIES:
                resolved_curr = paren_match.group(1).upper()
            elif norm.upper() in _ISO_CURRENCIES:
                resolved_curr = norm.upper()
        if resolved_curr is None:
            raise ChartBuildError("Historical price series must declare a currency.")
        return _UnitSpec(
            family="historical_price",
            scale="ones",
            currency=resolved_curr,
            canonical_unit=norm,
        )

    if re.search(r"(?:per\s+share|/\s*shares?\b)", lowered):
        found_curr: str | None = None
        for token in re.findall(r"[A-Za-z]{3}", norm.upper()):
            if token in _ISO_CURRENCIES:
                found_curr = token
                break
        if found_curr and norm_curr and found_curr != norm_curr:
            raise ChartBuildError(f"Conflicting currencies {found_curr} vs {norm_curr} in unit {norm!r}.")
        resolved = norm_curr or found_curr
        return _UnitSpec(
            family="price_per_share",
            scale="ones",
            currency=resolved,
            canonical_unit=norm,
        )

    if re.fullmatch(r"shares?", lowered):
        return _UnitSpec(family="shares", scale="ones", currency=None, canonical_unit="shares")

    # Try monetary / scaled pattern (e.g., "millions", "USD millions", "billions of USD", "USD").
    cleaned_phrase = lowered.replace(" of ", " ")
    tokens = [tok for tok in re.split(r"[\s,/()]+", cleaned_phrase) if tok]
    if not tokens:
        raise ChartBuildError(f"Invalid unit {unit!r}.")

    scale: ChartAxisScale | None = None
    unit_currency: str | None = None
    unmatched: list[str] = []

    if cleaned_phrase in _CURRENCY_WORDS:
        unit_currency = _CURRENCY_WORDS[cleaned_phrase]
    else:
        i = 0
        while i < len(tokens):
            two = f"{tokens[i]} {tokens[i + 1]}" if i + 1 < len(tokens) else ""
            if two in _CURRENCY_WORDS:
                if unit_currency and unit_currency != _CURRENCY_WORDS[two]:
                    raise ChartBuildError(f"Multiple currencies found in unit {unit!r}.")
                unit_currency = _CURRENCY_WORDS[two]
                i += 2
                continue
            tok = tokens[i]
            upper_tok = tok.upper()
            if upper_tok in _ISO_CURRENCIES:
                iso = "CNY" if upper_tok == "RMB" else upper_tok
                if unit_currency and unit_currency != iso:
                    raise ChartBuildError(f"Multiple currencies found in unit {unit!r}.")
                unit_currency = iso
            elif tok in _CURRENCY_WORDS:
                iso = _CURRENCY_WORDS[tok]
                if unit_currency and unit_currency != iso:
                    raise ChartBuildError(f"Multiple currencies found in unit {unit!r}.")
                unit_currency = iso
            elif tok in _SCALE_WORD_MAP:
                mapped_scale = _SCALE_WORD_MAP[tok]
                if scale is not None and scale != mapped_scale:
                    raise ChartBuildError(f"Multiple scales found in unit {unit!r}.")
                scale = mapped_scale
            else:
                unmatched.append(tok)
            i += 1

    if not unmatched and (unit_currency is not None or scale is not None):
        if unit_currency is not None and norm_curr is not None and unit_currency != norm_curr:
            raise ChartBuildError(f"Conflicting currencies {unit_currency} vs {norm_curr}.")
        resolved_curr = norm_curr or unit_currency
        resolved_scale = scale or "ones"
        if resolved_curr is not None:
            canonical = f"{resolved_curr} {resolved_scale}" if resolved_scale != "ones" else resolved_curr
            return _UnitSpec(
                family="monetary",
                scale=resolved_scale,
                currency=resolved_curr,
                canonical_unit=canonical,
            )
        return _UnitSpec(
            family="monetary_unspecified",
            scale=resolved_scale,
            currency=None,
            canonical_unit=resolved_scale,
        )

    # Custom scalar unit (e.g. multiple, x, days): must match exact normalized family + currency.
    return _UnitSpec(
        family=f"custom:{lowered}",
        scale="ones",
        currency=norm_curr,
        canonical_unit=norm,
    )


def _scale_decimal(value: Decimal, source_scale: ChartAxisScale, target_scale: ChartAxisScale) -> Decimal:
    if source_scale == target_scale:
        return value
    diff = _SCALE_EXPONENT[source_scale] - _SCALE_EXPONENT[target_scale]
    with localcontext() as ctx:
        ctx.prec = 100
        return value.scaleb(diff)


def _format_display_value(value: Decimal, spec: _UnitSpec, axis_unit: str, target_scale: ChartAxisScale) -> str:
    plain = _format_decimal_plain(value)
    grouped = _format_grouped_number(plain)
    if spec.family == "percent":
        return f"{grouped}%"
    if spec.family == "percentage_points":
        return f"{grouped} pp"
    if spec.family == "bps":
        return f"{grouped} bps"
    if spec.family == "index_base_100":
        return grouped
    if spec.family == "monetary" and spec.currency:
        if target_scale == "ones":
            return f"{spec.currency} {grouped}"
        return f"{spec.currency} {grouped} {target_scale}"
    if spec.family == "monetary_unspecified":
        return f"{grouped} {target_scale}" if target_scale != "ones" else grouped
    if spec.family == "historical_price" and spec.currency and axis_unit == spec.currency:
        return f"{spec.currency} {grouped}"
    return f"{grouped} {axis_unit}"


def _pick_auto_scale(points: list[_RawPoint]) -> ChartAxisScale:
    scales = {pt.unit_spec.scale for pt in points}
    if len(scales) == 1:
        only_scale = next(iter(scales))
        if only_scale != "ones":
            return only_scale
        # Raw SEC filings / unscaled USD revenue: choose readable magnitude scale when all values are large.
        max_abs_ones = max((abs(pt.value) for pt in points), default=Decimal(0))
        if max_abs_ones >= Decimal("1000000000000"):
            return "trillions"
        if max_abs_ones >= Decimal("1000000000"):
            return "billions"
        if max_abs_ones >= Decimal("1000000"):
            return "millions"
        if max_abs_ones >= Decimal("10000"):
            return "thousands"
        return "ones"
    max_abs_ones = max(
        (abs(_scale_decimal(pt.value, pt.unit_spec.scale, "ones")) for pt in points),
        default=Decimal(0),
    )
    if max_abs_ones >= Decimal("1000000000000"):
        return "trillions"
    if max_abs_ones >= Decimal("1000000000"):
        return "billions"
    if max_abs_ones >= Decimal("1000000"):
        return "millions"
    if max_abs_ones >= Decimal("1000"):
        return "thousands"
    return "ones"


def _validate_source(
    source_id: str,
    sources: Mapping[str, ResearchSource],
    as_of: datetime | None,
    *,
    allow_simulated: bool,
) -> ResearchSource:
    source = sources.get(source_id)
    if source is None:
        raise ChartBuildError(f"Unknown source_id {source_id!r}.")
    if not source.permissions.display:
        raise ChartBuildError(f"Source {source_id!r} is not permitted for display.")
    check_target = (
        source.model_copy(update={"is_simulated": False})
        if (allow_simulated and source.is_simulated)
        else source
    )
    code = source_eligibility([check_target], as_of)[0][1]
    if code != "eligible":
        raise ChartBuildError(f"Source {source_id!r} is not eligible for chart rendering ({code}).")
    return source


def _parse_historical_prices_source(
    source: ResearchSource,
    *,
    normalize_base_100: bool,
) -> list[_RawPoint]:
    if not source.text.strip():
        raise ChartBuildError(f"Historical price source {source.source_id!r} is empty.")
    rows: list[tuple[str, Decimal, str, int, int]] = []
    bar_labels: set[str] = set()
    bar_intervals: set[str] = set()
    offset = 0
    prov_ccy = source.provenance.get("currency") if isinstance(source.provenance, dict) else None
    for raw_line in source.text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_start = offset
        line_end = offset + len(line)
        offset += len(raw_line)
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(" | ")]
        bar = _BAR_LINE_RE.match(parts[3]) if len(parts) == 4 else None
        if bar is not None and parts[2] in _BAR_INTERVALS and "@" in parts[0]:
            # market_data format: <SYMBOL>@<EXCHANGE> | <ts> | <interval> | O .. H .. L .. C .. V .. <currency>
            try:
                datetime.fromisoformat(parts[1])
            except ValueError as error:
                raise ChartBuildError(f"Invalid timestamp {parts[1]!r} in market data source.") from error
            for idx, name in ((1, "open"), (2, "high"), (3, "low"), (4, "close")):
                if _to_decimal(bar.group(idx), what=f"{source.source_id} {name}") <= 0:
                    raise ChartBuildError(f"Market data {name} must be positive in {source.source_id!r}.")
            if bar.group(5) != "-" and _to_decimal(bar.group(5), what=f"{source.source_id} volume") < 0:
                raise ChartBuildError(f"Market data volume must not be negative in {source.source_id!r}.")
            if bar.group(7) is not None and _to_decimal(bar.group(7), what=f"{source.source_id} adjustment factor") <= 0:
                raise ChartBuildError(f"Market data adjustment factor must be positive in {source.source_id!r}.")
            close = _to_decimal(bar.group(4), what=f"{source.source_id} close")
            bar_labels.add(parts[0].upper())
            bar_intervals.add(parts[2])
            if len(bar_labels) > 1 or len(bar_intervals) > 1:
                raise ChartBuildError(
                    f"Market data source {source.source_id!r} must hold a single instrument and interval to chart.")
            ccy = bar.group(6) or (prov_ccy.strip().upper() if isinstance(prov_ccy, str) and prov_ccy.strip() else None)
            if not ccy:
                raise ChartBuildError(f"Market data source {source.source_id!r} has no currency.")
            rows.append((parts[1], close, ccy, line_start, line_end))
            continue
        if len(parts) != 4 or parts[2].casefold() != "daily close":
            raise ChartBuildError(f"Source {source.source_id!r} is not in historical price line format.")
        try:
            session = date.fromisoformat(parts[1])
        except ValueError as error:
            raise ChartBuildError(f"Invalid session date {parts[1]!r} in historical price source.") from error
        match = _HIST_PRICE_LINE_RE.match(parts[3])
        if not match:
            raise ChartBuildError(f"Invalid price observation {parts[3]!r} in historical price source.")
        close = _to_decimal(match.group(1), what=f"{source.source_id} close")
        if close <= 0:
            raise ChartBuildError(f"Historical price close must be positive in {source.source_id!r}.")
        raw_unit = match.group(2).strip()
        rows.append((session.isoformat(), close, raw_unit, line_start, line_end))

    if not rows:
        raise ChartBuildError(f"Historical price source {source.source_id!r} contains no observations.")
    if len(rows) > _MAX_HISTORICAL_PRICE_POINTS:
        raise ChartBuildError(f"Historical price series exceeds max {_MAX_HISTORICAL_PRICE_POINTS} points.")

    rows.sort(key=lambda item: item[0])
    distinct_dates = [r[0] for r in rows]
    if len(distinct_dates) != len(set(distinct_dates)):
        raise ChartBuildError(f"Historical price source {source.source_id!r} contains duplicate session dates.")

    prov_currency = source.provenance.get("currency") if isinstance(source.provenance, dict) else None
    currency_hint = prov_currency.strip().upper() if isinstance(prov_currency, str) and prov_currency.strip() else None

    first_unit_spec = _classify_unit(
        rows[0][2],
        currency_hint,
        is_historical_price=True,
        normalized_base_100=normalize_base_100,
    )
    for _, _, raw_unit, _, _ in rows[1:]:
        row_spec = _classify_unit(
            raw_unit,
            currency_hint,
            is_historical_price=True,
            normalized_base_100=normalize_base_100,
        )
        if row_spec != first_unit_spec:
            raise ChartBuildError(f"Historical price source {source.source_id!r} has inconsistent units.")

    base_close = rows[0][1]
    raw_points: list[_RawPoint] = []
    for session, close, _, line_start, line_end in rows:
        if normalize_base_100:
            with localcontext() as ctx:
                ctx.prec = 100
                val = ((close / base_close) * Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        else:
            val = close
        raw_points.append(
            _RawPoint(
                x=session,
                value=val,
                unit_spec=first_unit_spec,
                provenance=ChartProvenance(
                    source_id=source.source_id,
                    character_start=line_start,
                    character_end=line_end,
                ),
                normalized_base_100=normalize_base_100,
            )
        )
    return raw_points


def _parse_sec_concept_source(
    source: ResearchSource,
    *,
    form_filter: Literal["10-K", "10-Q", "all"],
    max_points: int,
) -> list[_RawPoint]:
    if not source.text.strip():
        raise ChartBuildError(f"SEC concept source {source.source_id!r} is empty.")
    deduped: dict[tuple[str, str, str], tuple[str, str, dict[str, Any], Decimal, str, int, int]] = {}
    offset = 0
    for raw_line in source.text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_start = offset
        line_end = offset + len(line)
        offset += len(raw_line)
        stripped = line.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            continue
        try:
            obj = json.loads(stripped, parse_float=Decimal, parse_int=Decimal)
        except (ValueError, InvalidOperation):
            continue
        if not isinstance(obj, dict) or "val" not in obj or "end" not in obj or "unit" not in obj:
            continue
        row_form = str(obj.get("form") or "").strip()
        if form_filter != "all" and row_form != form_filter:
            continue
        end_raw = str(obj.get("end") or "").strip()
        start_raw = str(obj.get("start") or "").strip() if obj.get("start") is not None else ""
        try:
            date.fromisoformat(end_raw)
            if start_raw:
                date.fromisoformat(start_raw)
        except ValueError as error:
            raise ChartBuildError(f"Invalid SEC concept date in {source.source_id!r}.") from error
        val = _to_decimal(obj.get("val"), what=f"{source.source_id} SEC val")
        raw_unit = str(obj.get("unit") or "").strip()
        if not raw_unit:
            raise ChartBuildError(f"Missing SEC observation unit in {source.source_id!r}.")
        filed_raw = str(obj.get("filed") or "")
        accn_raw = str(obj.get("accn") or "")
        period_key = (start_raw, end_raw, raw_unit)
        existing = deduped.get(period_key)
        if existing is None or (filed_raw, accn_raw) > (existing[0], existing[1]):
            deduped[period_key] = (filed_raw, accn_raw, obj, val, raw_unit, line_start, line_end)

    if not deduped:
        raise ChartBuildError(
            f"SEC concept source {source.source_id!r} has no matching observations for form_filter={form_filter!r}."
        )

    ordered = sorted(
        deduped.values(),
        key=lambda row: (str(row[2].get("end") or ""), str(row[2].get("start") or ""), row[0]),
    )
    selected = ordered[-max_points:]

    base_labels: list[str] = []
    for _, _, obj, _, _, _, _ in selected:
        fy_raw = obj.get("fy")
        fp_raw = str(obj.get("fp") or "").strip()
        end_raw = str(obj.get("end") or "").strip()
        if fy_raw is not None and fp_raw:
            fy_txt = str(int(fy_raw)) if isinstance(fy_raw, Decimal) else str(fy_raw).strip()
            base_labels.append(f"FY{fy_txt}" if fp_raw == "FY" else f"FY{fy_txt} {fp_raw}")
        else:
            base_labels.append(end_raw)

    label_counts: dict[str, int] = {}
    for lbl in base_labels:
        label_counts[lbl] = label_counts.get(lbl, 0) + 1

    raw_points: list[_RawPoint] = []
    for base_label, (_, _, obj, val, raw_unit, line_start, line_end) in zip(base_labels, selected):
        end_raw = str(obj.get("end") or "").strip()
        start_raw = str(obj.get("start") or "").strip()
        if label_counts[base_label] > 1:
            x_label = f"{base_label} ({start_raw}~{end_raw})" if start_raw else f"{base_label} ({end_raw})"
        else:
            x_label = base_label
        unit_spec = _classify_unit(raw_unit)
        raw_points.append(
            _RawPoint(
                x=x_label,
                value=val,
                unit_spec=unit_spec,
                provenance=ChartProvenance(
                    source_id=source.source_id,
                    character_start=line_start,
                    character_end=line_end,
                ),
            )
        )
    return raw_points


def _fact_offsets(fact: Mapping[str, Any]) -> tuple[int | None, int | None]:
    c_start = fact.get("character_start")
    c_end = fact.get("character_end")
    loc = fact.get("location")
    if isinstance(loc, Mapping):
        if c_start is None:
            c_start = loc.get("character_start")
        if c_end is None:
            c_end = loc.get("character_end")
    return (
        c_start if type(c_start) is int and c_start >= 0 else None,
        c_end if type(c_end) is int and c_end > 0 else None,
    )


def _bind_facts_series(
    binding: FactsChartBinding,
    *,
    fact_by_id: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, ResearchSource],
    as_of: datetime | None,
    allow_simulated: bool,
) -> tuple[list[_RawPoint], dict[str, ResearchSource]]:
    resolved_facts: list[Mapping[str, Any]] = []
    used_sources: dict[str, ResearchSource] = {}
    for fid in binding.fact_ids:
        fact = fact_by_id.get(fid)
        if fact is None:
            raise ChartBuildError(f"Unknown fact_id {fid!r}.")
        sid = fact.get("source_id")
        if not isinstance(sid, str) or not sid:
            raise ChartBuildError(f"Fact {fid!r} has no source_id.")
        used_sources[sid] = _validate_source(sid, sources, as_of, allow_simulated=allow_simulated)
        resolved_facts.append(fact)

    periods = [str(f.get("period") or "").strip() for f in resolved_facts]
    all_periods_distinct = all(periods) and len(periods) == len(set(periods))
    labels_plus_period: list[str] = []
    for f, p in zip(resolved_facts, periods):
        lbl = str(f.get("label") or "").strip()
        labels_plus_period.append(p if all_periods_distinct else (lbl or p or str(f.get("fact_id"))))

    label_counts: dict[str, int] = {}
    for x_candidate in labels_plus_period:
        label_counts[x_candidate] = label_counts.get(x_candidate, 0) + 1

    raw_points: list[_RawPoint] = []
    for f, x_candidate, p in zip(resolved_facts, labels_plus_period, periods):
        x_val = x_candidate
        if label_counts[x_candidate] > 1 and p and p != x_candidate:
            x_val = f"{x_candidate} ({p})"
        val = _to_decimal(f.get("value"), what=f"fact {f.get('fact_id')}")
        spec = _classify_unit(str(f.get("unit") or ""), f.get("currency"))
        c_start, c_end = _fact_offsets(f)
        raw_points.append(
            _RawPoint(
                x=x_val,
                value=val,
                unit_spec=spec,
                provenance=ChartProvenance(
                    fact_id=str(f["fact_id"]),
                    source_id=str(f["source_id"]),
                    character_start=c_start,
                    character_end=c_end,
                ),
            )
        )
    return raw_points, used_sources


def _bind_calculations_series(
    binding: CalculationsChartBinding,
    *,
    calc_by_id: Mapping[str, Mapping[str, Any]],
    fact_by_id: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, ResearchSource],
    as_of: datetime | None,
    allow_simulated: bool,
) -> tuple[list[_RawPoint], dict[str, ResearchSource]]:
    resolved: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    used_sources: dict[str, ResearchSource] = {}
    for cid in binding.calculation_ids:
        calc = calc_by_id.get(cid)
        if calc is None:
            raise ChartBuildError(f"Unknown calculation_id {cid!r}.")
        inputs = calc.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise ChartBuildError(f"Calculation {cid!r} has no verifiable inputs.")
        primary_fact: Mapping[str, Any] | None = None
        for idx, inp in enumerate(inputs):
            if not isinstance(inp, Mapping) or not isinstance(inp.get("fact_id"), str):
                raise ChartBuildError(f"Calculation {cid!r} has an invalid input entry.")
            fact = fact_by_id.get(inp["fact_id"])
            if fact is None:
                raise ChartBuildError(f"Calculation {cid!r} references missing fact_id {inp['fact_id']!r}.")
            sid = fact.get("source_id")
            if not isinstance(sid, str) or not sid:
                raise ChartBuildError(f"Input fact {inp['fact_id']!r} has no source_id.")
            used_sources[sid] = _validate_source(sid, sources, as_of, allow_simulated=allow_simulated)
            if idx == 0:
                primary_fact = fact
        assert primary_fact is not None
        resolved.append((calc, primary_fact))

    periods = [str(primary.get("period") or "").strip() for _, primary in resolved]
    all_periods_distinct = all(periods) and len(periods) == len(set(periods))

    x_candidates: list[str] = []
    for (calc, primary), p in zip(resolved, periods):
        if all_periods_distinct:
            x_candidates.append(p)
        else:
            fid = str(calc.get("formula_id") or "").strip()
            lbl = str(primary.get("label") or "").strip()
            base = lbl or fid or str(calc.get("calculation_id"))
            x_candidates.append(f"{base} ({p})" if p else base)

    counts: dict[str, int] = {}
    for item in x_candidates:
        counts[item] = counts.get(item, 0) + 1

    raw_points: list[_RawPoint] = []
    for (calc, primary), x_candidate in zip(resolved, x_candidates):
        x_val = f"{x_candidate} [{calc['calculation_id'][:10]}]" if counts[x_candidate] > 1 else x_candidate
        val = _to_decimal(calc.get("value"), what=f"calculation {calc.get('calculation_id')}")
        spec = _classify_unit(str(calc.get("unit") or ""), calc.get("currency"))
        c_start, c_end = _fact_offsets(primary)
        raw_points.append(
            _RawPoint(
                x=x_val,
                value=val,
                unit_spec=spec,
                provenance=ChartProvenance(
                    calculation_id=str(calc["calculation_id"]),
                    source_id=str(primary["source_id"]),
                    character_start=c_start,
                    character_end=c_end,
                ),
            )
        )
    return raw_points, used_sources


def _claim_source_ids(claim: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    citations = claim.get("citations")
    if isinstance(citations, list):
        for cit in citations:
            if isinstance(cit, Mapping) and isinstance(cit.get("source_id"), str) and cit["source_id"]:
                found.append(cit["source_id"])
    raw_source_ids = claim.get("source_ids")
    if isinstance(raw_source_ids, list):
        for sid in raw_source_ids:
            if isinstance(sid, str) and sid:
                found.append(sid)
    top_sid = claim.get("source_id")
    if isinstance(top_sid, str) and top_sid:
        found.append(top_sid)
    return list(dict.fromkeys(found))


def _build_causal_graph_chart(
    request: CreateChartRequest,
    *,
    claims: list[Mapping[str, Any]] | None,
    sources: Mapping[str, ResearchSource],
    as_of: datetime | None,
    allow_simulated: bool,
) -> ResearchChart:
    spec = request.causal_graph
    assert spec is not None
    claim_by_id: dict[str, Mapping[str, Any]] = {}
    for row in claims or ():
        if isinstance(row, Mapping) and isinstance(row.get("claim_id"), str) and row["claim_id"]:
            claim_by_id[row["claim_id"]] = row

    all_used_sources: dict[str, ResearchSource] = {}
    built_edges: list[CausalGraphEdge] = []
    for edge in spec.edges:
        for cid in edge.claim_ids:
            claim = claim_by_id.get(cid)
            if claim is None:
                raise ChartBuildError(f"Unknown claim_id {cid!r} in causal_graph edge {edge.from_id!r} -> {edge.to_id!r}.")
            for sid in _claim_source_ids(claim):
                all_used_sources[sid] = _validate_source(sid, sources, as_of, allow_simulated=allow_simulated)
        unverified = True if not edge.claim_ids else bool(edge.unverified_assumption)
        built_edges.append(edge.model_copy(update={"unverified_assumption": unverified}))

    verified_connected_nodes: set[str] = set()
    for edge in built_edges:
        if edge.claim_ids and not edge.unverified_assumption:
            verified_connected_nodes.add(edge.from_id)
            verified_connected_nodes.add(edge.to_id)

    built_nodes: list[CausalGraphNode] = []
    for node in spec.nodes:
        status = node.status
        if status == "confirmed" and node.id not in verified_connected_nodes:
            status = "assumption"
        built_nodes.append(node.model_copy(update={"status": status}))

    is_simulated = any(src.is_simulated for src in all_used_sources.values())
    has_projected_points = any(n.status == "assumption" for n in built_nodes) or any(
        e.unverified_assumption for e in built_edges
    )

    subtitle_parts: list[str] = [f"因果传导图（{len(built_nodes)} 节点 / {len(built_edges)} 条边）"]
    subtitle_parts.append("含未核实假设或推演传导" if has_projected_points else "边已绑定研究引证")
    if as_of is not None:
        subtitle_parts.append(f"截至 {as_of.date().isoformat()}")
    if is_simulated:
        subtitle_parts.append("【模拟数据】")

    return ResearchChart(
        chart_id="chart-" + uuid4().hex,
        chart_type="causal_graph",
        title=request.title,
        subtitle=" · ".join(subtitle_parts),
        x_label=request.x_label,
        axes=[],
        series=[],
        causal_graph=CausalGraphSpec(nodes=built_nodes, edges=built_edges),
        is_simulated=is_simulated,
        has_projected_points=has_projected_points,
        replaces_chart_id=request.replaces_chart_id,
        source_ids=sorted(all_used_sources),
    )


def build_research_chart(
    request: CreateChartRequest,
    *,
    facts: list[Mapping[str, Any]],
    calculations: list[Mapping[str, Any]],
    sources: Mapping[str, ResearchSource],
    claims: list[Mapping[str, Any]] | None = None,
    existing_charts: list[Mapping[str, Any]] | None = None,
    as_of: datetime | None = None,
    allow_simulated: bool = False,
) -> ResearchChart:
    """Validate every citation/id/unit/permission rule and deterministically build a ResearchChart."""
    if request.replaces_chart_id is not None:
        known_chart_ids = {
            item.get("chart_id")
            for item in (existing_charts or [])
            if isinstance(item, Mapping) and isinstance(item.get("chart_id"), str)
        }
        if request.replaces_chart_id not in known_chart_ids:
            raise ChartBuildError(f"Unknown replaces_chart_id {request.replaces_chart_id!r}.")

    if request.chart_type == "causal_graph":
        return _build_causal_graph_chart(
            request,
            claims=claims,
            sources=sources,
            as_of=as_of,
            allow_simulated=allow_simulated,
        )

    fact_by_id: dict[str, Mapping[str, Any]] = {}
    for row in facts:
        if isinstance(row, Mapping) and isinstance(row.get("fact_id"), str):
            fact_by_id[row["fact_id"]] = row

    calc_by_id: dict[str, Mapping[str, Any]] = {}
    for row in calculations:
        if isinstance(row, Mapping) and isinstance(row.get("calculation_id"), str):
            calc_by_id[row["calculation_id"]] = row

    all_used_sources: dict[str, ResearchSource] = {}
    series_raw: list[tuple[CreateChartSeries, list[_RawPoint]]] = []

    for s_req in request.series:
        binding = s_req.binding
        if isinstance(binding, FactsChartBinding):
            pts, used = _bind_facts_series(
                binding,
                fact_by_id=fact_by_id,
                sources=sources,
                as_of=as_of,
                allow_simulated=allow_simulated,
            )
        elif isinstance(binding, CalculationsChartBinding):
            pts, used = _bind_calculations_series(
                binding,
                calc_by_id=calc_by_id,
                fact_by_id=fact_by_id,
                sources=sources,
                as_of=as_of,
                allow_simulated=allow_simulated,
            )
        elif isinstance(binding, HistoricalPricesChartBinding):
            src = _validate_source(binding.source_id, sources, as_of, allow_simulated=allow_simulated)
            pts = _parse_historical_prices_source(src, normalize_base_100=False)
            used = {src.source_id: src}
        else:
            src = _validate_source(binding.source_id, sources, as_of, allow_simulated=allow_simulated)
            pts = _parse_sec_concept_source(
                src,
                form_filter=binding.form_filter,
                max_points=binding.max_points,
            )
            used = {src.source_id: src}

        if not pts:
            raise ChartBuildError(f"Series {s_req.name!r} yielded no observations.")
        all_used_sources.update(used)
        series_raw.append((s_req, pts))

    # Compare normalized series against one shared observation, never independently
    # rebasing each instrument on a different starting day.
    normalized = [(s, points) for s, points in series_raw
                  if isinstance(s.binding, HistoricalPricesChartBinding) and s.binding.normalize_base_100]
    historical = [(s, points) for s, points in series_raw if isinstance(s.binding, HistoricalPricesChartBinding)]
    if normalized and len(normalized) != len(historical):
        raise ChartBuildError("Historical price comparison cannot mix normalized and raw prices.")
    normalization_base = None
    if normalized:
        def timeline_key(value: str) -> str:
            if len(value) == 10:
                return date.fromisoformat(value).isoformat()
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ChartBuildError("Intraday comparison requires timezone-aware timestamps.")
            return stamp.astimezone(timezone.utc).isoformat()
        keyed = []
        for _, points in normalized:
            values = {timeline_key(point.x): point for point in points}
            if len(values) != len(points):
                raise ChartBuildError("Price series contains duplicate observation times.")
            keyed.append(values)
        common = set(keyed[0]).intersection(*(set(item) for item in keyed[1:]))
        if not common:
            raise ChartBuildError("Normalized price series have no common observation time.")
        normalization_base = min(common)
        index_spec = _classify_unit("index", None, is_historical_price=True, normalized_base_100=True)
        for (_, points), mapping in zip(normalized, keyed):
            base = mapping[normalization_base].value
            points[:] = [point for point in points if timeline_key(point.x) >= normalization_base]
            for point in points:
                point.x = timeline_key(point.x)
                with localcontext() as decimal_context:
                    decimal_context.prec = 100
                    point.value = (point.value / base * Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                point.unit_spec = index_spec
                point.normalized_base_100 = True

    # Validate each declared Y axis across all points bound to it and resolve target scale & unit.
    axis_meta: dict[ChartAxisId, tuple[ChartAxis, _UnitSpec, bool]] = {}
    for ax_req in request.y_axes:
        ax_points = [pt for s_req, pts in series_raw if s_req.y_axis_id == ax_req.id for pt in pts]
        families = {pt.unit_spec.family for pt in ax_points}
        if len(families) != 1:
            raise ChartBuildError(
                f"Axis {ax_req.id!r} cannot combine incompatible unit families: {', '.join(sorted(families))}."
            )
        family = next(iter(families))

        currencies = {pt.unit_spec.currency for pt in ax_points}
        if len(currencies) != 1:
            raise ChartBuildError(f"Axis {ax_req.id!r} mixes inconsistent currencies.")
        resolved_currency = next(iter(currencies))

        req_curr = ax_req.currency.strip().upper() if ax_req.currency else None
        if req_curr is not None:
            if resolved_currency is None:
                raise ChartBuildError(f"Axis {ax_req.id!r} declares currency {req_curr!r} for non-monetary data.")
            if req_curr != resolved_currency:
                raise ChartBuildError(
                    f"Axis {ax_req.id!r} currency {req_curr!r} does not match bound data currency {resolved_currency!r}."
                )

        if family in ("monetary", "monetary_unspecified"):
            raw_scales = {pt.unit_spec.scale for pt in ax_points}
            if ax_req.unit_preference is not None:
                pref_spec = _classify_unit(ax_req.unit_preference)
                if pref_spec.family not in ("monetary", "monetary_unspecified"):
                    raise ChartBuildError(
                        f"unit_preference {ax_req.unit_preference!r} is incompatible with {family} axis {ax_req.id!r}."
                    )
                if pref_spec.currency is not None and resolved_currency is not None and pref_spec.currency != resolved_currency:
                    raise ChartBuildError(
                        f"unit_preference currency {pref_spec.currency!r} conflicts with {resolved_currency!r}."
                    )
                if pref_spec.currency is not None and resolved_currency is None:
                    raise ChartBuildError(
                        f"unit_preference currency {pref_spec.currency!r} cannot be applied to currency-unspecified data."
                    )
                target_scale = pref_spec.scale
            else:
                target_scale = _pick_auto_scale(ax_points)
            converted = raw_scales != {target_scale}
            axis_unit = (
                f"{resolved_currency} {target_scale}" if resolved_currency and target_scale != "ones"
                else (resolved_currency or target_scale)
            )
        else:
            target_scale = "ones"
            converted = False
            canonical_units = {pt.unit_spec.canonical_unit for pt in ax_points}
            if len(canonical_units) != 1:
                raise ChartBuildError(f"Axis {ax_req.id!r} mixes units: {', '.join(sorted(canonical_units))}.")
            axis_unit = next(iter(canonical_units))
            if ax_req.unit_preference is not None:
                pref_lower = ax_req.unit_preference.casefold()
                pref_spec = _classify_unit(
                    ax_req.unit_preference,
                    resolved_currency,
                    is_historical_price=(family == "historical_price"),
                    normalized_base_100=(family == "index_base_100" and ("index" in pref_lower or "100" in pref_lower)),
                )
                if pref_spec.family != family or pref_spec.currency != resolved_currency:
                    raise ChartBuildError(
                        f"unit_preference {ax_req.unit_preference!r} is incompatible with axis {ax_req.id!r} ({axis_unit!r})."
                    )

        axis_obj = ChartAxis(
            id=ax_req.id,
            label=(f"{ax_req.label}（{normalization_base}=100）" if family == "index_base_100" and normalization_base else ax_req.label),
            unit=axis_unit,
            currency=resolved_currency,
            scale=target_scale,
        )
        rep_spec = _UnitSpec(
            family=family,
            scale=target_scale,
            currency=resolved_currency,
            canonical_unit=axis_unit,
        )
        axis_meta[ax_req.id] = (axis_obj, rep_spec, converted)

    built_series: list[ChartSeries] = []
    for s_req, raw_pts in series_raw:
        axis_obj, rep_spec, _ = axis_meta[s_req.y_axis_id]
        points: list[ChartPoint] = []
        for pt in raw_pts:
            scaled_val = _scale_decimal(pt.value, pt.unit_spec.scale, axis_obj.scale)
            y_str = _format_decimal_plain(scaled_val)
            disp = _format_display_value(scaled_val, rep_spec, axis_obj.unit, axis_obj.scale)
            points.append(
                ChartPoint(
                    x=pt.x,
                    y=y_str,
                    display_value=disp,
                    is_projected=pt.is_projected,
                    provenance=pt.provenance,
                )
            )
        built_series.append(
            ChartSeries(
                name=s_req.name,
                mark=s_req.mark,
                y_axis_id=s_req.y_axis_id,
                is_projected=any(p.is_projected for p in points),
                points=points,
            )
        )

    is_simulated = any(src.is_simulated for src in all_used_sources.values())
    has_projected = any(p.is_projected for s in built_series for p in s.points)

    subtitle_parts: list[str] = []
    for ax_req in request.y_axes:
        ax_obj, spec, converted = axis_meta[ax_req.id]
        prefix = "左轴" if len(request.y_axes) > 1 and ax_obj.id == "left" else ("右轴" if len(request.y_axes) > 1 else "单位")
        note = ""
        if converted:
            note = f"（已统一换算至 {ax_obj.scale}）"
        elif spec.family == "index_base_100":
            note = f"（{normalization_base} 基准=100）"
        elif ax_obj.scale != "ones":
            note = f"（量级：{ax_obj.scale}）"
        subtitle_parts.append(f"{prefix}：{ax_obj.unit}{note}")
    if as_of is not None:
        subtitle_parts.append(f"截至 {as_of.date().isoformat()}")
    if is_simulated:
        subtitle_parts.append("【模拟数据】")

    ordered_axes = [axis_meta[ax.id][0] for ax in request.y_axes]
    return ResearchChart(
        chart_id="chart-" + uuid4().hex,
        chart_type=request.chart_type,
        title=(f"{request.title}（基准 {normalization_base}）" if normalization_base else request.title),
        subtitle=" · ".join(subtitle_parts),
        x_label=request.x_label,
        axes=ordered_axes,
        series=built_series,
        is_simulated=is_simulated,
        has_projected_points=has_projected,
        replaces_chart_id=request.replaces_chart_id,
        source_ids=sorted(all_used_sources),
        normalization_base=normalization_base,
    )
