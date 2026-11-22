"""Deterministic, evidence-bound comparison table builder and verification."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Annotated, Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .research_calculations import UnitScaleSpec, parse_unit_scale
from .research_source_policy import source_eligibility
from .research_sources import ResearchSource

TableType = Literal["comparison", "financial_summary", "peer_comp", "valuation", "time_series", "custom"]
TableAlign = Literal["left", "right", "center"]


class TableBuildError(ValueError):
    """Raised when table inputs violate provenance, permission, or unit-comparability rules."""


_ALLOWED_IDENTIFIER_RE = re.compile(
    r"(?:"
    r"\bFY\s*(?:19|20)?\d{2}\b|"
    r"\b(?:19|20)\d{2}\s*[- ]?\s*Q[1-4]\b|"
    r"(?<![A-Za-z0-9])Q[1-4](?:\s*(?:19|20)\d{2})?(?![A-Za-z0-9])|"
    r"\b(?:19|20)\d{2}\s*[- ]?\s*H[12]\b|"
    r"\bH[12]\s*(?:19|20)\d{2}\b|"
    r"\b(?:19|20)\d{2}-9M\b|"
    r"\b9M\s*(?:19|20)\d{2}\b|"
    r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b|"
    r"\b(?:19|20)\d{2}-\d{2}\b|"
    r"(?:19|20)\d{2}\s*年(?:\s*(?:[-/]?\s*Q[1-4]|第?\s*[1-4一二三四]\s*季(?:度)?|\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号]?)?))?|"
    r"\b(?:10-[KQ]|8-K|20-F|40-F|6-K|S-1|F-1|20F|10K|10Q)\b|"
    r"\b\d{4,6}\.(?:HK|KS|KQ|SH|SZ|BJ|SS|T)\b|"
    r"\b(?:CIK\s*\d{6,10})\b|"
    r"\b(?:Item\s+\d+[A-Z]?|Note\s+\d+|Scope\s+[123]|Tier\s+[123]|Series\s+[A-Z0-9]+|Phase\s+[0-9IVX]+)\b|"
    r"\b(?:S&P\s*500|CSI\s*300|Russell\s*2000|FTSE\s*100|Nikkei\s*225|HSCEI)\b"
    r")",
    re.IGNORECASE,
)


def _contains_ungrounded_number(text: str) -> bool:
    """Return True if a text cell contains numeric quantities beyond safe identifiers/dates."""
    if re.search(r"[$€£¥₩%％]", text):
        if re.search(r"\d", text):
            return True
    stripped = _ALLOWED_IDENTIFIER_RE.sub(" ", text)
    return bool(re.search(r"\d", stripped))


class CreateTableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    label: str = Field(min_length=1, max_length=100)
    unit_preference: str | None = Field(default=None, min_length=1, max_length=64)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    align: TableAlign | None = None

    @field_validator("label")
    @classmethod
    def _nonblank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Column label must not be blank.")
        return value


class FactTableCell(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["fact"]
    fact_id: str = Field(min_length=1, max_length=200)
    unit_preference: str | None = Field(default=None, min_length=1, max_length=64)


class CalculationTableCell(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["calculation"]
    calculation_id: str = Field(min_length=1, max_length=200)
    unit_preference: str | None = Field(default=None, min_length=1, max_length=64)


class TextTableCell(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=200)

    @field_validator("text")
    @classmethod
    def _reject_ungrounded_numbers(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text cell must not be blank.")
        if _contains_ungrounded_number(value):
            raise ValueError(
                "Text cells cannot contain ungrounded numeric quantities; bind numeric cells to fact_id or calculation_id."
            )
        return value


class EmptyTableCell(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["empty"]
    display: Literal["—", "-", "N/A", ""] = "—"


TableCellInput = Annotated[
    FactTableCell | CalculationTableCell | TextTableCell | EmptyTableCell,
    Field(discriminator="kind"),
]


class CreateTableRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str | None = Field(default=None, max_length=120)
    cells: dict[str, TableCellInput] = Field(min_length=1, max_length=16)

    @field_validator("label")
    @classmethod
    def _normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value if value.strip() else None


class CreateTableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    table_type: TableType = "comparison"
    title: str = Field(min_length=1, max_length=200)
    subtitle: str = Field(default="", max_length=300)
    columns: list[CreateTableColumn] = Field(min_length=1, max_length=16)
    rows: list[CreateTableRow] = Field(min_length=1, max_length=50)
    replaces_table_id: str | None = Field(default=None, min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=300)

    @field_validator("title", "rationale")
    @classmethod
    def _nonblank_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Field must not be blank.")
        return value

    @model_validator(mode="after")
    def _validate_structure(self) -> CreateTableRequest:
        col_keys = [col.key for col in self.columns]
        if len(col_keys) != len(set(col_keys)):
            raise ValueError("Table column keys must be distinct.")
        col_key_set = set(col_keys)
        for idx, row in enumerate(self.rows):
            unknown = [key for key in row.cells if key not in col_key_set]
            if unknown:
                raise ValueError(f"Row {idx} references undefined column keys: {', '.join(unknown)}.")
        return self


class TableProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str | None = None
    calculation_id: str | None = None
    source_id: str
    character_start: int | None = Field(default=None, ge=0)
    character_end: int | None = Field(default=None, gt=0)
    unit: str
    currency: str | None = None
    period: str
    input_fact_ids: list[str] = Field(default_factory=list)


class TableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    unit: str | None = None
    currency: str | None = None
    scale: str | None = None
    align: TableAlign = "left"


class TableCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["fact", "calculation", "text", "empty"]
    value: str | None = None
    display_value: str
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    provenance: TableProvenance | None = None


class TableRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = None
    cells: dict[str, TableCell]


class ResearchTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_id: str
    turn_id: str | None = None
    table_type: TableType
    title: str
    subtitle: str = ""
    columns: list[TableColumn]
    rows: list[TableRow]
    is_simulated: bool = False
    replaces_table_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    markdown: str = ""


@dataclass
class _ResolvedNumericCell:
    row_idx: int
    col_key: str
    kind: Literal["fact", "calculation"]
    value: Decimal
    unit_spec: UnitScaleSpec
    raw_unit: str
    currency: str | None
    period: str
    provenance: TableProvenance
    cell_unit_pref: str | None


def _to_decimal(raw: Any, *, what: str) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise TableBuildError(f"Invalid numeric value for {what}.")
    try:
        val = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise TableBuildError(f"Invalid numeric value for {what}.") from error
    if not val.is_finite():
        raise TableBuildError(f"Non-finite numeric value for {what}.")
    return val


def _format_decimal_plain(value: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = 100
        if value == 0:
            return "0"
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"


def _format_grouped(plain: str) -> str:
    sign = ""
    body = plain
    if body.startswith("-"):
        sign = "-"
        body = body[1:]
    if "." in body:
        whole, frac = body.split(".", 1)
        return f"{sign}{int(whole):,}.{frac}"
    return f"{sign}{int(body):,}"


def _format_cell_display(
    value: Decimal,
    spec: UnitScaleSpec,
    unit_label: str,
    currency: str | None,
) -> str:
    grouped = _format_grouped(_format_decimal_plain(value))
    if spec.family == "percent":
        return f"{grouped}%"
    if spec.family == "percentage_points":
        return f"{grouped} pp"
    if spec.family == "bps":
        return f"{grouped} bps"
    if spec.family == "monetary":
        if spec.is_chinese_yuan:
            prefix = f"{currency} " if currency and currency not in unit_label else ""
            return f"{prefix}{grouped} {spec.scale_name}" if prefix else f"{grouped} {spec.scale_name}"
        if currency:
            return f"{currency} {grouped} {spec.scale_name}" if spec.scale_name != "ones" else f"{currency} {grouped}"
        return f"{grouped} {spec.scale_name}" if spec.scale_name != "ones" else grouped
    if unit_label in ("ratio", "multiple", "x"):
        return f"{grouped}x" if unit_label == "x" else f"{grouped} {unit_label}"
    return f"{grouped} {unit_label}" if unit_label else grouped


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


def _validate_source(
    source_id: str,
    sources: Mapping[str, ResearchSource],
    as_of: datetime | None,
    *,
    allow_simulated: bool,
) -> ResearchSource:
    source = sources.get(source_id)
    if source is None:
        raise TableBuildError(f"Unknown source_id {source_id!r}.")
    if not source.permissions.display:
        raise TableBuildError(f"Source {source_id!r} is not permitted for display.")
    check_target = (
        source.model_copy(update={"is_simulated": False})
        if (allow_simulated and source.is_simulated)
        else source
    )
    code = source_eligibility([check_target], as_of)[0][1]
    if code != "eligible":
        raise TableBuildError(f"Source {source_id!r} is not eligible for table rendering ({code}).")
    return source


def _parse_unit_for_table(unit: str, currency: str | None) -> UnitScaleSpec:
    try:
        return parse_unit_scale(unit, currency)
    except ValueError as error:
        raise TableBuildError(str(error)) from error


def _derive_calc_period(calc: Mapping[str, Any], input_facts: list[Mapping[str, Any]]) -> str:
    periods = [str(f.get("period") or "").strip() for f in input_facts if str(f.get("period") or "").strip()]
    if not periods:
        raise TableBuildError(f"Calculation {calc.get('calculation_id')!r} has no period provenance.")
    distinct = list(dict.fromkeys(periods))
    if len(distinct) == 1:
        return distinct[0]
    formula = str(calc.get("formula_id") or "")
    if formula in ("growth_percent", "cagr") and len(periods) == 2:
        return f"{periods[1]} -> {periods[0]}"
    if formula == "ttm_sum":
        return f"TTM ({' + '.join(distinct)})"
    return ", ".join(distinct)


def _scale_value(value: Decimal, source_exp: int, target_exp: int) -> Decimal:
    if source_exp == target_exp:
        return value
    with localcontext() as ctx:
        ctx.prec = 100
        return value.scaleb(source_exp - target_exp)


def _pick_auto_scale_spec(specs: list[UnitScaleSpec], values: list[Decimal], currency: str | None) -> UnitScaleSpec:
    exponents = {s.exponent for s in specs}
    if len(exponents) == 1 and specs[0].exponent != 0:
        return specs[0]
    is_yuan = all(s.is_chinese_yuan for s in specs)
    with localcontext() as ctx:
        ctx.prec = 100
        max_ones = max((abs(v.scaleb(s.exponent)) for v, s in zip(values, specs)), default=Decimal(0))
    if is_yuan:
        if max_ones >= Decimal("1000000000000"):
            exp, name = 12, "万亿元"
        elif max_ones >= Decimal("100000000"):
            exp, name = 8, "亿元"
        elif max_ones >= Decimal("10000"):
            exp, name = 4, "万元"
        elif max_ones >= Decimal("1000"):
            exp, name = 3, "千元"
        else:
            exp, name = 0, "元"
        canonical = f"{currency} {name}" if currency else name
        return UnitScaleSpec("monetary", currency, name, exp, canonical, is_chinese_yuan=True)
    if max_ones >= Decimal("1000000000000"):
        exp, name = 12, "trillions"
    elif max_ones >= Decimal("1000000000"):
        exp, name = 9, "billions"
    elif max_ones >= Decimal("1000000"):
        exp, name = 6, "millions"
    elif max_ones >= Decimal("10000"):
        exp, name = 3, "thousands"
    else:
        exp, name = 0, "ones"
    canonical = (f"{currency} {name}" if currency and name != "ones" else (currency or name))
    return UnitScaleSpec("monetary", currency, name, exp, canonical, is_chinese_yuan=False)


def _escape_gfm_cell(text: str) -> str:
    return " ".join(text.replace("|", "\\|").splitlines()).strip()


def _render_markdown(columns: list[TableColumn], rows: list[TableRow], has_row_labels: bool) -> str:
    headers: list[str] = (["Metric"] if has_row_labels else []) + [col.label for col in columns]
    align_tokens: list[str] = ([":---"] if has_row_labels else []) + [
        "---:" if col.align == "right" else (":---:" if col.align == "center" else ":---")
        for col in columns
    ]
    lines = [
        "| " + " | ".join(_escape_gfm_cell(h) for h in headers) + " |",
        "| " + " | ".join(align_tokens) + " |",
    ]
    for row in rows:
        vals: list[str] = ([_escape_gfm_cell(row.label or "")] if has_row_labels else [])
        for col in columns:
            cell = row.cells.get(col.key)
            vals.append(_escape_gfm_cell(cell.display_value if cell is not None else "—"))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def build_research_table(
    request: CreateTableRequest,
    *,
    facts: list[Mapping[str, Any]],
    calculations: list[Mapping[str, Any]],
    sources: Mapping[str, ResearchSource],
    existing_tables: list[Mapping[str, Any]] | None = None,
    as_of: datetime | None = None,
    allow_simulated: bool = False,
) -> ResearchTable:
    """Validate every provenance/unit/permission rule and deterministically build a ResearchTable."""
    if request.replaces_table_id is not None:
        known_ids = {
            item.get("table_id")
            for item in (existing_tables or [])
            if isinstance(item, Mapping) and isinstance(item.get("table_id"), str)
        }
        if request.replaces_table_id not in known_ids:
            raise TableBuildError(f"Unknown replaces_table_id {request.replaces_table_id!r}.")

    fact_by_id: dict[str, Mapping[str, Any]] = {}
    for row in facts:
        if isinstance(row, Mapping) and isinstance(row.get("fact_id"), str):
            fact_by_id[row["fact_id"]] = row

    calc_by_id: dict[str, Mapping[str, Any]] = {}
    for row in calculations:
        if isinstance(row, Mapping) and isinstance(row.get("calculation_id"), str):
            calc_by_id[row["calculation_id"]] = row

    used_sources: dict[str, ResearchSource] = {}
    resolved_numeric: dict[tuple[int, str], _ResolvedNumericCell] = {}

    for row_idx, row_req in enumerate(request.rows):
        for col_key, cell_req in row_req.cells.items():
            if isinstance(cell_req, FactTableCell):
                fact = fact_by_id.get(cell_req.fact_id)
                if fact is None:
                    raise TableBuildError(f"Unknown fact_id {cell_req.fact_id!r}.")
                sid = fact.get("source_id")
                if not isinstance(sid, str) or not sid:
                    raise TableBuildError(f"Fact {cell_req.fact_id!r} has no source_id.")
                used_sources[sid] = _validate_source(sid, sources, as_of, allow_simulated=allow_simulated)
                raw_unit = str(fact.get("unit") or "").strip()
                raw_period = str(fact.get("period") or "").strip()
                if not raw_unit or not raw_period:
                    raise TableBuildError(f"Fact {cell_req.fact_id!r} is missing unit or period provenance.")
                raw_curr = fact.get("currency")
                norm_curr = raw_curr.strip().upper() if isinstance(raw_curr, str) and raw_curr.strip() else None
                val = _to_decimal(fact.get("value"), what=f"fact {cell_req.fact_id}")
                spec = _parse_unit_for_table(raw_unit, norm_curr)
                c_start, c_end = _fact_offsets(fact)
                prov = TableProvenance(
                    fact_id=str(fact["fact_id"]),
                    source_id=sid,
                    character_start=c_start,
                    character_end=c_end,
                    unit=raw_unit,
                    currency=norm_curr,
                    period=raw_period,
                    input_fact_ids=[str(fact["fact_id"])],
                )
                resolved_numeric[(row_idx, col_key)] = _ResolvedNumericCell(
                    row_idx=row_idx,
                    col_key=col_key,
                    kind="fact",
                    value=val,
                    unit_spec=spec,
                    raw_unit=raw_unit,
                    currency=norm_curr,
                    period=raw_period,
                    provenance=prov,
                    cell_unit_pref=cell_req.unit_preference,
                )
            elif isinstance(cell_req, CalculationTableCell):
                calc = calc_by_id.get(cell_req.calculation_id)
                if calc is None:
                    raise TableBuildError(f"Unknown calculation_id {cell_req.calculation_id!r}.")
                inputs = calc.get("inputs")
                if not isinstance(inputs, list) or not inputs:
                    raise TableBuildError(f"Calculation {cell_req.calculation_id!r} has no verifiable inputs.")
                input_facts: list[Mapping[str, Any]] = []
                input_fact_ids: list[str] = []
                for inp in inputs:
                    if not isinstance(inp, Mapping) or not isinstance(inp.get("fact_id"), str):
                        raise TableBuildError(f"Calculation {cell_req.calculation_id!r} has an invalid input entry.")
                    f_obj = fact_by_id.get(inp["fact_id"])
                    if f_obj is None:
                        raise TableBuildError(
                            f"Calculation {cell_req.calculation_id!r} references missing fact_id {inp['fact_id']!r}."
                        )
                    sid = f_obj.get("source_id")
                    if not isinstance(sid, str) or not sid:
                        raise TableBuildError(f"Input fact {inp['fact_id']!r} has no source_id.")
                    used_sources[sid] = _validate_source(sid, sources, as_of, allow_simulated=allow_simulated)
                    input_facts.append(f_obj)
                    input_fact_ids.append(str(f_obj["fact_id"]))
                primary = input_facts[0]
                raw_unit = str(calc.get("unit") or "").strip()
                if not raw_unit:
                    raise TableBuildError(f"Calculation {cell_req.calculation_id!r} has no unit.")
                raw_curr = calc.get("currency")
                norm_curr = raw_curr.strip().upper() if isinstance(raw_curr, str) and raw_curr.strip() else None
                val = _to_decimal(calc.get("value"), what=f"calculation {cell_req.calculation_id}")
                spec = _parse_unit_for_table(raw_unit, norm_curr)
                period_str = _derive_calc_period(calc, input_facts)
                c_start, c_end = _fact_offsets(primary)
                prov = TableProvenance(
                    calculation_id=str(calc["calculation_id"]),
                    source_id=str(primary["source_id"]),
                    character_start=c_start,
                    character_end=c_end,
                    unit=raw_unit,
                    currency=norm_curr,
                    period=period_str,
                    input_fact_ids=input_fact_ids,
                )
                resolved_numeric[(row_idx, col_key)] = _ResolvedNumericCell(
                    row_idx=row_idx,
                    col_key=col_key,
                    kind="calculation",
                    value=val,
                    unit_spec=spec,
                    raw_unit=raw_unit,
                    currency=norm_curr,
                    period=period_str,
                    provenance=prov,
                    cell_unit_pref=cell_req.unit_preference,
                )

    # Resolve column metadata & validate column-level unit/currency constraints.
    built_columns: list[TableColumn] = []
    col_target_specs: dict[str, UnitScaleSpec | None] = {}
    scale_notes: list[str] = []

    for col_req in request.columns:
        col_cells = [cell for (_, ck), cell in resolved_numeric.items() if ck == col_req.key]
        req_curr = col_req.currency.strip().upper() if col_req.currency else None
        target_spec: UnitScaleSpec | None = None

        if req_curr is not None:
            for cell in col_cells:
                if cell.unit_spec.currency != req_curr:
                    raise TableBuildError(
                        f"Column {col_req.key!r} declares currency {req_curr!r}, incompatible with {cell.unit_spec.currency!r}."
                    )

        if col_req.unit_preference is not None:
            pref_spec = _parse_unit_for_table(col_req.unit_preference, req_curr)
            if not col_cells:
                target_spec = pref_spec
            else:
                for cell in col_cells:
                    if cell.unit_spec.family != pref_spec.family:
                        raise TableBuildError(
                            f"Column {col_req.key!r} unit_preference {col_req.unit_preference!r} is incompatible with {cell.raw_unit!r}."
                        )
                    if pref_spec.currency is not None and cell.unit_spec.currency != pref_spec.currency:
                        raise TableBuildError(
                            f"Column {col_req.key!r} currency {pref_spec.currency!r} conflicts with {cell.unit_spec.currency!r}."
                        )
                currencies = {c.unit_spec.currency for c in col_cells}
                if len(currencies) != 1:
                    raise TableBuildError(f"Column {col_req.key!r} mixes inconsistent currencies.")
                resolved_curr = next(iter(currencies))
                if pref_spec.family == "monetary":
                    target_spec = UnitScaleSpec(
                        "monetary",
                        resolved_curr,
                        pref_spec.scale_name,
                        pref_spec.exponent,
                        pref_spec.canonical_unit if resolved_curr is None else (
                            f"{resolved_curr} {pref_spec.scale_name}" if pref_spec.scale_name != "ones" else resolved_curr
                        ),
                        is_chinese_yuan=pref_spec.is_chinese_yuan,
                    )
                    if {c.unit_spec.exponent for c in col_cells} != {target_spec.exponent}:
                        scale_notes.append(f"{col_req.label}: {target_spec.canonical_unit}")
                else:
                    target_spec = pref_spec
        elif col_cells:
            monetary_currencies = {c.unit_spec.currency for c in col_cells if c.unit_spec.family == "monetary"}
            if len(monetary_currencies) > 1:
                raise TableBuildError(
                    f"Column {col_req.key!r} cannot mix currencies ({', '.join(sorted(str(c) for c in monetary_currencies))})."
                )
            families = {c.unit_spec.family for c in col_cells}
            currencies = {c.unit_spec.currency for c in col_cells}
            if families == {"monetary"} and len(currencies) == 1:
                resolved_curr = next(iter(currencies))
                target_spec = _pick_auto_scale_spec(
                    [c.unit_spec for c in col_cells],
                    [c.value for c in col_cells],
                    resolved_curr,
                )
                if len({c.unit_spec.exponent for c in col_cells}) > 1:
                    scale_notes.append(f"{col_req.label}: {target_spec.canonical_unit}")
            elif families == {"monetary"} and len(currencies) > 1 and None in currencies:
                raise TableBuildError(f"Column {col_req.key!r} mixes currency-specified and unspecified monetary facts.")
            elif len(families) == 1 and len(currencies) == 1:
                canon_units = {c.unit_spec.canonical_unit for c in col_cells}
                if len(canon_units) == 1:
                    target_spec = col_cells[0].unit_spec

        col_target_specs[col_req.key] = target_spec
        resolved_align: TableAlign = col_req.align or ("right" if col_cells else "left")
        built_columns.append(
            TableColumn(
                key=col_req.key,
                label=col_req.label,
                unit=target_spec.canonical_unit if target_spec else None,
                currency=target_spec.currency if target_spec else req_curr,
                scale=target_spec.scale_name if (target_spec and target_spec.family in ("monetary", "shares")) else None,
                align=resolved_align,
            )
        )

    built_rows: list[TableRow] = []
    for row_idx, row_req in enumerate(request.rows):
        row_cells: dict[str, TableCell] = {}
        for col_req in request.columns:
            cell_req = row_req.cells.get(col_req.key)
            if cell_req is None:
                row_cells[col_req.key] = TableCell(kind="empty", value=None, display_value="—")
                continue
            if isinstance(cell_req, EmptyTableCell):
                row_cells[col_req.key] = TableCell(kind="empty", value=None, display_value=cell_req.display)
                continue
            if isinstance(cell_req, TextTableCell):
                row_cells[col_req.key] = TableCell(kind="text", value=None, display_value=cell_req.text)
                continue

            resolved = resolved_numeric[(row_idx, col_req.key)]
            col_spec = col_target_specs[col_req.key]
            target_spec = col_spec
            if resolved.cell_unit_pref is not None:
                pref_spec = _parse_unit_for_table(resolved.cell_unit_pref, resolved.unit_spec.currency)
                if pref_spec.family != resolved.unit_spec.family or (
                    pref_spec.currency is not None and pref_spec.currency != resolved.unit_spec.currency
                ):
                    raise TableBuildError(
                        f"Cell unit_preference {resolved.cell_unit_pref!r} is incompatible with {resolved.raw_unit!r}."
                    )
                target_spec = pref_spec

            if target_spec is not None and resolved.unit_spec.family in ("monetary", "shares"):
                scaled_val = _scale_value(resolved.value, resolved.unit_spec.exponent, target_spec.exponent)
                unit_out = target_spec.canonical_unit
                disp_spec = target_spec
            else:
                scaled_val = resolved.value
                unit_out = resolved.raw_unit
                disp_spec = resolved.unit_spec

            plain_val = _format_decimal_plain(scaled_val)
            disp_val = _format_cell_display(scaled_val, disp_spec, unit_out, resolved.currency)
            row_cells[col_req.key] = TableCell(
                kind=resolved.kind,
                value=plain_val,
                display_value=disp_val,
                unit=unit_out,
                currency=resolved.currency,
                period=resolved.period,
                provenance=resolved.provenance,
            )
        built_rows.append(TableRow(label=row_req.label, cells=row_cells))

    is_simulated = any(src.is_simulated for src in used_sources.values())
    subtitle_parts: list[str] = []
    if request.subtitle:
        subtitle_parts.append(request.subtitle)
    if scale_notes:
        subtitle_parts.append("统一量级：" + "；".join(scale_notes))
    if as_of is not None:
        subtitle_parts.append(f"截至 {as_of.date().isoformat()}")
    if is_simulated:
        subtitle_parts.append("【模拟数据】")

    has_row_labels = any(r.label is not None for r in built_rows)
    markdown = _render_markdown(built_columns, built_rows, has_row_labels)

    return ResearchTable(
        table_id="table-" + uuid4().hex,
        table_type=request.table_type,
        title=request.title,
        subtitle=" · ".join(subtitle_parts),
        columns=built_columns,
        rows=built_rows,
        is_simulated=is_simulated,
        replaces_table_id=request.replaces_table_id,
        source_ids=sorted(used_sources),
        markdown=markdown,
    )
