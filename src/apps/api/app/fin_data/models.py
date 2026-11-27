"""Standard financial data models and the W6 tool result envelope.

Provider specific shapes never leave the adapters; everything the research
runtime sees is one of these models. Money and prices are ``Decimal`` and are
serialised as JSON strings so no value passes through binary floating point.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..research_clock import EXCHANGES

ToolStatus = Literal["ok", "partial", "empty", "failed", "denied", "rate_limited", "unknown"]
Interval = Literal["1min", "5min", "15min", "30min", "1h", "1day", "1week", "1month"]
INTERVALS: tuple[str, ...] = ("1min", "5min", "15min", "30min", "1h", "1day", "1week", "1month")
INTRADAY_INTERVALS = frozenset({"1min", "5min", "15min", "30min", "1h"})

_EXCHANGE_ALIASES = {
    "NASDAQ": "NASDAQ", "NMS": "NASDAQ", "XNAS": "NASDAQ", "XNGS": "NASDAQ", "XNMS": "NASDAQ", "XNCM": "NASDAQ",
    "NYSE": "NYSE", "XNYS": "NYSE",
    "TSX": "TSX", "XTSE": "TSX", "LSE": "LSE", "XLON": "LSE",
    "XETRA": "XETRA", "XETR": "XETRA", "EURONEXT": "EURONEXT", "XPAR": "EURONEXT", "XAMS": "EURONEXT",
    "SSE": "SSE", "XSHG": "SSE", "SZSE": "SZSE", "XSHE": "SZSE", "XBSE": "BJSE", "BJSE": "BJSE",
    "HKEX": "HKEX", "XHKG": "HKEX", "SEHK": "HKEX",
    "TSE": "TSE", "XTKS": "TSE", "JPX": "TSE", "KRX": "KRX", "XKRX": "KRX",
    "TWSE": "TWSE", "XTAI": "TWSE", "NSE": "NSE", "XNSE": "NSE", "ASX": "ASX", "XASX": "ASX",
}


def normalize_exchange(raw: str | None) -> str | None:
    """Map a provider exchange name or MIC onto an ``EXCHANGES`` key, else None."""
    if not isinstance(raw, str):
        return None
    code = _EXCHANGE_ALIASES.get(raw.strip().upper())
    return code if code in EXCHANGES else None


_ASSET_TYPES = {"common stock": "equity", "depositary receipt": "equity", "preferred stock": "equity",
                "reit": "equity", "etf": "etf", "index": "index", "mutual fund": "fund",
                "digital currency": "crypto", "physical currency": "fx"}


def normalize_asset_type(raw: str | None) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    return _ASSET_TYPES.get(raw.strip().lower(), "other")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Permissions(_Frozen):
    store: bool = False
    display: bool = False
    model_input: bool = False
    export: bool = False
    # Extension to W6: True when the data was fetched with a user's own key and
    # may only be cached/shown inside that owner's account (never shared).
    owner_scoped: bool = False

    @classmethod
    def none(cls) -> "Permissions":
        return cls()

    def narrow(self, other: "Permissions") -> "Permissions":
        """Intersection: the result never grants more than either side."""
        return Permissions(store=self.store and other.store, display=self.display and other.display,
                           model_input=self.model_input and other.model_input, export=self.export and other.export,
                           owner_scoped=self.owner_scoped or other.owner_scoped)


class EntityRef(_Frozen):
    """A listed security (with its issuer). ``entity_id`` is ``EXCHANGE:SYMBOL`` or ``cik:##########``."""
    entity_id: str
    company_name: str | None = None
    symbol: str | None = None
    exchange: str | None = None
    currency: str | None = None
    asset_type: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    listing_date: date | None = None

    @field_validator("exchange")
    @classmethod
    def known_exchange(cls, value: str | None) -> str | None:
        if value is not None and value not in EXCHANGES:
            raise ValueError("exchange must be a research_clock.EXCHANGES key; keep raw names in identifiers")
        return value


Security = EntityRef


