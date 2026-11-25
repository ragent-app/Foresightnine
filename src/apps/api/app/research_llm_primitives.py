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


def _fold_quote_char(ch: str) -> str:
    """Fold a single source character into its normalized quote representation."""
    if ch in ("\u2018", "\u2019"):
        return "'"
    if ch in ("\u201c", "\u201d"):
        return '"'
    if ch in ("\u2013", "\u2014", "\u2212"):
        return "-"
    if ch == "\u00a0":
        return " "
    return ch.casefold()


def normalize_with_raw_map(
    raw_text: str,
    mode: Literal["norm_text", "norm_table"] = "norm_text",
) -> tuple[str, list[tuple[int, int]]]:
    """Normalize ``raw_text`` while preserving a per-character ``(raw_start, raw_end)`` map.

    Accurately handles Unicode ligature expansion (e.g. ``ﬁ`` -> ``fi`` via ``casefold``),
    collapsed whitespace runs, currency-gap removal, and ``norm_table`` punctuation padding
    so matched spans in normalized space always project back to exact raw character offsets.
    """
    if not raw_text:
        return "", []

    # Pass 1: per-character folding (preserving raw spans for every emitted char)
    folded_chars: list[str] = []
    folded_spans: list[tuple[int, int]] = []
    for raw_idx, ch in enumerate(raw_text):
        rep = _fold_quote_char(ch)
        if mode == "norm_table" and rep == "|":
            rep = " "
        for sub_ch in rep:
            folded_chars.append(sub_ch)
            folded_spans.append((raw_idx, raw_idx + 1))

    # Pass 2: remove "**" and "__" markdown markers in norm_table mode
    if mode == "norm_table":
        cleaned_chars: list[str] = []
        cleaned_spans: list[tuple[int, int]] = []
        i = 0
        n = len(folded_chars)
        while i < n:
            if i + 1 < n and folded_chars[i] in ("*", "_") and folded_chars[i + 1] == folded_chars[i]:
                cleaned_chars.append(" ")
                cleaned_spans.append((folded_spans[i][0], folded_spans[i + 1][1]))
                i += 2
            else:
                cleaned_chars.append(folded_chars[i])
                cleaned_spans.append(folded_spans[i])
                i += 1
        folded_chars, folded_spans = cleaned_chars, cleaned_spans

    # Pass 3: remove whitespace between currency symbol and digit (matching norm_quote_text)
    curr_chars: list[str] = []
    curr_spans: list[tuple[int, int]] = []
    i = 0
    n = len(folded_chars)
    while i < n:
        ch = folded_chars[i]
        curr_chars.append(ch)
        curr_spans.append(folded_spans[i])
        if ch in ("$", "€", "£", "¥"):
            j = i + 1
            while j < n and folded_chars[j].isspace():
                j += 1
            if j > i + 1 and j < n and folded_chars[j].isdigit():
                i = j
                continue
        i += 1

    # Pass 4: in norm_table mode, pad punctuation [,;:%()\-/] with synthetic spaces
    if mode == "norm_table":
        _punct = set(",;:%()-/")
        padded_chars: list[str] = []
        padded_spans: list[tuple[int, int]] = []
        for ch, sp in zip(curr_chars, curr_spans):
            if ch in _punct:
                padded_chars.append(" ")
                padded_spans.append((sp[0], sp[0]))
                padded_chars.append(ch)
                padded_spans.append(sp)
                padded_chars.append(" ")
                padded_spans.append((sp[1], sp[1]))
            else:
                padded_chars.append(ch)
                padded_spans.append(sp)
        curr_chars, curr_spans = padded_chars, padded_spans

    # Pass 5: collapse runs of whitespace into a single space and strip leading/trailing space
    out_chars: list[str] = []
    out_spans: list[tuple[int, int]] = []
    i = 0
    n = len(curr_chars)
    while i < n:
        if curr_chars[i].isspace():
            run_start = curr_spans[i][0]
            j = i + 1
            while j < n and curr_chars[j].isspace():
                j += 1
            run_end = curr_spans[j - 1][1]
            if out_chars and j < n:
                out_chars.append(" ")
                out_spans.append((run_start, run_end))
            i = j
        else:
            out_chars.append(curr_chars[i])
            out_spans.append(curr_spans[i])
            i += 1

    return "".join(out_chars), out_spans


