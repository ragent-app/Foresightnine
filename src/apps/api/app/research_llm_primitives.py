"""Shared LLM citation-grounding and structured-JSON repair primitives.

Extracted so Direction A (Earnings Research Squad), V1 (SEC Event Analyst), and future
vertical engines share exact quotation verification (including same-space negation/exception
context scanning) and schema-repair loops without circular imports or coupling to
``research_earnings_squad.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Literal, Mapping, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)

DEFAULT_NEGATION_EXCEPTION_MARKERS: tuple[str, ...] = (
    "however",
    "except",
    "unless",
    "provided that",
    "subject to",
    "not expected to have a material",
    "no material impact",
    "not material",
    "no disagreement",
    "no disagreements",
    "did not result from any disagreement",
    "not the result of any disagreement",
    "routine",
    "previously announced",
    "previously disclosed",
    "preliminary",
    "cannot currently estimate",
    "unable to estimate",
)


@dataclass(frozen=True)
class QuotationMatch:
    """Located match of a quotation within a specific normalized text space."""

    start: int
    end: int
    match_space: Literal["raw", "norm_text", "norm_table"]
    matched_text: str
    space_text: str


def norm_quote_text(text: str) -> str:
    """Fold smart quotes, dashes, NBSP, currency spacing, and collapse whitespace."""
    folded = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
        .replace("\u00a0", " ")
    )
    folded = re.sub(r"([\$€£¥])\s+(?=\d)", r"\1", folded)
    return re.sub(r"\s+", " ", folded).strip().casefold()


def norm_quote_table_tolerant(text: str) -> str:
    """Normalize 8-K/10-Q HTML table formatting (pipes, markdown bold, punctuation spacing)
    while preserving exact word and digit sequence for strict anti-hallucination verification."""
    folded = norm_quote_text(text)
    folded = folded.replace("|", " ").replace("**", " ").replace("__", " ")
    folded = re.sub(r"\s*([,;:%()\-/])\s*", r" \1 ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def find_quotation_span(quotation: str, src_text: str) -> QuotationMatch | None:
    """Locate ``quotation`` in ``src_text`` and return span in the matched text space.

    Always slices negation/exception context in the returned ``match_space`` so
    normalization length changes never cause character-offset drift.
    """
    if not quotation or not src_text:
        return None
    idx = src_text.find(quotation)
    if idx != -1:
        return QuotationMatch(
            start=idx,
            end=idx + len(quotation),
            match_space="raw",
            matched_text=quotation,
            space_text=src_text,
        )
    q_norm = norm_quote_text(quotation)
    if q_norm:
        s_norm = norm_quote_text(src_text)
        idx = s_norm.find(q_norm)
        if idx != -1:
            return QuotationMatch(
                start=idx,
                end=idx + len(q_norm),
                match_space="norm_text",
                matched_text=q_norm,
                space_text=s_norm,
            )
    q_tab = norm_quote_table_tolerant(quotation)
    if len(q_tab) >= 8:
        s_tab = norm_quote_table_tolerant(src_text)
        idx = s_tab.find(q_tab)
        if idx != -1:
            return QuotationMatch(
                start=idx,
                end=idx + len(q_tab),
                match_space="norm_table",
                matched_text=q_tab,
                space_text=s_tab,
            )
    return None


def quotation_matches_source(quotation: str, src_text: str) -> bool:
    """Boolean compatibility wrapper around ``find_quotation_span``."""
    return find_quotation_span(quotation, src_text) is not None


def check_negation_context(
    match: QuotationMatch,
    *,
    before_chars: int = 80,
    after_chars: int = 200,
    markers: Sequence[str] | None = None,
) -> tuple[str | None, str | None]:
    """Scan surrounding context in the SAME ``match_space`` for qualifying/negation clauses.

    Returns ``(warning_message, surrounding_snippet)`` or ``(None, None)``.
    """
    active_markers = tuple(markers) if markers is not None else DEFAULT_NEGATION_EXCEPTION_MARKERS
    before_start = max(0, match.start - max(0, before_chars))
    after_end = min(len(match.space_text), match.end + max(0, after_chars))
    before_slice = match.space_text[before_start:match.start]
    after_slice = match.space_text[match.end:after_end]
    combined_low = (before_slice + " " + after_slice).casefold()
    quote_low = match.matched_text.casefold()

    hit_markers = [
        m for m in active_markers
        if m.casefold() in combined_low and m.casefold() not in quote_low
    ]
    if not hit_markers:
        return None, None
    snippet = match.space_text[before_start:after_end].strip()
    warning = (
        f"Surrounding source text contains qualifying/negation clause(s) omitted from quote: "
        f"{', '.join(hit_markers[:4])}."
    )
    return warning, snippet[:500]


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extract the first valid JSON object from raw LLM output (handling markdown fences)."""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Model returned empty content; expected a JSON object.")
    # Strip fenced ```json ... ``` blocks if present
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        parsed = json.loads(text[first : last + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Could not locate a valid JSON object in model output.")


def format_pydantic_repair_prompt(
    model_cls: type[BaseModel],
    error: Exception,
    *,
    attempt: int,
    max_attempts: int,
) -> str:
    """Build a focused schema-repair prompt showing exact Pydantic validation errors."""
    schema_name = model_cls.__name__
    err_text = str(error)[:1200]
    return (
        f"SCHEMA VALIDATION REPAIR (attempt {attempt}/{max_attempts}): "
        f"Your previous response did not match `{schema_name}` (extra='forbid').\n"
        f"Validation error:\n{err_text}\n\n"
        "Fix ONLY the invalid/missing/extra fields and reply with a single raw JSON object "
        f"strictly conforming to `{schema_name}`. Do not wrap in commentary."
    )


def invoke_and_parse_with_repair(
    *,
    invoke_fn: Callable[[list[dict[str, Any]], int], Any],
    model_cls: type[ModelT],
    system_prompt: str,
    user_prompt: str,
    max_repairs: int = 2,
    normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    on_repair_attempt: Callable[[int, Exception], None] | None = None,
) -> tuple[ModelT, int]:
    """Generic, Squad-independent JSON invocation + Pydantic validation + bounded repair loop.

    ``invoke_fn(messages, attempt_idx)`` returns an object with ``.content`` (str | None)
    and optional ``.parsed`` (Mapping | None).
    Returns ``(validated_model, repair_attempts_used)``.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_err: Exception | None = None
    total_attempts = max(0, int(max_repairs)) + 1

    for attempt_idx in range(total_attempts):
        resp = invoke_fn(list(messages), attempt_idx)
        try:
            parsed_attr = getattr(resp, "parsed", None)
            if isinstance(parsed_attr, Mapping):
                raw_dict = dict(parsed_attr)
            else:
                raw_dict = extract_json_object(str(getattr(resp, "content", "") or ""))
            if normalizer is not None:
                raw_dict = normalizer(raw_dict)
            return model_cls.model_validate(raw_dict), attempt_idx
        except (ValidationError, ValueError, TypeError) as exc:
            last_err = exc
            if attempt_idx >= total_attempts - 1:
                raise
            if on_repair_attempt is not None:
                on_repair_attempt(attempt_idx + 1, exc)
            prior_raw = str(getattr(resp, "content", "") or "")
            if not prior_raw and isinstance(getattr(resp, "parsed", None), Mapping):
                prior_raw = json.dumps(resp.parsed, ensure_ascii=False, default=str)
            messages.append({"role": "assistant", "content": prior_raw[:3000] or "<invalid output>"})
            messages.append({
                "role": "user",
                "content": format_pydantic_repair_prompt(
                    model_cls,
                    exc,
                    attempt=attempt_idx + 1,
                    max_attempts=max(1, int(max_repairs)),
                ),
            })

    assert last_err is not None
    raise last_err
