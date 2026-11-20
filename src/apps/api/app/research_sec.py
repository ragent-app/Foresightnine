"""Bounded SEC company-concept retrieval; no credentials in responses."""
from __future__ import annotations

import json
import base64
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from threading import Event, Thread
from time import monotonic, sleep
from typing import Callable, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .openai_compatible import HttpResponse, _TransportFailure, _TransportTimeout, _default_transport
from .research_sources import SourcePermissions
from .research_fetch import FetchSourceRequest
from .research_uploads import HtmlUploadRequest, PdfFailure, extract_uploaded_html


class SecConceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    cik: str = Field(pattern=r"^\d{1,10}$")
    taxonomy: Literal["us-gaap", "ifrs-full", "dei"] = "us-gaap"
    tag: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    unit: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    filing_before: date | None = None
    max_records: int = Field(default=30, ge=1, le=100)
    permissions: SourcePermissions

    @field_validator("cik")
    @classmethod
    def nonzero_cik(cls, value: str) -> str:
        if int(value) == 0:
            raise ValueError("CIK must be nonzero.")
        return value.zfill(10)


class SecIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=100)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Query must not be blank.")
        return value


class SecFailure(ValueError):
    def __init__(self, code: str, available_units: list[str] | None = None):
        super().__init__(code)
        self.code = code
        self.available_units = available_units


@dataclass(frozen=True)
class SecResult:
    url: str
    title: str
    text: str
    response_sha256: str


@dataclass(frozen=True)
class SecFilingResult:
    text: str
    response_sha256: str
    original_base64: str


def fetch_sec_filing(request: FetchSourceRequest, user_agent: str | None,
                     transport: Callable[..., HttpResponse] | None = None, *,
                     timeout_seconds: float = 15.0) -> SecFilingResult:
    """Retrieve one validated SEC HTML filing, preserving the exact UTF-8 body."""
    if not request.permissions.store:
        raise SecFailure("source_permission_unknown")
    if not 0 < timeout_seconds <= 15:
        raise SecFailure("source_timeout")
    if urlsplit(str(request.url)).hostname != "www.sec.gov":
        raise SecFailure("source_invalid")
    agent = validate_user_agent(user_agent)
    deadline = monotonic() + timeout_seconds
    send = transport or (lambda method, address, headers, body, timeout:
                         _default_transport(method, address, headers, body, timeout,
                                            max_response_bytes=5_000_000))
    try:
        response = send("GET", str(request.url), {"User-Agent": agent, "Accept": "text/html"}, b"", timeout_seconds)
    except _TransportTimeout as error:
        raise SecFailure("source_timeout") from error
    except (_TransportFailure, OSError) as error:
        raise SecFailure("source_unavailable") from error
    if response.status_code == 429:
        raise SecFailure("source_rate_limited")
    if not 200 <= response.status_code < 300:
        raise SecFailure("source_unavailable")
    if response.headers.get("content-type", "").split(";", 1)[0].lower().strip() != "text/html":
        raise SecFailure("source_format_unsupported")
    raw = response.body.encode("utf-8")
    if len(raw) > 5_000_000:
        raise SecFailure("source_invalid")
    encoded = base64.b64encode(raw).decode("ascii")
    try:
        upload = HtmlUploadRequest(title=request.title, html_base64=encoded, permissions=request.permissions)
    except ValidationError as error:
        raise SecFailure("source_invalid") from error
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise SecFailure("source_timeout")
    done = Event()
    state: dict[str, object] = {}
    def parse() -> None:
        try:
            state["result"] = extract_uploaded_html(upload)
        except Exception as error:
            state["error"] = error
        finally:
            done.set()
    Thread(target=parse, name="foresight-sec-html", daemon=True).start()
    if not done.wait(remaining):
        # HTML extraction uses a shared bounded semaphore. A timed-out parse
        # may finish later but cannot submit another external request.
        raise SecFailure("source_timeout")
    error = state.get("error")
    if error is not None:
        raise SecFailure("source_unavailable" if isinstance(error, PdfFailure) and error.code == "html_busy" else "source_invalid") from error
    result = state.get("result")
    if not isinstance(result, tuple) or len(result) != 2:
        raise SecFailure("source_invalid")
    text, raw_hash = result
    return SecFilingResult(text=text, response_sha256=raw_hash, original_base64=encoded)