def map_match_to_raw_span(
    match: QuotationMatch,
    raw_text: str,
    *,
    local_window: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Project a ``QuotationMatch`` back to exact ``(raw_start, raw_end)`` offsets in ``raw_text``.

    When ``local_window=(win_start, win_end)`` is provided (e.g. a specific transcript turn
    or PPTX slide span), matching is attempted inside that window first so short numeric
    quotes do not falsely snap to an earlier occurrence on page 1.
    """
    if not raw_text:
        return 0, 0

    def _project_in_slice(slice_text: str, base_offset: int, needle: str, space: str) -> tuple[int, int] | None:
        if space == "raw":
            pos = slice_text.find(needle)
            if pos != -1:
                return base_offset + pos, base_offset + pos + len(needle)
            return None
        mode: Literal["norm_text", "norm_table"] = "norm_table" if space == "norm_table" else "norm_text"
        norm_s, raw_map = normalize_with_raw_map(slice_text, mode)
        if not norm_s or not raw_map:
            return None
        pos = norm_s.find(needle)
        if pos == -1:
            return None
        end_pos = min(len(raw_map), pos + len(needle))
        if end_pos <= pos:
            return None
        # Collect non-empty raw spans within the matched range
        r_starts = [raw_map[k][0] for k in range(pos, end_pos)]
        r_ends = [raw_map[k][1] for k in range(pos, end_pos)]
        r_start = min(r_starts)
        r_end = max(r_ends)
        # Trim any accidental leading/trailing whitespace included by synthetic padding
        while r_start < r_end and slice_text[r_start].isspace():
            r_start += 1
        while r_end > r_start and slice_text[r_end - 1].isspace():
            r_end -= 1
        return base_offset + r_start, base_offset + r_end

    # Try local window first when supplied
    if local_window is not None:
        w_start = max(0, min(len(raw_text), local_window[0]))
        w_end = max(w_start, min(len(raw_text), local_window[1]))
        if w_end > w_start:
            local_slice = raw_text[w_start:w_end]
            local_hit = _project_in_slice(local_slice, w_start, match.matched_text, match.match_space)
            if local_hit is not None:
                return local_hit

    if match.match_space == "raw":
        start = max(0, min(len(raw_text), match.start))
        end = max(start, min(len(raw_text), match.end))
        return start, end

    mode: Literal["norm_text", "norm_table"] = "norm_table" if match.match_space == "norm_table" else "norm_text"
    norm_s, raw_map = normalize_with_raw_map(raw_text, mode)
    if norm_s and raw_map and match.start < len(raw_map):
        # Fast direct projection when match.space_text aligns with normalize_with_raw_map
        if norm_s[match.start : match.end] == match.matched_text:
            pos, end_pos = match.start, min(len(raw_map), match.end)
        else:
            pos = norm_s.find(match.matched_text)
            end_pos = min(len(raw_map), pos + len(match.matched_text)) if pos != -1 else -1
        if pos != -1 and end_pos > pos:
            r_start = min(raw_map[k][0] for k in range(pos, end_pos))
            r_end = max(raw_map[k][1] for k in range(pos, end_pos))
            while r_start < r_end and raw_text[r_start].isspace():
                r_start += 1
            while r_end > r_start and raw_text[r_end - 1].isspace():
                r_end -= 1
            return r_start, r_end

    # Fallback: safe clamped raw bounds
    fallback_start = max(0, min(len(raw_text), match.start))
    fallback_end = max(fallback_start, min(len(raw_text), match.end))
    return fallback_start, fallback_end


def find_quotation_span_with_raw(
    quotation: str,
    raw_text: str,
    *,
    local_window: tuple[int, int] | None = None,
) -> tuple[QuotationMatch | None, int | None, int | None]:
    """Locate ``quotation`` (preferring ``local_window``) and return ``(match, raw_start, raw_end)``."""
    if not quotation or not raw_text:
        return None, None, None
    if local_window is not None:
        w_start = max(0, min(len(raw_text), local_window[0]))
        w_end = max(w_start, min(len(raw_text), local_window[1]))
        if w_end > w_start:
            local_match = find_quotation_span(quotation, raw_text[w_start:w_end])
            if local_match is not None:
                r_start, r_end = map_match_to_raw_span(local_match, raw_text[w_start:w_end])
                return local_match, w_start + r_start, w_start + r_end
    match = find_quotation_span(quotation, raw_text)
    if match is None:
        return None, None, None
    r_start, r_end = map_match_to_raw_span(match, raw_text)
    return match, r_start, r_end


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


def _looks_truncated_json(raw_text: str, finish_reason: str | None) -> bool:
    if isinstance(finish_reason, str) and finish_reason.strip().lower() in ("length", "max_tokens"):
        return True
    s = (raw_text or "").strip()
    if not s:
        return False
    return s.startswith("{") and not s.endswith("}")


def _call_invoke_fn(
    invoke_fn: Callable[..., Any],
    messages: list[dict[str, Any]],
    attempt_idx: int,
    *,
    reset_conversation: bool,
) -> Any:
    if reset_conversation:
        try:
            return invoke_fn(messages, attempt_idx, reset_conversation=True)
        except TypeError:
            pass
    return invoke_fn(messages, attempt_idx)


def invoke_and_parse_with_repair(
    *,
    invoke_fn: Callable[..., Any],
    model_cls: type[ModelT],
    system_prompt: str,
    user_prompt: str,
    max_repairs: int = 2,
    normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    on_repair_attempt: Callable[[int, Exception], None] | None = None,
    compact_retry_instruction: str | None = None,
) -> tuple[ModelT, int]:
    """Generic, Squad-independent JSON invocation + Pydantic validation + bounded repair loop.

    ``invoke_fn(messages, attempt_idx[, reset_conversation=True])`` returns an object with
    ``.content`` (str | None), optional ``.parsed`` (Mapping | None), and optional
    ``.finish_reason`` (str | None). When output truncation is detected
    (``finish_reason == "length"`` / incomplete JSON), switches to compact reset mode
    (``reset_conversation=True``) so stateful runners clear prior truncated turns rather than
    replaying truncated debris.
    Returns ``(validated_model, repair_attempts_used)``.
    """
    base_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    messages: list[dict[str, Any]] = list(base_messages)
    last_err: Exception | None = None
    total_attempts = max(0, int(max_repairs)) + 1
    next_reset = False

    for attempt_idx in range(total_attempts):
        try:
            resp = _call_invoke_fn(invoke_fn, list(messages), attempt_idx, reset_conversation=next_reset)
        except Exception as invoke_exc:
            if getattr(invoke_exc, "code", None) == "provider_output_truncated" and attempt_idx < total_attempts - 1:
                last_err = invoke_exc
                if on_repair_attempt is not None:
                    on_repair_attempt(attempt_idx + 1, invoke_exc)
                compact_note = compact_retry_instruction or (
                    "PREVIOUS RESPONSE WAS TRUNCATED AT MAX OUTPUT TOKENS. "
                    "Keep internal reasoning minimal and immediately emit a COMPACT, strictly valid single JSON object matching "
                    f"`{model_cls.__name__}`: keep at most 8 highest-signal items per list, "
                    "keep verbatim quotations <= 160 characters, and omit redundant prose."
                )
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"{user_prompt}\n\n{compact_note}"},
                ]
                next_reset = True
                continue
            raise
        next_reset = False
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
            finish_reason = getattr(resp, "finish_reason", None)
            if _looks_truncated_json(prior_raw, finish_reason):
                # Reset conversation to single-turn compact retry so _AccountSlotModelRunner
                # does not replay truncated output debris into history.
                compact_note = compact_retry_instruction or (
                    "PREVIOUS RESPONSE WAS TRUNCATED AT MAX OUTPUT TOKENS. "
                    "Re-emit a COMPACT, strictly valid single JSON object matching "
                    f"`{model_cls.__name__}`: keep at most 12 highest-signal items per list, "
                    "keep verbatim quotations <= 200 characters, and omit redundant prose."
                )
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"{user_prompt}\n\n{compact_note}"},
                ]
                next_reset = True
                continue
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
