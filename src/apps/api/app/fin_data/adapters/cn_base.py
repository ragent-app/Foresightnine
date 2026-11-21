"""Provider independent assembly for A-share bars and statement lines (used by Tushare and BaoStock)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from ...research_clock import exchange_session, last_completed_session, session_status
from .. import cn
from ..models import StatementLine

DELAY_NOTE = ("A-share daily bars are end-of-day data from a free source; intraday bars are not available. "
              "Trading holidays are not modelled, so a holiday simply has no bar.")


def dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def parse_day(value: Any) -> date | None:
    text = str(value or "").strip()
    try:
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class BarWindow:
    start: date | None
    requested_end: date
    effective_end: date
    latest: date
    status: str | None
    clamped: bool
    tz: ZoneInfo
    open_time: Any
    notes: list[str] = field(default_factory=list)


def bar_window(now: datetime, exchange: str, start: date | None, end: date | None, include_partial: bool = False) -> BarWindow:
    sess = exchange_session(exchange)
    assert sess is not None, "A-share exchanges are registered in research_clock"
    tz = ZoneInfo(sess.timezone)
    latest = last_completed_session(now, sess)
    today_local = now.astimezone(tz).date()
    requested_end = end or today_local
    effective = requested_end if include_partial and requested_end <= today_local else min(requested_end, latest)
    notes = [DELAY_NOTE]
    if effective < requested_end:
        notes.append(f"Requested end {requested_end} clamped to last completed session {effective}.")
    return BarWindow(start, requested_end, effective, latest, session_status(now, sess), effective < requested_end,
                     tz, sess.open, notes)


# ------------------------------------------------------------------ statement assembly
@dataclass(frozen=True)
class LineValue:
    value: Decimal
    tag: str
    statement: str
    unit: str
    flow: bool
    filed: date | None = None
    form: str | None = None
    superseded: tuple[str, ...] = ()
    basis: str | None = None  # overrides the default (ytd_cumulative / point_in_time), e.g. "period_ratio"


def build_statement_lines(entity_id: str, values: dict[str, dict[date, LineValue]], *, taxonomy: str, period: str,
                          n: int, derive_quarters: bool) -> tuple[list[StatementLine], list[str]]:
    """``values[line][report_end]``. Periods are chosen across all lines (newest n). Quarter periods keep the
    cumulative (year-to-date) values as reported; single quarters are derived only when ``derive_quarters``."""
    notes: list[str] = []
    all_ends = {end for per in values.values() for end in per}
    chosen = cn.select_report_ends(all_ends, period, n)
    out: list[StatementLine] = []
    for line in sorted(values):
        per = values[line]
        for end in chosen:
            lv = per.get(end)
            if lv is None:
                continue
            instant = not lv.flow
            out.append(StatementLine(
                entity_id=entity_id, statement=lv.statement, line=line, reported_tag=lv.tag, taxonomy=taxonomy,
                period_start=None if instant else cn.year_start(end), period_end=end,
                fiscal_label=cn.ytd_label(end), value=lv.value, unit=lv.unit, currency=cn.CN_CURRENCY if "CNY" in lv.unit else None,
                form=lv.form, filed_at=lv.filed,
                basis=lv.basis or ("point_in_time" if instant else "ytd_cumulative"), accounting_standard=cn.ACCOUNTING_STANDARD,
                revision_count=len(lv.superseded), superseded=list(lv.superseded)))
        if derive_quarters and period == "quarter" and any(v.flow for v in per.values()):
            if line.startswith("eps") or "ratio" in next(iter(per.values())).unit:
                notes.append(f"{line}: single-quarter derivation is not applied to per-share or ratio values.")
                continue
            # Derivation needs the earlier periods of the same year even if they fall outside the chosen window.
            ytd = {e: v.value for e, v in per.items() if v.flow and cn.is_report_end(e)}
            quarters, skipped = cn.derive_single_quarters(ytd, chosen)
            notes.extend(f"{line}: {s}" for s in skipped)
            for qend, qvalue, derivation in quarters:
                lv = per[qend]
                out.append(StatementLine(
                    entity_id=entity_id, statement=lv.statement, line=line, reported_tag=lv.tag, taxonomy=taxonomy,
                    period_start=cn.quarter_start(qend), period_end=qend, fiscal_label=cn.quarter_label(qend),
                    value=qvalue, unit=lv.unit, currency=cn.CN_CURRENCY if "CNY" in lv.unit else None, form=lv.form,
                    filed_at=lv.filed, derived=True, derivation=derivation, basis="single_quarter_derived",
                    accounting_standard=cn.ACCOUNTING_STANDARD))
    if any(l.basis == "ytd_cumulative" for l in out):
        notes.append("Flow values for Q1/H1/9M/FY are cumulative from 1 January (year-to-date) exactly as reported; "
                     "they are not single quarters unless marked derived.")
    if any(l.derived for l in out):
        notes.append("Derived rows are differences of cumulative reported figures and are marked derived=true; "
                     "Q1 equals the reported Q1 figure.")
    if any(l.revision_count for l in out):
        notes.append("Where a period was restated or updated the latest revision is shown; earlier values are listed "
                     "in superseded.")
    notes.append(f"Accounting standard: {cn.ACCOUNTING_STANDARD}; currency {cn.CN_CURRENCY}; fiscal year ends 31 December.")
    return out, notes