def search_sec_identities(request: SecIdentityRequest, user_agent: str | None,
                          transport: Callable[..., HttpResponse] | None = None) -> dict:
    """Return current issuer candidates; ticker is not a unique security ID."""
    agent = validate_user_agent(user_agent)
    url = "https://www.sec.gov/files/company_tickers.json"
    send = transport or (lambda method, address, headers, body, timeout:
                         _default_transport(method, address, headers, body, timeout,
                                            max_response_bytes=2 * 1024 * 1024))
    try:
        response = send("GET", url, {"User-Agent": agent, "Accept": "application/json"}, b"", 15.0)
    except _TransportTimeout as error:
        raise SecFailure("source_timeout") from error
    except (_TransportFailure, OSError) as error:
        raise SecFailure("source_unavailable") from error
    if response.status_code == 429:
        raise SecFailure("source_rate_limited")
    if not 200 <= response.status_code < 300:
        raise SecFailure("source_unavailable")
    if response.headers.get("content-type", "").split(";", 1)[0].lower().strip() != "application/json":
        raise SecFailure("source_format_unsupported")
    if len(response.body.encode("utf-8")) > 2 * 1024 * 1024:
        raise SecFailure("source_invalid")
    try:
        payload = json.loads(response.body)
        if not isinstance(payload, dict) or len(payload) > 50000:
            raise ValueError("Invalid SEC ticker index")
        query = request.query.casefold()
        matches = []
        valid_records = 0
        for item in payload.values():
            if not isinstance(item, dict):
                continue
            cik, ticker, name = item.get("cik_str"), item.get("ticker"), item.get("title")
            if (isinstance(cik, bool) or not isinstance(cik, int) or not 0 < cik < 10**10 or
                    not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z0-9.\-]{1,20}", ticker) or
                    not isinstance(name, str) or not name.strip() or len(name) > 300):
                continue
            valid_records += 1
            ticker_match, name_match = ticker.casefold(), name.casefold()
            if query not in ticker_match and query not in name_match and query != str(cik):
                continue
            rank = 0 if query == ticker_match or query == str(cik) else 1 if name_match.startswith(query) else 2
            matches.append((rank, ticker_match, cik, {"cik": str(cik).zfill(10), "ticker": ticker,
                                                      "name": name, "exchange": None}))
        if not valid_records:
            raise ValueError("SEC ticker index has no valid entries")
        matches.sort(key=lambda item: (item[0], item[1], item[2]))
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise SecFailure("source_invalid") from error
    return {"candidates": [item[3] for item in matches[:10]], "source_url": url,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "response_sha256": sha256(response.body.encode("utf-8")).hexdigest(),
            "limitation": "Current SEC ticker mapping only; exchange, security class and historical validity are not established."}


def validate_user_agent(value: str | None) -> str:
    # The deployment operator must provide their own real contact; never invent one.
    if not value or len(value) > 200 or "\r" in value or "\n" in value or not re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", value):
        raise SecFailure("sec_contact_unconfigured")
    return value


