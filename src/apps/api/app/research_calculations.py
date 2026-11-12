"""Auditable deterministic financial calculations for research tools."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from datetime import date, datetime
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class NumericFact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    fact_id: str = Field(min_length=1, max_length=200)
    value: Decimal
    unit: str = Field(min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    period: str = Field(min_length=1, max_length=100)

    @field_validator("value")
    @classmethod
    def finite_reasonable_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or len(value.as_tuple().digits) > 30 or abs(value.adjusted()) > 30:
            raise ValueError("Value must be finite and within supported precision.")
        return value


class CalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    formula: Literal["growth_percent", "margin_percent", "difference"]
    first: NumericFact
    second: NumericFact
    decimal_places: int = Field(default=2, ge=0, le=8)


class RecalculateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_version_id: str = Field(min_length=1, max_length=100)
    calculation_index: int = Field(ge=0, le=1999, strict=True)


class CreateResearchCalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    parent_version_id: str = Field(min_length=1, max_length=100)
    formula: Literal["growth_percent", "margin_percent", "difference"]
    first_fact_id: str = Field(min_length=1, max_length=200)
    second_fact_id: str = Field(min_length=1, max_length=200)
    decimal_places: int = Field(default=2, ge=0, le=8, strict=True)


class ConfirmDirectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    parent_version_id: str = Field(min_length=1, max_length=100)
    fact_id: str = Field(min_length=1, max_length=200)


_DIRECTION_FINGERPRINT_FIELDS = ("fact_id", "label", "value", "unit", "currency", "period", "source_id", "quotation", "location")


def direction_confirmation_fingerprint(fact: dict) -> str:
    data = {key: fact.get(key) for key in _DIRECTION_FINGERPRINT_FIELDS}
    return sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def direction_user_confirmed(fact: dict) -> bool:
    confirmation = fact.get("direction_confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("confirmed_by") != "user":
        return False
    try:
        return confirmation.get("fact_sha256") == direction_confirmation_fingerprint(fact)
    except (TypeError, ValueError):
        return False


def table_user_confirmed(fact: dict) -> bool:
    confirmation = fact.get("table_confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("confirmed_by") != "user":
        return False
    try:
        return confirmation.get("fact_sha256") == direction_confirmation_fingerprint(fact)
    except (TypeError, ValueError):
        return False
class CalculationError(ValueError):
    """The available inputs do not support the requested calculation."""


def is_percent_unit(unit: str) -> bool:
    """Recognize percentage levels, excluding percentage-point differences."""
    normalized = " ".join(unit.strip().casefold().split())
    if re.search(r"\bpoints?\b|百分点", normalized):
        return False
    return bool(re.fullmatch(r"(?:percent|percentage|%|百分比)(?:\s*\([^()]{1,80}\)|,\s*(?:annualized|annual rate))?", normalized))


def quotation_is_inside_table_row(source_text: str, start: object, quotation: object) -> bool:
    """Identify a located single-line quotation inside a pipe-delimited row.

    This is a formatting cue, not validation of any table cell or period.
    """
    if (type(start) is not int or not isinstance(quotation, str) or not quotation or
            "\n" in quotation or start < 0 or source_text[start:start + len(quotation)] != quotation):
        return False
    line_start = source_text.rfind("\n", 0, start) + 1
    return source_text[line_start:start].lstrip().startswith("|")


def quotation_is_table_row(source_text: str, start: object, quotation: object) -> bool:
    """Recognize a uniquely located single-line table quotation, including a fragment."""
    if (type(start) is not int or not isinstance(quotation, str) or not quotation or
            "\n" in quotation or start < 0 or source_text[start:start + len(quotation)] != quotation):
        return False
    line_start = source_text.rfind("\n", 0, start) + 1
    line_end = source_text.find("\n", start)
    if line_end < 0:
        line_end = len(source_text)
    row = source_text[line_start:line_end].strip()
    return row.startswith("|") and row.endswith("|") and row.count("|") >= 3



def quotation_is_pdf_layout_row(source_text: str, start: object, quotation: object) -> bool:
    """Identify a possible multi-column row in layout-preserving PDF text.

    A formatting cue only: it cannot establish table boundaries, column
    identity, units or whether the selected value supports the fact.
    """
    if (type(start) is not int or not isinstance(quotation, str) or not quotation or
            len(quotation) > 1000 or start < 0 or source_text[start:start + len(quotation)] != quotation):
        return False
    if "\n" in quotation or "\f" in quotation:
        cursor = start
        for part in quotation.splitlines(keepends=True):
            line = part.rstrip("\r\n\f")
            if line and quotation_is_pdf_layout_row(source_text, cursor, line):
                return True
            cursor += len(part)
        return False
    line_start = max(source_text.rfind("\n", 0, start), source_text.rfind("\f", 0, start)) + 1
    ends = [end for end in (source_text.find("\n", start), source_text.find("\f", start)) if end >= 0]
    line_end = min(ends) if ends else len(source_text)
    line = source_text[line_start:line_end]
    if len(line) > 2000 or start + len(quotation) > line_end:
        return False
    cells = re.split(r"(?: {2,}|\t+)", line.strip())
    amount = re.compile(r"[\$€£¥]?\(?[+−-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?%?")
    numeric_cells = [index for index, cell in enumerate(cells) if amount.fullmatch(cell.strip())]
    return len(numeric_cells) >= 2 and any(re.search(r"[A-Za-z\u3400-\u9fff]", cell) for cell in cells[:numeric_cells[0]])

def _spanned_table_period_conflict(source_text: str, start: int, value: Decimal,
                                   period: str) -> dict[str, str] | None:
    """Flag a possible mismatch in a tightly bounded extracted HTML table.

    Span labels are extractor annotations or user-provided text, not proof of
    the original HTML layout. The result is a review cue, never verification.
    """
    year_match = re.fullmatch(r"(?:FY\s*)?((?:19|20)\d{2})", period.strip(), re.IGNORECASE)
    if year_match is None:
        year_match = re.fullmatch(r"(?:Fiscal\s+)?Year\s+Ended\s+[A-Za-z]+\s+\d{1,2},?\s+((?:19|20)\d{2})", period.strip(), re.IGNORECASE)
        if year_match is not None:
            date_text = re.sub(r"^(?:Fiscal\s+)?Year\s+Ended\s+", "", period.strip(), flags=re.IGNORECASE)
            try:
                datetime.strptime(date_text.replace(",", ""), "%B %d %Y")
            except ValueError:
                return None
    if year_match is None:
        return None
    def parse_row(line: str) -> list[tuple[int, int, str]] | None:
        line = line.strip()
        if len(line) > 1000 or not line.startswith("|") or not line.endswith("|"):
            return None
        result = []
        position = 0
        for raw in line[1:-1].split("|"):
            cell = raw.strip()
            marker = re.match(r"^\[colspan=([2-9]|[1-9]\d)\]\s*", cell, re.IGNORECASE)
            span = int(marker.group(1)) if marker else 1
            cell = cell[marker.end():].strip() if marker else cell
            if "[colspan=" in cell.casefold() or "[rowspan=" in cell.casefold():
                return None
            result.append((position, position + span, cell))
            position += span
            if position > 120:
                return None
        return result if len(result) >= 2 else None
    def amount(cell: str) -> Decimal | None:
        if not re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", cell):
            return None
        try:
            return Decimal(cell.replace(",", ""))
        except InvalidOperation:
            return None
    def parsed_year(cell: str) -> str | None:
        match = re.fullmatch(r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+([0-3]?\d),\s*((?:19|20)\d{2})", cell, re.IGNORECASE)
        if not match:
            return None
        try:
            datetime.strptime(" ".join(cell.split()), "%B %d, %Y")
        except ValueError:
            return None
        return match.group(2)
    line_start = source_text.rfind("\n", 0, start) + 1
    line_end = source_text.find("\n", start)
    if line_end < 0:
        line_end = len(source_text)
    line = source_text[line_start:line_end]
    row = parse_row(line)
    if row is None or "[colspan=" not in line.casefold() or not row[0][2]:
        return None
    positions = [(a, b) for a, b, cell in row[1:] if amount(cell) == value]
    if len(positions) != 1:
        return None
    width = row[-1][1]
    lines = source_text[max(0, line_start - 12000):line_start].splitlines()[-12:]
    for i in range(len(lines) - 1, -1, -1):
        cells = parse_row(lines[i])
        if cells is None or cells[-1][1] != width:
            return None
        years = [(a, b, parsed_year(cell)) for a, b, cell in cells if parsed_year(cell)]
        if len(years) >= 2:
            if len({year for _, _, year in years}) != len(years):
                return None
            if any(cell and not parsed_year(cell) for _, _, cell in cells):
                return None
            title_found = False
            for title_line in reversed(lines[max(0, i - 4):i]):
                title_row = parse_row(title_line)
                if title_row is None or title_row[-1][1] != width:
                    break
                content = [cell for _, _, cell in title_row if cell]
                if len(content) == 1 and re.fullmatch(r"years?\s+ended", content[0], re.IGNORECASE):
                    title_found = True
                    break
                if content:
                    break
            if not title_found:
                return None
            candidates = [year for a, b, year in years if a <= positions[0][0] and positions[0][1] <= b]
            if len(candidates) == 1 and candidates[0] != year_match.group(1):
                return {"declared_year": year_match.group(1), "column_year": candidates[0]}
            return None
        # Only blank rows, unit labels, or ordinary numeric data rows may
        # intervene between date header and target. Arbitrary text stops us.
        nonempty = [cell for _, _, cell in cells if cell]
        if nonempty and not (len(nonempty) == 1 and re.fullmatch(r"\(in\s+(?:millions?|billions?|thousands?)\)", nonempty[0], re.IGNORECASE)):
            if not cells[0][2] or any(cell and cell not in {"$", "€", "£"} and amount(cell) is None for _, _, cell in cells[1:]):
                return None
    return None


def table_period_conflict(source_text: str, start: object, quotation: object,
                          value: Decimal, period: str) -> dict[str, str] | None:
    """Detect a narrow year/value mismatch in a simple rectangular table.

    This never confirms that a fact is correct: unsupported spans, repeated
    values, unrecognized periods, complex amounts and broken table layout fall
    back to ordinary review. The caller must establish unique source location.
    """
    if not quotation_is_table_row(source_text, start, quotation):
        return None
    clean_quote = re.sub(r"\[(?:colspan|rowspan)=\d+\]", " ", quotation, flags=re.IGNORECASE)
    quoted_values = set()
    for match in re.finditer(r"(?<![\w.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\w.,])", clean_quote):
        try:
            quoted_values.add(Decimal(match.group(1).replace(",", "")))
        except InvalidOperation:
            continue
    if value not in quoted_values:
        return None
    spanned_conflict = _spanned_table_period_conflict(source_text, start, value, period)
    if spanned_conflict is not None:
        return spanned_conflict
    year_match = re.fullmatch(r"(?:FY\s*)?((?:19|20)\d{2})", period.strip(), re.IGNORECASE)
    if not year_match:
        return None
    line_start = source_text.rfind("\n", 0, start) + 1
    line_end = source_text.find("\n", start)
    if line_end < 0:
        line_end = len(source_text)
    def cells(line: str) -> list[str] | None:
        line = line.strip()
        if len(line) > 1000 or not line.startswith("|") or not line.endswith("|") or "[colspan=" in line.casefold() or "[rowspan=" in line.casefold():
            return None
        result = [part.strip() for part in line[1:-1].split("|")]
        return result if 3 <= len(result) <= 12 else None
    def amount(cell: str) -> Decimal | None:
        match = re.fullmatch(r"(?:(?:USD|EUR|GBP)\s+)?([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?:\s+(?:USD|EUR|GBP))?", cell, re.IGNORECASE)
        if not match:
            return None
        try:
            return Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            return None
    row = cells(source_text[line_start:line_end])
    if row is None or not row[0]:
        return None
    positions = [i for i, cell in enumerate(row[1:], 1) if amount(cell) == value]
    if len(positions) != 1:
        return None
    # A small, contiguous rectangle prevents a header from an unrelated
    # narrative or a differently shaped table being applied to the row.
    prior = source_text[max(0, line_start - 12000):line_start].splitlines()
    for line in reversed(prior[-10:]):
        current = cells(line)
        if current is None or len(current) != len(row):
            return None
        if re.fullmatch(r"(?:fiscal\s+)?years?(?:\s+ended)?", current[0], re.IGNORECASE):
            years = current[1:]
            if (not all(re.fullmatch(r"(?:19|20)\d{2}", year) for year in years) or
                    len(set(years)) != len(years)):
                return None
            column_year, declared_year = years[positions[0] - 1], year_match.group(1)
            if column_year != declared_year:
                return {"declared_year": declared_year, "column_year": column_year}
            return None
        if not current[0] or any(cell and amount(cell) is None for cell in current[1:]):
            return None
    return None


def table_currency_quote_missing(currency: str | None, quotation: str | None) -> bool:
    """Flag a table-row quotation without an explicit currency identifier.

    A code or symbol is only a cue, not verification of the financial fact.
    A bare dollar sign is ambiguous across several currencies.
    """
    if not currency or not isinstance(quotation, str) or not quotation.lstrip().startswith("|"):
        return False
    code = currency.strip().upper()
    if re.search(r"(?<![A-Za-z])" + re.escape(code) + r"(?![A-Za-z])", quotation, re.IGNORECASE):
        return False
    patterns = {"USD": r"US\s*\$|U\.?S\.?\s+dollars?", "EUR": r"€|\beuros?\b",
                "GBP": r"£|\bpounds?\s+sterling\b", "JPY": r"\bJapanese\s+yen\b",
                "KRW": r"₩|\bKorean\s+won\b"}
    return not bool(re.search(patterns.get(code, r"(?!)"), quotation, re.IGNORECASE))



_CURRENCY_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<before>USD|EUR|GBP|US\s*\$|€|£)\s*(?P<amount1>\d[\d,]*(?:\.\d+)?)|"
    r"(?P<amount2>\d[\d,]*(?:\.\d+)?)\s*(?P<after>USD|EUR|GBP|US\s*\$|€|£))(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def currency_value_conflict(currency: str | None, value: object, quotation: str | None) -> bool:
    """Flag explicit currency cues on the same value; not a semantic verdict.

    Bare dollar/yen signs, unrelated values and multiple conflicting cues are
    deliberately inconclusive. Neither a match nor silence proves the unit.
    """
    code = currency.strip().upper() if isinstance(currency, str) else ""
    if code not in {"USD", "EUR", "GBP"} or not isinstance(quotation, str):
        return False
    value_text = str(value)
    if len(value_text) > 128:
        return False
    try:
        expected = Decimal(value_text)
        if not expected.is_finite():
            return False
    except (InvalidOperation, ValueError):
        return False
    seen: set[str] = set()
    for match in _CURRENCY_NUMBER.finditer(quotation):
        try:
            amount = Decimal((match.group("amount1") or match.group("amount2")).replace(",", ""))
        except InvalidOperation:
            continue
        if amount != expected:
            continue
        cue = (match.group("before") or match.group("after")).upper().replace(" ", "")
        seen.add({"US$": "USD", "€": "EUR", "£": "GBP"}.get(cue, cue))
    return bool(len(seen) == 1 and code not in seen)


def _comparable_period(period: str) -> tuple[str, tuple[int, ...]] | None:
    """Parse only unambiguous matching period formats, without guessing fiscal calendars."""
    value = period.strip()
    if match := re.fullmatch(r"(FY)?((?:19|20)\d{2})", value, re.IGNORECASE):
        return ("fiscal_year" if match.group(1) else "calendar_year", (int(match.group(2)),))
    if match := re.fullmatch(r"((?:19|20)\d{2})[- ]?Q([1-4])", value, re.IGNORECASE):
        return ("calendar_quarter", (int(match.group(1)), int(match.group(2))))
    if match := re.fullmatch(r"((?:19|20)\d{2})-(0[1-9]|1[0-2])", value):
        return ("calendar_month", (int(match.group(1)), int(match.group(2))))
    # Financial filings commonly spell out the end date of each annual period.
    # Keep this distinct from bare dates and FY labels: matching dates alone do
    # not establish that two companies use comparable fiscal calendars.
    match = re.fullmatch(r"(?:fiscal\s+)?year\s+ended\s+([A-Za-z]+)\s+(\d{1,2}),?\s+((?:19|20)\d{2})", value, re.IGNORECASE)
    if match:
        months = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
        try:
            day = date(int(match.group(3)), months.index(match.group(1).casefold()) + 1, int(match.group(2)))
        except ValueError:
            return None
        return ("year_ended_date", (day.year, day.month, day.day))
    if re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
        return ("date", (day.year, day.month, day.day))
    return None


def _period_granularity(period: str) -> str | None:
    """Return only explicit, recognized annual, quarterly or monthly grains.

    A bare date does not establish a reporting duration. Matching grains do
    not establish comparable fiscal calendars or accounting definitions.
    """
    parsed = _comparable_period(period)
    if parsed is None:
        return None
    return {"fiscal_year": "year", "calendar_year": "year", "year_ended_date": "year",
            "calendar_quarter": "quarter", "calendar_month": "month"}.get(parsed[0])


def _growth_granularity_conflict(first: str, second: str) -> bool:
    a, b = _period_granularity(first), _period_granularity(second)
    return a is not None and b is not None and a != b


_TEXT_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_TEXT_PERCENT = re.compile(r"(?<![A-Za-z0-9_.])([-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:%|％|percent\b)", re.IGNORECASE)
_RATIO_SCALE_PREFIX = re.compile(r"(?:/|÷)\s*[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*\)*\s*[×*]\s*$")


def _is_ratio_scale_factor(text: str, match: re.Match[str]) -> bool:
    """Recognize only the explicit conversion factor in a numeric ratio.

    A bare 100% or other percentage still needs review. This does not parse
    arbitrary expressions or establish whether a model's formula is correct.
    """
    try:
        return Decimal(match.group(1).replace(",", "").replace("−", "-")) == 100 and bool(
            _RATIO_SCALE_PREFIX.search(text[:match.start()]))
    except InvalidOperation:
        return False


def calculation_input_status(calculation: dict, facts: list[dict]) -> str:
    """Check a retained calculation against this version's fact values.

    This verifies consistency, not whether the facts or the result are true.
    Old or user-supplied records with missing structure fail closed.
    """
    inputs = calculation.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 2 or any(not isinstance(item, dict) for item in inputs):
        return "unverifiable"
    fields = ("fact_id", "value", "unit", "currency", "period")
    try:
        expected = [NumericFact.model_validate({key: item[key] for key in fields if key in item}) for item in inputs]
        if expected[0].fact_id == expected[1].fact_id:
            return "unverifiable"
        by_id: dict[str, list[dict]] = {}
        for item in facts:
            fact_id = item.get("fact_id")
            if isinstance(fact_id, str):
                by_id.setdefault(fact_id, []).append(item)
        for snapshot in expected:
            candidates = by_id.get(snapshot.fact_id, [])
            if len(candidates) != 1:
                return "missing_or_ambiguous"
            if (candidates[0].get("quotation_match") == "pdf_line_hyphen_restored" or
                candidates[0].get("quotation_match") == "table_markup_expanded" and not table_user_confirmed(candidates[0]) or
                candidates[0].get("table_period_conflict") or (candidates[0].get("direction_context_unverified") and not direction_user_confirmed(candidates[0])) or currency_value_conflict(
                    candidates[0].get("currency"), snapshot.value, candidates[0].get("quotation"))):
                return "unverifiable"
            actual = NumericFact.model_validate({key: candidates[0][key] for key in fields if key in candidates[0]})
            if actual != snapshot:
                return "changed"
            if candidates[0].get("source_value_warnings"):
                return "unverifiable"
    except (ValidationError, TypeError, ValueError):
        return "unverifiable"
    return "matched"


def calculate(request: CalculationRequest) -> dict:
    """Calculate first versus second; first is current/numerator.

    Preserve original inputs and reject ambiguous or non-comparable quantities.
    The second input is the baseline for growth and denominator for margin.
    """
    a, b = request.first, request.second
    period_order_check = "not_checked"
    if a.fact_id == b.fact_id:
        raise CalculationError("Inputs must identify distinct facts.")
    if a.unit != b.unit or a.currency != b.currency:
        raise CalculationError("Input units and currencies must match.")
    if request.formula == "margin_percent" and a.period != b.period:
        raise CalculationError("Margin inputs must have the same period.")
    if request.formula == "growth_percent" and a.period == b.period:
        raise CalculationError("Growth inputs must refer to different periods.")
    if request.formula == "growth_percent":
        current, baseline = _comparable_period(a.period), _comparable_period(b.period)
        if current is not None and baseline is not None and current[0] == baseline[0]:
            if current[1] <= baseline[1]:
                raise CalculationError("Growth current period must be later than its baseline.")
            period_order_check = "recognized_formats_only"
    if request.formula == "growth_percent" and b.value <= 0:
        raise CalculationError("Growth requires a positive baseline.")
    if request.formula == "margin_percent" and b.value <= 0:
        raise CalculationError("Margin requires a positive denominator.")
    formula_version = "1"
    try:
        with localcontext() as context:
            context.prec = 100
            if request.formula == "growth_percent":
                result = (a.value / b.value - 1) * 100
                expression = "(first / second - 1) * 100"
                unit = "percent"
            elif request.formula == "margin_percent":
                result = a.value / b.value * 100
                expression = "first / second * 100"
                unit = "percent"
            else:
                result = a.value - b.value
                expression = "first - second"
                if is_percent_unit(a.unit):
                    if a.currency is not None:
                        raise CalculationError("Percentage inputs cannot have a currency.")
                    unit = "percentage points"
                    formula_version = "2"
                else:
                    unit = a.unit
            rounded = result.quantize(Decimal(1).scaleb(-request.decimal_places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, OverflowError) as error:
        raise CalculationError("Calculation exceeds supported precision.") from error
    return {"formula_id": request.formula, "formula_version": formula_version, "expression": expression,
            "input_verification": "not_verified_by_calculator",
            "input_precision_status": "not_established",
            **({"period_order_check": period_order_check,
                "period_granularity_check": "different_labels_verify_stock_or_flow" if _growth_granularity_conflict(a.period, b.period)
                    else "not_established"} if request.formula == "growth_percent" else {}),
            "inputs": [a.model_dump(mode="json"), b.model_dump(mode="json")],
            "value": str(rounded), "unit": unit,
            "currency": None if unit in {"percent", "percentage points"} else a.currency,
            "rounding": "half_up", "decimal_places": request.decimal_places}


def _positive_percentage_describes_decrease(text: str, number_start: int, raw: str) -> bool:
    """Recognize only an immediately stated decrease, not its magnitude or cause."""
    if raw.startswith(("-", "+", "−")):
        return False
    prefix = text[max(0, number_start - 80):number_start]
    cue = re.search(r"(?:减少|減少|下降|下滑|下跌|降低)(?:了)?\s*(?:约|約|大约|大約)?\s*$"
                    r"|\b(?:declined|decreased|fell|dropped)\s+by\s+(?:(?:about|approximately)\s+)?$",
                    prefix, re.IGNORECASE)
    if cue is None:
        return False
    prior = prefix[max(0, cue.start() - 16):cue.start()]
    if re.search(r"未|不|没|沒|無|无|并非|並非|\b(?:not|never|no)\b", prior, re.IGNORECASE):
        return False
    return True


def claim_calculation_differences(claims: list[dict], calculations: list[dict], facts: list[dict]) -> list[dict]:
    """Flag narrow numerical mismatches for review, without asserting entailment.

    Compare claims whose percentages denote the same number and have a uniquely
    matching denominator; inspect each stated precision separately. Ratio
    calculations additionally require a ratio cue or both input values, so an
    unrelated year-on-year percentage is not matched on the denominator alone.
    Recompute the saved calculation from current facts at both its own and the
    claim's stated precision; ignore user-edited, stale or ambiguous calculations.
    """
    by_denominator: dict[Decimal, list[tuple[int, CalculationRequest]]] = {}
    for index, item in enumerate(calculations):
        if (item.get("authorship") == "user" or item.get("formula_version") != "1" or
                item.get("formula_id") not in {"growth_percent", "margin_percent"} or
                item.get("unit") != "percent" or calculation_input_status(item, facts) != "matched"):
            continue
        try:
            inputs = item["inputs"]
            request = CalculationRequest.model_validate({"formula": item["formula_id"], "first": inputs[0],
                "second": inputs[1], "decimal_places": item.get("decimal_places", 2)})
            recomputed = calculate(request)
            if (recomputed.get("period_granularity_check") == "different_labels_verify_stock_or_flow" or
                    Decimal(str(item.get("value"))) != Decimal(recomputed["value"])):
                continue
        except (ValidationError, CalculationError, InvalidOperation, TypeError, ValueError, KeyError, IndexError):
            continue
        by_denominator.setdefault(request.second.value, []).append((index, request))
    differences: list[dict] = []
    for claim_index, claim in enumerate(claims):
        text = claim.get("text")
        if not isinstance(text, str) or len(text) > 3000:
            continue
        percentages = [match for match in _TEXT_PERCENT.finditer(text) if not _is_ratio_scale_factor(text, match)]
        if not percentages or len(percentages) > 20:
            continue
        raw_values = [item.group(1).replace(",", "").replace("−", "-") for item in percentages]
        if any(len(raw) > 40 or ("." in raw and len(raw.rsplit(".", 1)[1]) > 8) for raw in raw_values):
            continue
        try:
            stated_values = {Decimal(raw) for raw in raw_values}
            mentioned = {Decimal(m.group().replace(",", "").replace("−", "-")) for m in _TEXT_NUMBER.finditer(text)}
        except InvalidOperation:
            continue
        if len(stated_values) != 1:
            continue
        ratio_cue = re.search(r"\b(?:margin|ratio|share|proportion|percentage\s+of)\b|利潤率|利润率|毛利率|占比|佔比|比例|比率|占|佔", text, re.IGNORECASE)
        candidates = [entry for value in mentioned for entry in by_denominator.get(value, [])
                      if entry[1].formula != "margin_percent" or ratio_cue or entry[1].first.value in mentioned]
        if len(candidates) != 1:
            continue
        calculation_index, request = candidates[0]
        seen: set[tuple[str, str]] = set()
        for match, raw in zip(percentages, raw_values):
            precision = len(raw.rsplit(".", 1)[1]) if "." in raw else 0
            try:
                expected = calculate(request.model_copy(update={"decimal_places": precision}))["value"]
                if (Decimal(raw) == Decimal(expected) or
                        (request.formula == "growth_percent" and Decimal(raw) > 0 and
                         -Decimal(raw) == Decimal(expected) and
                         _positive_percentage_describes_decrease(text, match.start(1), raw)) or
                        (raw, expected) in seen):
                    continue
            except (CalculationError, InvalidOperation):
                continue
            seen.add((raw, expected))
            differences.append({"claim_index": claim_index, "calculation_index": calculation_index,
                                "claimed_value": raw, "calculated_value": expected,
                                "unit": "percent", "status": "requires_review"})
            if len(differences) == 50:
                return differences
    return differences


def suggest_percentage_calculations(claims: list[dict], calculations: list[dict], facts: list[dict]) -> list[dict]:
    """Suggest source-covered calculations; matching is not semantic verification.

    Only reproducible machine calculations with uniquely identified, unedited
    facts whose original passages are covered by the claim's citations qualify.
    Percentages remain uncited and still require review.
    """
    from copy import deepcopy

    facts_by_id: dict[str, list[dict]] = {}
    for fact in facts:
        if isinstance(fact.get("fact_id"), str):
            facts_by_id.setdefault(fact["fact_id"], []).append(fact)
    valid: list[tuple[int, CalculationRequest, list[dict]]] = []
    for index, item in enumerate(calculations):
        if (item.get("authorship") == "user" or item.get("formula_version") != "1" or
                item.get("formula_id") not in {"growth_percent", "margin_percent"} or
                item.get("unit") != "percent" or calculation_input_status(item, facts) != "matched"):
            continue
        try:
            inputs = item["inputs"]
            request = CalculationRequest.model_validate({"formula": item["formula_id"], "first": inputs[0],
                "second": inputs[1], "decimal_places": item.get("decimal_places", 2)})
            recomputed = calculate(request)
            if (recomputed.get("period_granularity_check") == "different_labels_verify_stock_or_flow" or
                    Decimal(str(item.get("value"))) != Decimal(recomputed["value"])):
                continue
            source_facts = [facts_by_id[fact.fact_id][0] for fact in (request.first, request.second)]
        except (ValidationError, CalculationError, InvalidOperation, TypeError, ValueError, KeyError, IndexError):
            continue
        if any(fact.get("verification") != "model_extraction_not_independently_verified" or
               fact.get("authorship") == "user" or not isinstance(fact.get("source_id"), str) or
               not isinstance(fact.get("quotation"), str) or not fact.get("quotation") or
               not isinstance(fact.get("location"), dict) or fact["location"].get("status") == "ambiguous" or
               type(fact["location"].get("character_start")) is not int or
               type(fact["location"].get("character_end")) is not int or
               fact["location"]["character_end"] - fact["location"]["character_start"] != len(fact["quotation"])
               for fact in source_facts):
            continue
        valid.append((index, request, source_facts))
    # Index rounded values once so a large saved version does not repeatedly
    # recompute every calculation for every claim.
    by_result: dict[tuple[int, Decimal], list[tuple[int, CalculationRequest, list[dict]]]] = {}
    for index, request, source_facts in valid:
        for precision in range(9):
            try:
                value = Decimal(calculate(request.model_copy(update={"decimal_places": precision}))["value"])
            except (CalculationError, InvalidOperation):
                continue
            by_result.setdefault((precision, value), []).append((index, request, source_facts))
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("suggested_calculations", None)
        citations = entry.get("citations")
        percentages = entry.get("uncited_percentages")
        if not isinstance(citations, list) or not isinstance(percentages, list):
            output.append(entry)
            continue
        candidates = []
        for raw in percentages[:5]:
            match = _TEXT_PERCENT.fullmatch(raw) if isinstance(raw, str) else None
            if match is None:
                continue
            value = match.group(1).replace(",", "").replace("−", "-")
            precision = len(value.rsplit(".", 1)[1]) if "." in value else 0
            if len(value) > 40 or precision > 8:
                continue
            try:
                stated = Decimal(value)
                matching = by_result.get((precision, stated), [])
                text = entry.get("text")
                if (not matching and stated > 0 and isinstance(text, str) and text.count(raw) == 1 and
                        _positive_percentage_describes_decrease(text, text.index(raw) + match.start(1), value)):
                    matching = [item for item in by_result.get((precision, -stated), []) if item[1].formula == "growth_percent"]
            except InvalidOperation:
                continue
            for index, request, source_facts in matching:
                if not all(any(isinstance(citation, dict) and not citation.get("partial_token") and
                    citation.get("source_id") == fact["source_id"] and isinstance(citation.get("location"), dict) and
                    citation["location"].get("status") != "ambiguous" and
                    type(citation["location"].get("character_start")) is int and
                    type(citation["location"].get("character_end")) is int and
                    citation["location"]["character_start"] <= fact["location"]["character_start"] and
                    citation["location"]["character_end"] >= fact["location"]["character_end"]
                    for citation in citations) for fact in source_facts):
                    continue
                candidates.append({"calculation_index": index, "claimed_percentage": raw,
                    "formula_id": request.formula, "input_fact_ids": [request.first.fact_id, request.second.fact_id],
                    "status": "requires_semantic_review"})
                if len(candidates) >= 5:
                    break
            if len(candidates) >= 5:
                break
        if candidates:
            entry["suggested_calculations"] = candidates
        output.append(entry)
    return output