class Bar(_Frozen):
    entity_id: str
    interval: Interval
    ts_start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    # Provider's cumulative adjustment factor on this day (A-share sources); prices stay as traded.
    adj_factor: Decimal | None = None
    adjustment: str = "none"
    session: str | None = None
    is_final: bool = True

    @field_validator("ts_start")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ts_start must be timezone-aware")
        return value


class StatementLine(_Frozen):
    entity_id: str
    statement: Literal["income", "balance", "cashflow", "footnote", "regulatory", "credit", "equity"]
    line: str
    reported_tag: str
    taxonomy: str
    period_start: date | None = None
    period_end: date
    fiscal_label: str | None = None
    value: Decimal
    unit: str
    currency: str | None = None
    form: str | None = None
    filed_at: date | None = None
    accession: str | None = None
    frame: str | None = None
    derived: bool = False
    derivation: str | None = None
    # Non-SEC sources: reporting basis ("ytd_cumulative", "single_quarter_derived", "point_in_time"), accounting
    # standard, and restatements: the latest revision wins; earlier values are kept as "<filed>:<value>".
    basis: str | None = None
    accounting_standard: str | None = None
    revision_count: int = 0
    superseded: list[str] = Field(default_factory=list)


class Filing(_Frozen):
    entity_id: str
    form: str
    filed_at: date
    accepted_at: datetime | None = None
    period_of_report: date | None = None
    accession: str
    primary_document_url: str | None = None
    index_url: str | None = None
    description: str | None = None


class FrameRow(_Frozen):
    entity_id: str
    cik: str
    entity_name: str | None = None
    period_end: date
    value: Decimal
    accession: str | None = None
    location: str | None = None


class FullTextHit(_Frozen):
    entity_id: str | None = None
    ciks: list[str] = Field(default_factory=list)
    display_name: str | None = None
    form: str | None = None
    filed_at: date | None = None
    period_of_report: date | None = None
    accession: str | None = None
    document: str | None = None
    document_type: str | None = None
    description: str | None = None
    url: str | None = None


class Coverage(_Frozen):
    entity_id: str | None = None
    requested: dict[str, str | None] = Field(default_factory=dict)
    actual: dict[str, str | None] = Field(default_factory=dict)
    rows: int = 0
    data_as_of: date | datetime | None = None
    currency: str | None = None
    session_status: str | None = None
    is_final: bool | None = None
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)


class SuggestedAction(_Frozen):
    action: str
    hint: str | None = None
    retry_after_seconds: float | None = None


class Provenance(_Frozen):
    provider: str
    endpoint: str
    retrieved_at: datetime
    response_sha256: str | None = None
    endpoints: list[str] = Field(default_factory=list)


class Cost(_Frozen):
    provider_calls: int = 0
    credits: int | None = None


class ToolResult(_Frozen):
    """W6 envelope. ``data`` carries the records (an extension; W6 leaves payload placement open)."""
    status: ToolStatus
    reason: str
    message: str = ""
    retryable: bool = False
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    coverage: Coverage | None = None
    permissions: Permissions = Field(default_factory=Permissions.none)
    provenance: Provenance | None = None
    source_ids: list[str] = Field(default_factory=list)
    cost: Cost = Field(default_factory=Cost)
    data: list[Any] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self) -> "ToolResult":
        if self.status in ("empty", "failed", "denied", "rate_limited", "unknown") and self.data:
            raise ValueError(f"status {self.status} must not carry data")
        if self.status == "rate_limited" and not self.retryable:
            raise ValueError("rate_limited results must be retryable")
        if self.status == "denied" and self.permissions != Permissions.none():
            raise ValueError("denied results carry no permissions")
        return self

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def failure(cls, status: ToolStatus, reason: str, message: str = "", *, retryable: bool = False,
                suggested_actions: list[SuggestedAction] | None = None, provenance: Provenance | None = None,
                cost: Cost | None = None, coverage: Coverage | None = None) -> "ToolResult":
        return cls(status=status, reason=reason, message=message, retryable=retryable,
                   suggested_actions=suggested_actions or [], provenance=provenance,
                   cost=cost or Cost(), coverage=coverage)