def fetch_sec_concept(request: SecConceptRequest, user_agent: str | None,
                      transport: Callable[..., HttpResponse] = _default_transport) -> SecResult:
    if not request.permissions.store:
        raise SecFailure("source_permission_unknown")
    agent = validate_user_agent(user_agent)
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{request.cik}/{request.taxonomy}/{request.tag}.json"
    try:
        response = transport("GET", url, {"User-Agent": agent, "Accept": "application/json"}, b"", 15.0)
    except _TransportTimeout as error:
        raise SecFailure("source_timeout") from error
    except (_TransportFailure, OSError) as error:
        raise SecFailure("source_unavailable") from error
    if response.status_code == 429:
        raise SecFailure("source_rate_limited")
    if response.status_code == 404:
        raise SecFailure("source_tag_not_found")
    if not 200 <= response.status_code < 300:
        raise SecFailure("source_unavailable")
    if response.headers.get("content-type", "").split(";", 1)[0].lower().strip() != "application/json":
        raise SecFailure("source_format_unsupported")
    try:
        # XBRL decimals must not pass through binary floating point: even
        # ordinary JSON decimals can otherwise be silently rounded before
        # they become source text and evidence.
        payload = json.loads(response.body, parse_float=Decimal)
        if not isinstance(payload, dict) or int(payload["cik"]) != int(request.cik):
            raise ValueError("CIK mismatch")
        if payload.get("taxonomy") != request.taxonomy or payload.get("tag") != request.tag:
            raise ValueError("Concept mismatch")
        units = payload["units"]
        if not isinstance(units, dict):
            raise ValueError("Invalid units")
        entity = payload.get("entityName", "Unknown entity")
        label = payload.get("label", request.tag)
        if not isinstance(entity, str) or not isinstance(label, str):
            raise ValueError("Invalid metadata")
        records = []
        unsupported_precision = 0
        for unit, entries in units.items():
            if not isinstance(unit, str) or not isinstance(entries, list) or (request.unit is not None and unit != request.unit):
                continue
            for item in entries:
                if not isinstance(item, dict) or not isinstance(item.get("filed"), str):
                    continue
                filed = date.fromisoformat(item["filed"])
                if request.filing_before is not None and filed >= request.filing_before:
                    continue
                value = item.get("val")
                if (not isinstance(value, (int, float, Decimal)) or isinstance(value, bool) or
                        (isinstance(value, Decimal) and not value.is_finite()) or
                        (isinstance(value, float) and not math.isfinite(value))):
                    continue
                if isinstance(value, Decimal) and (len(value.as_tuple().digits) > 1000 or abs(value.as_tuple().exponent) > 1000):
                    unsupported_precision += 1
                    continue
                records.append((filed, str(item.get("end", "")), unit, item))
        records.sort(key=lambda item: (item[0], item[1]), reverse=True)
    except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation, json.JSONDecodeError) as error:
        raise SecFailure("source_invalid") from error
    if not records:
        reported = sorted(name for name, entries in units.items() if isinstance(name, str) and isinstance(entries, list) and entries)
        raise SecFailure("source_no_matching_facts", reported or None)
    lines = [f"SEC company concept: {entity}; CIK {request.cik}; taxonomy {request.taxonomy}; tag {request.tag}; label {label}.",
             f"Most recent {min(len(records), request.max_records)} of {len(records)} matching observations; filing date is not exact public availability time."]
    if unsupported_precision:
        lines.append(f"{unsupported_precision} observations exceeded supported numeric precision and were omitted.")
    for filed, _end, unit, item in records[:request.max_records]:
        fields = {key: item.get(key) for key in ("val", "start", "end", "fy", "fp", "form", "filed", "accn", "frame") if item.get(key) is not None}
        fields["unit"] = unit
        # Serialize Decimal values as JSON numbers, not quoted strings, and
        # preserve their exact decimal value. The remaining upstream metadata
        # is informational and cannot supply raw JSON syntax.
        lines.append("{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" +
            (format(fields[key], "f") if key == "val" and isinstance(fields[key], Decimal)
             else json.dumps(fields[key], ensure_ascii=False, separators=(",", ":"), default=str))
            for key in sorted(fields)) + "}")
    return SecResult(url=url, title=f"{entity}: {label}", text="\n".join(lines),
                     response_sha256=sha256(response.body.encode("utf-8")).hexdigest())


def reserve_sec_request(repository, now: datetime | None = None) -> bool:
    """Reserve a global one-second slot using the shared repository lock."""
    current = now or datetime.now(timezone.utc)
    def reserve() -> bool:
        previous = repository.get("sec")
        if previous:
            last = datetime.fromisoformat(previous["requested_at"])
            if (current - last).total_seconds() < 1:
                return False
        repository.put("sec", {"requested_at": current.isoformat()})
        return True
    return repository.with_advisory_lock("source:sec-global", reserve)


def pace_sec_request(
    repository,
    *,
    cancel_event: Event | None = None,
    deadline: float | None = None,
    now_fn: Callable[[], float] = monotonic,
    utcnow_fn: Callable[[], datetime] | None = None,
) -> bool:
    """Wait up to bounded retries for the global 1-second SEC slot when budget and cancellation allow."""
    utc_now = utcnow_fn or (lambda: datetime.now(timezone.utc))
    for attempt in range(4):
        if cancel_event is not None and cancel_event.is_set():
            return False
        current_utc = utc_now()
        if reserve_sec_request(repository, now=current_utc):
            return True
        if attempt == 3:
            return False
        delay = 0.25
        previous = repository.get("sec")
        if isinstance(previous, dict) and isinstance(previous.get("requested_at"), str):
            try:
                last = datetime.fromisoformat(previous["requested_at"])
                delay = min(1.05, max(0.05, 1.02 - (current_utc - last).total_seconds()))
            except (TypeError, ValueError):
                delay = 0.25
        if deadline is not None and (deadline - now_fn()) < (15.0 + delay):
            return False
        if cancel_event is not None:
            if cancel_event.wait(delay) or cancel_event.is_set():
                return False
        else:
            sleep(delay)
    return False
