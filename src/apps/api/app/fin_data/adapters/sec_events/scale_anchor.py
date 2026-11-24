"""SEC-native XBRL ScaleAnchor builder with strict ``filed_at <= as_of`` truncation.

Strictly ``owner_scoped=False``: consumes ONLY public SEC EDGAR XBRL ``companyconcept`` endpoints
(``us-gaap`` -> ``ifrs-full`` fallback + ``dei`` cover facts). Never touches BYOK price feeds.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from threading import Event
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict

from ....research_signals import ScaleAnchor
from ...policy import FinDataPolicy
from ..base import StreamTransport, Transport
from ..sec_edgar import STANDARD_LINES, Period, _collect, _currency, _d, _entries, _pick_unit, cik10
from .pacing import PacingTelemetry, SecPacingError, SecPacingLimiter, sec_get_json

CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
DEI_PUBLIC_FLOAT_TAG = "EntityPublicFloat"
DEI_SHARES_OUTSTANDING_TAG = "EntityCommonStockSharesOutstanding"
_SCALE_LINES = ("revenue", "net_income", "total_assets")


class MaterialityCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["ok", "currency_mismatch", "missing_anchor", "unverified_adr_ratio"]
    ratio: str | None = None
    anchor_metric: str | None = None
    anchor_value: str | None = None
    anchor_currency: str | None = None
    event_currency: str | None = None
    note: str | None = None


def _filter_entries_as_of(raw_entries: Any, as_of_filed_date: date | None) -> list[dict[str, Any]]:
    if not isinstance(raw_entries, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        filed = _d(item.get("filed"))
        if as_of_filed_date is not None:
            if filed is None or filed > as_of_filed_date:
                continue
        out.append(item)
    return out


def _truncate_facts_as_of(facts: Mapping[str, Any], as_of_filed_date: date | None) -> dict[str, Any]:
    """Return a shallow-copied ``facts`` tree retaining only entries with ``filed <= as_of_filed_date``."""
    out: dict[str, Any] = {}
    for taxonomy, tax_node in facts.items():
        if not isinstance(tax_node, Mapping):
            continue
        tax_copy: dict[str, Any] = {}
        for tag, tag_node in tax_node.items():
            if not isinstance(tag_node, Mapping):
                continue
            units = tag_node.get("units")
            if not isinstance(units, Mapping):
                continue
            units_copy: dict[str, list[dict[str, Any]]] = {}
            for unit_name, unit_entries in units.items():
                filtered = _filter_entries_as_of(unit_entries, as_of_filed_date)
                if filtered:
                    units_copy[str(unit_name)] = filtered
            if units_copy:
                tax_copy[str(tag)] = {**tag_node, "units": units_copy}
        if tax_copy:
            out[str(taxonomy)] = tax_copy
    return out


def _fetch_concept_nodes(
    cik: str,
    taxonomy: str,
    tags: tuple[str, ...],
    *,
    user_agent: str | None,
    limiter: SecPacingLimiter | None,
    policy: FinDataPolicy | None,
    transport: Transport | None,
    stream_transport: StreamTransport | None,
    cancel_event: Event | None,
    deadline: float | None,
    telemetry: PacingTelemetry | None,
    gaps: list[str],
) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    for tag in tags:
        url = CONCEPT_URL.format(cik=cik, taxonomy=taxonomy, tag=tag)
        try:
            doc, _sha = sec_get_json(
                url,
                category="financials",
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                stream_transport=stream_transport,
                parse_float=Decimal,
                cancel_event=cancel_event,
                deadline=deadline,
                telemetry=telemetry,
            )
        except SecPacingError as err:
            if err.reason == "not_found":
                continue
            gaps.append(f"xbrl_fetch_failed:{taxonomy}:{tag}:{err.reason}")
            if err.reason in ("cancelled", "deadline_exceeded", "forbidden", "sec_contact_unconfigured"):
                break
            continue
        if isinstance(doc, Mapping) and isinstance(doc.get("units"), Mapping):
            loaded[tag] = dict(doc)
    return loaded


def _pick_monetary_unit(units: Mapping[str, Any]) -> str | None:
    return _pick_unit(dict(units), "money")


def _select_ttm_from_quarters(quarters: list[Period]) -> Decimal | None:
    if len(quarters) < 4:
        return None
    ordered = sorted(quarters, key=lambda q: q.end, reverse=True)
    for i in range(len(ordered) - 3):
        win = ordered[i : i + 4]
        # win is newest-first: win[0] newest, win[3] oldest
        if any(q.start is None for q in win):
            continue
        contiguous = all(
            abs(((win[j].start - win[j + 1].end).days) - 1) <= 5  # type: ignore[operator]
            for j in range(3)
        )
        total_days = (win[0].end - win[3].start).days  # type: ignore[operator]
        if contiguous and 340 <= total_days <= 385:
            return sum((q.value for q in win), Decimal(0))
    return None


def _select_semiannual_ttm(entries: list[dict[str, Any]]) -> Decimal | None:
    """Handle IFRS FPI semi-annual reporters: TTM = cur_H1 + prev_FY - prev_H1."""
    groups: dict[tuple[date | None, date], list[dict[str, Any]]] = {}
    for e in _entries(entries):
        groups.setdefault((e["_start"], e["_end"]), []).append(e)
    halves: list[tuple[date, date, Decimal, date]] = []
    years: list[tuple[date, date, Decimal, date]] = []
    for (start, end), grp in groups.items():
        if start is None:
            continue
        days = (end - start).days
        latest = max(grp, key=lambda x: (x["_filed"], str(x.get("accn", ""))))
        if 160 <= days <= 205:
            halves.append((start, end, latest["_val"], latest["_filed"]))
        elif 340 <= days <= 380:
            years.append((start, end, latest["_val"], latest["_filed"]))
    if not halves or not years:
        return None
    halves.sort(key=lambda x: x[1], reverse=True)
    years.sort(key=lambda x: x[1], reverse=True)
    cur_h1 = halves[0]
    fy = next((y for y in years if y[1] < cur_h1[1] and abs((cur_h1[0] - y[1]).days - 1) <= 10), None)
    if fy is None:
        return None
    prev_h1 = next(
        (h for h in halves[1:] if abs((cur_h1[1] - h[1]).days - 365) <= 20 and abs((h[0] - fy[0]).days) <= 10),
        None,
    )
    if prev_h1 is None:
        return None
    return cur_h1[2] + fy[2] - prev_h1[2]


def _extract_flow_metric(
    tax_facts: Mapping[str, Any],
    line_name: str,
    *,
    preferred_currency: str | None = None,
) -> tuple[Decimal | None, str | None, str | None]:
    """Extract TTM (else semi-annual TTM, else latest FY) flow metric and currency."""
    spec = STANDARD_LINES[line_name]
    # Collect candidate entries across tags for each unit, keeping latest filing per (start, end)
    by_unit_periods: dict[str, dict[tuple[date | None, date], tuple[Period, dict[str, Any]]]] = {}
    raw_by_unit: dict[str, list[dict[str, Any]]] = {}

    for tag in spec.tags.get("us-gaap", ()) + spec.tags.get("ifrs-full", ()):
        node = tax_facts.get(tag)
        units = node.get("units") if isinstance(node, Mapping) else None
        if not isinstance(units, Mapping):
            continue
        unit = _pick_monetary_unit(units)
        if unit is None:
            continue
        raw_list = units.get(unit)
        if not isinstance(raw_list, list) or not raw_list:
            continue
        raw_by_unit.setdefault(unit, []).extend(raw_list)
        q_periods = _collect(raw_list, "quarter", flow=True, derive_q4=True, tag=tag, unit=unit)
        y_periods = _collect(raw_list, "year", flow=True, derive_q4=False, tag=tag, unit=unit)
        bucket = by_unit_periods.setdefault(unit, {})
        for p in [*q_periods, *y_periods]:
            key = (p.start, p.end)
            existing = bucket.get(key)
            if existing is None or (p.filed or date.min) > (existing[0].filed or date.min):
                bucket[key] = (p, {"kind": p.kind})

    if not by_unit_periods:
        return None, None, "missing"

    if preferred_currency and preferred_currency in by_unit_periods:
        chosen_unit = preferred_currency
    elif "USD" in by_unit_periods:
        chosen_unit = "USD"
    else:
        chosen_unit = sorted(by_unit_periods)[0]

    periods = [p for p, _meta in by_unit_periods[chosen_unit].values()]
    quarters = [p for p in periods if p.kind == "quarter"]
    ttm = _select_ttm_from_quarters(quarters)
    cur = _currency(chosen_unit)
    if ttm is not None:
        return ttm, cur, "ttm_4q"

    semi_ttm = _select_semiannual_ttm(raw_by_unit.get(chosen_unit, []))
    if semi_ttm is not None:
        return semi_ttm, cur, "ttm_semiannual"

    years = sorted((p for p in periods if p.kind == "year"), key=lambda p: (p.end, p.filed or date.min), reverse=True)
    if years:
        return years[0].value, cur, "latest_fy"

    if quarters:
        latest_q = sorted(quarters, key=lambda p: (p.end, p.filed or date.min), reverse=True)[0]
        return latest_q.value, cur, "latest_quarter_only"

    return None, cur, "missing"


def _extract_instant_metric(
    tax_facts: Mapping[str, Any],
    tags: tuple[str, ...],
    *,
    preferred_currency: str | None = None,
) -> tuple[Decimal | None, str | None, date | None]:
    candidates: list[tuple[date, date, str, str, Decimal]] = []
    for tag in tags:
        node = tax_facts.get(tag)
        units = node.get("units") if isinstance(node, Mapping) else None
        if not isinstance(units, Mapping):
            continue
        unit = preferred_currency if (preferred_currency and preferred_currency in units and units[preferred_currency]) else _pick_monetary_unit(units)
        if unit is None:
            continue
        for entry in _entries(units.get(unit, [])):
            if entry["_start"] is not None:
                continue
            candidates.append((entry["_end"], entry["_filed"], str(entry.get("accn") or ""), unit, entry["_val"]))
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    end_d, filed_d, _accn, unit, val = candidates[0]
    return val, _currency(unit), end_d or filed_d


def _extract_dei_latest(
    dei_node: Mapping[str, Any] | None,
    *,
    preferred_units: tuple[str, ...],
    require_currency: str | None = None,
) -> tuple[Decimal | None, date | None, str | None]:
    if not isinstance(dei_node, Mapping):
        return None, None, None
    units = dei_node.get("units")
    if not isinstance(units, Mapping) or not units:
        return None, None, None

    chosen_unit: str | None = None
    for cand in preferred_units:
        if cand in units and isinstance(units[cand], list) and units[cand]:
            chosen_unit = cand
            break
    if chosen_unit is None:
        non_empty = [str(u) for u, rows in units.items() if isinstance(rows, list) and rows]
        if not non_empty:
            return None, None, None
        chosen_unit = sorted(non_empty)[0]

    parsed = _entries(units[chosen_unit])
    if not parsed:
        return None, None, chosen_unit
    parsed.sort(key=lambda e: (e["_end"], e["_filed"], str(e.get("accn") or "")), reverse=True)
    top = parsed[0]
    if require_currency is not None and chosen_unit.upper() != require_currency.upper():
        return None, top["_end"], chosen_unit
    return top["_val"], top["_end"], chosen_unit


def _fmt_decimal(val: Decimal | None) -> str | None:
    if val is None:
        return None
    return format(val, "f")


def build_scale_anchor(
    cik: str | int,
    *,
    as_of_filed_date: date | None = None,
    is_fpi: bool = False,
    user_agent: str | None = None,
    limiter: SecPacingLimiter | None = None,
    policy: FinDataPolicy | None = None,
    transport: Transport | None = None,
    stream_transport: StreamTransport | None = None,
    cancel_event: Event | None = None,
    deadline: float | None = None,
    telemetry: PacingTelemetry | None = None,
    facts_override: Mapping[str, Any] | None = None,
) -> ScaleAnchor:
    """Build SEC-native XBRL ``ScaleAnchor`` truncated strictly at ``filed_at <= as_of_filed_date``."""
    target_cik = cik10(cik)
    gaps: list[str] = []

    if facts_override is not None:
        facts = _truncate_facts_as_of(facts_override, as_of_filed_date)
    else:
        facts = {}
        # 1. Try us-gaap first, fall back to ifrs-full only if us-gaap yields no scale facts
        us_tags = tuple(dict.fromkeys(t for name in _SCALE_LINES for t in STANDARD_LINES[name].tags["us-gaap"]))
        us_raw = _fetch_concept_nodes(
            target_cik,
            "us-gaap",
            us_tags,
            user_agent=user_agent,
            limiter=limiter,
            policy=policy,
            transport=transport,
            stream_transport=stream_transport,
            cancel_event=cancel_event,
            deadline=deadline,
            telemetry=telemetry,
            gaps=gaps,
        )
        facts_us = _truncate_facts_as_of({"us-gaap": us_raw}, as_of_filed_date)
        if facts_us.get("us-gaap"):
            facts.update(facts_us)
        else:
            ifrs_tags = tuple(dict.fromkeys(t for name in _SCALE_LINES for t in STANDARD_LINES[name].tags["ifrs-full"]))
            ifrs_raw = _fetch_concept_nodes(
                target_cik,
                "ifrs-full",
                ifrs_tags,
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                stream_transport=stream_transport,
                cancel_event=cancel_event,
                deadline=deadline,
                telemetry=telemetry,
                gaps=gaps,
            )
            facts_ifrs = _truncate_facts_as_of({"ifrs-full": ifrs_raw}, as_of_filed_date)
            facts.update(facts_ifrs)

        # 2. Fetch DEI cover metrics directly from companyconcept/CIK{cik}/dei/{tag}.json
        dei_raw = _fetch_concept_nodes(
            target_cik,
            "dei",
            (DEI_PUBLIC_FLOAT_TAG, DEI_SHARES_OUTSTANDING_TAG),
            user_agent=user_agent,
            limiter=limiter,
            policy=policy,
            transport=transport,
            stream_transport=stream_transport,
            cancel_event=cancel_event,
            deadline=deadline,
            telemetry=telemetry,
            gaps=gaps,
        )
        facts_dei = _truncate_facts_as_of({"dei": dei_raw}, as_of_filed_date)
        facts.update(facts_dei)

    taxonomy_used: str | None = None
    tax_facts: Mapping[str, Any] = {}
    for tax in ("us-gaap", "ifrs-full"):
        if facts.get(tax):
            taxonomy_used = tax
            tax_facts = facts[tax]
            break

    rev_val: Decimal | None = None
    rev_cur: str | None = None
    rev_basis = "missing"
    ni_val: Decimal | None = None
    ni_cur: str | None = None
    ni_basis = "missing"
    assets_val: Decimal | None = None
    assets_cur: str | None = None

    if taxonomy_used is not None:
        rev_val, rev_cur, rev_basis = _extract_flow_metric(tax_facts, "revenue")
        ni_val, ni_cur, ni_basis = _extract_flow_metric(tax_facts, "net_income", preferred_currency=rev_cur)
        assets_tags = STANDARD_LINES["total_assets"].tags.get(taxonomy_used, ("Assets",))
        assets_val, assets_cur, _assets_end = _extract_instant_metric(
            tax_facts, assets_tags, preferred_currency=rev_cur
        )
    else:
        gaps.append("missing_us_gaap_and_ifrs_scale_facts")

    anchor_currency = rev_cur or assets_cur or ni_cur
    distinct_currencies = {c for c in (rev_cur, ni_cur, assets_cur) if c}
    if len(distinct_currencies) > 1:
        gaps.append(f"scale_anchor_mixed_currencies:{','.join(sorted(distinct_currencies))}")

    if rev_val is None:
        gaps.append("missing_ttm_revenue")
    elif rev_basis == "latest_fy":
        gaps.append("revenue_used_latest_fy_no_quarterly_ttm")
    elif rev_basis == "latest_quarter_only":
        gaps.append("revenue_used_single_quarter_only")

    if ni_val is None:
        gaps.append("missing_net_income")
    elif ni_basis == "latest_fy":
        gaps.append("net_income_used_latest_fy")
    elif ni_basis == "latest_quarter_only":
        gaps.append("net_income_used_single_quarter_only")

    if assets_val is None:
        gaps.append("missing_total_assets")

    dei_facts = facts.get("dei", {})
    pf_val, pf_date, pf_unit = _extract_dei_latest(
        dei_facts.get(DEI_PUBLIC_FLOAT_TAG),
        preferred_units=("USD",),
        require_currency="USD",
    )
    if pf_val is None:
        if pf_unit is not None and pf_unit.upper() != "USD":
            gaps.append(f"dei_public_float_non_usd:{pf_unit}")
        else:
            gaps.append("missing_dei_EntityPublicFloat")

    shares_val, shares_date, _shares_unit = _extract_dei_latest(
        dei_facts.get(DEI_SHARES_OUTSTANDING_TAG),
        preferred_units=("shares", "Shares"),
    )
    if shares_val is None:
        gaps.append("missing_dei_EntityCommonStockSharesOutstanding")

    share_unit_basis: Literal["common_shares", "ordinary_vs_adr_unverified"] = (
        "ordinary_vs_adr_unverified" if is_fpi else "common_shares"
    )
    if is_fpi:
        gaps.append("fpi_share_unit_basis_ordinary_vs_adr_unverified")

    return ScaleAnchor(
        cik=target_cik,
        as_of_filed_date=as_of_filed_date,
        taxonomy_used=taxonomy_used,
        currency=anchor_currency,
        latest_ttm_revenue=_fmt_decimal(rev_val),
        latest_net_income=_fmt_decimal(ni_val),
        latest_total_assets=_fmt_decimal(assets_val),
        public_float_usd=_fmt_decimal(pf_val),
        public_float_as_of_date=pf_date,
        shares_outstanding=_fmt_decimal(shares_val),
        shares_outstanding_as_of_date=shares_date,
        share_unit_basis=share_unit_basis,
        scale_anchor_gaps=gaps,
    )


def evaluate_materiality_against_anchor(
    amount: Decimal | str | int,
    event_currency: str | None,
    anchor: ScaleAnchor | None,
    *,
    metric: Literal["revenue", "total_assets", "public_float"] = "revenue",
) -> MaterialityCheck:
    """Compute exact materiality ratio vs ``ScaleAnchor`` while flagging currency mismatches cleanly."""
    if anchor is None:
        return MaterialityCheck(status="missing_anchor", anchor_metric=metric, note="ScaleAnchor unavailable.")
    try:
        amt = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return MaterialityCheck(status="missing_anchor", anchor_metric=metric, note="Invalid event amount.")

    if metric == "public_float":
        raw_anchor_val = anchor.public_float_usd
        anchor_cur = "USD"
    elif metric == "total_assets":
        raw_anchor_val = anchor.latest_total_assets
        anchor_cur = anchor.currency
    else:
        raw_anchor_val = anchor.latest_ttm_revenue
        anchor_cur = anchor.currency

    if raw_anchor_val is None:
        return MaterialityCheck(
            status="missing_anchor",
            anchor_metric=metric,
            anchor_currency=anchor_cur,
            event_currency=event_currency,
            note=f"ScaleAnchor.{metric} is missing.",
        )

    norm_ev_cur = event_currency.strip().upper() if event_currency and event_currency.strip() else None
    norm_an_cur = anchor_cur.strip().upper() if anchor_cur and anchor_cur.strip() else None
    if norm_ev_cur and norm_an_cur and norm_ev_cur != norm_an_cur:
        return MaterialityCheck(
            status="currency_mismatch",
            anchor_metric=metric,
            anchor_value=raw_anchor_val,
            anchor_currency=norm_an_cur,
            event_currency=norm_ev_cur,
            note=f"currency_mismatch: event is {norm_ev_cur} while ScaleAnchor.{metric} is {norm_an_cur}.",
        )

    denom = Decimal(raw_anchor_val)
    if denom <= 0:
        return MaterialityCheck(
            status="missing_anchor",
            anchor_metric=metric,
            anchor_value=raw_anchor_val,
            anchor_currency=norm_an_cur,
            event_currency=norm_ev_cur,
            note=f"ScaleAnchor.{metric} is non-positive.",
        )
    ratio = (amt / denom).quantize(Decimal("0.0001"))
    return MaterialityCheck(
        status="ok",
        ratio=format(ratio, "f"),
        anchor_metric=metric,
        anchor_value=raw_anchor_val,
        anchor_currency=norm_an_cur,
        event_currency=norm_ev_cur,
    )


def evaluate_share_pct_against_anchor(
    shares: Decimal | str | int,
    anchor: ScaleAnchor | None,
) -> MaterialityCheck:
    """Compute share % vs ``dei:EntityCommonStockSharesOutstanding``; blocks unsafe FPI ADR division."""
    if anchor is None or anchor.shares_outstanding is None:
        return MaterialityCheck(status="missing_anchor", anchor_metric="shares_outstanding")
    if anchor.share_unit_basis == "ordinary_vs_adr_unverified":
        return MaterialityCheck(
            status="unverified_adr_ratio",
            anchor_metric="shares_outstanding",
            anchor_value=anchor.shares_outstanding,
            note="FPI shares_outstanding basis is ordinary_vs_adr_unverified; cannot divide ADS shares without verified ratio.",
        )
    denom = Decimal(anchor.shares_outstanding)
    if denom <= 0:
        return MaterialityCheck(status="missing_anchor", anchor_metric="shares_outstanding")
    num = shares if isinstance(shares, Decimal) else Decimal(str(shares))
    pct = ((num / denom) * Decimal("100")).quantize(Decimal("0.0001"))
    return MaterialityCheck(
        status="ok",
        ratio=format(pct, "f"),
        anchor_metric="shares_outstanding",
        anchor_value=anchor.shares_outstanding,
    )


__all__ = [
    "DEI_PUBLIC_FLOAT_TAG",
    "DEI_SHARES_OUTSTANDING_TAG",
    "MaterialityCheck",
    "build_scale_anchor",
    "evaluate_materiality_against_anchor",
    "evaluate_share_pct_against_anchor",
]
