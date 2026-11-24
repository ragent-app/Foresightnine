"""Financial Modeling Prep (FMP) adapter: consensus estimates, surprises, and call transcripts.

Supports both FMP ``stable`` and legacy ``v3`` endpoints:
- stable:
  - /stable/analyst-estimates?symbol=...&period=...&page=0&limit=...
  - /stable/earnings?symbol=...
  - /stable/earning-call-transcript-dates?symbol=...
  - /stable/earning-call-transcript?symbol=...&year=...&quarter=...
- v3:
  - /api/v3/analyst-estimates/{symbol}?period=...&limit=...
  - /api/v3/earnings-surprises/{symbol}
  - /api/v3/earning_call_transcript/{symbol}[?year=...&quarter=...]

Security & Licensing:
- The FMP API key is sent via the ``apikey`` HTTP header, never in URL query parameters.
- API keys are scrubbed from every error message, URL provenance string, and diagnostic log.
- Strict BYOK by default: the platform environment key (``FORESIGHTNINE_FMP_API_KEY``) is used
  only when ``FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY`` (or ``allow_platform_key=True``) is
  explicitly enabled for personal development use. Third-party/BYOK data sets ``export=False``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import logging
import math
import os
import re
from time import monotonic
from typing import Any, Callable, Iterable, Literal, Mapping
from urllib.parse import quote, urlencode

from ...earnings_external_data import (
    EarningsConsensus,
    EarningsTranscript,
    FMP_ALLOW_PLATFORM_KEY_ENV,
    FMP_API_KEY_ENV,
    FMP_PROVIDER_ID,
    parse_env_bool,
    redact_secrets,
    sanitize_endpoint_url,
)
from ...openai_compatible import _TransportFailure, _TransportTimeout
from ..models import Cost, Coverage, Permissions, Provenance, SuggestedAction, ToolResult
from ..policy import Denied, FinDataPolicy, PolicyRule, load_policy
from ..ratelimit import InProcessRateLimiter, ProviderLimit, RateLimitBackend, default_limiter
from .base import StopWith, Transport, default_transport, rate_limited_result, retry_after, utcnow

logger = logging.getLogger("foresightnine.fin_data.fmp")

PROVIDER = FMP_PROVIDER_ID
BASE = "https://financialmodelingprep.com"
API_KEY_ENV = FMP_API_KEY_ENV
ALLOW_PLATFORM_KEY_ENV = FMP_ALLOW_PLATFORM_KEY_ENV
CATEGORY_CONSENSUS = "consensus"
CATEGORY_TRANSCRIPTS = "transcripts"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 5_000_000
MAX_ESTIMATES_LIMIT = 40
_VALID_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,19}$")

_PLAN_ERROR_KEYWORDS = (
    "legacy endpoint",
    "special endpoint",
    "exclusive endpoint",
    "upgrade",
    "subscription",
    "plan",
    "not available under your current",
    "premium",
)
_RATE_ERROR_KEYWORDS = ("limit reach", "rate limit", "too many requests", "quota")
_KEY_ERROR_KEYWORDS = ("invalid api key", "invalid apikey", "api key is invalid", "unauthorized")
_NOT_FOUND_KEYWORDS = ("not found", "no data", "no transcript")


def _env_positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(env.get(name, ""))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _default_fmp_limiter(env: Mapping[str, str] | None = None) -> RateLimitBackend:
    global_limiter = default_limiter()
    if PROVIDER in getattr(global_limiter, "_limits", {}):
        return global_limiter
    environ = os.environ if env is None else env
    per_min = _env_positive_int(environ, "FORESIGHTNINE_FMP_RATE_PER_MIN", 300)
    daily = _env_positive_int(environ, "FORESIGHTNINE_FMP_DAILY_QUOTA", 250)
    return InProcessRateLimiter(
        {
            PROVIDER: ProviderLimit(
                rate_per_sec=max(1.0, per_min / 60.0),
                burst=float(min(max(per_min, 10), 30)),
                daily_quota=daily,
            )
        }
    )


_MODULE_FMP_LIMITER: RateLimitBackend | None = None


def _get_fmp_limiter() -> RateLimitBackend:
    global _MODULE_FMP_LIMITER
    global_limiter = default_limiter()
    if PROVIDER in getattr(global_limiter, "_limits", {}):
        return global_limiter
    if _MODULE_FMP_LIMITER is None:
        _MODULE_FMP_LIMITER = _default_fmp_limiter()
    return _MODULE_FMP_LIMITER


def _first_present(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _parse_quarter(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 4 else None
    text = str(value).strip().upper()
    if text.startswith("Q") and text[1:].isdigit():
        q = int(text[1:])
        return q if 1 <= q <= 4 else None
    if text.isdigit():
        q = int(text)
        return q if 1 <= q <= 4 else None
    return None


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    head = text[:10]
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def _parse_call_datetime(value: Any) -> datetime | date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 10:
        return _parse_date(text)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return _parse_date(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _derive_fiscal_period(
    row: Mapping[str, Any],
    period_end: date | None,
    period_type: Literal["quarter", "annual"],
) -> tuple[str, int | None, int | None]:
    explicit_year = _int_or_none(_first_present(row, "fiscalYear", "year"))
    explicit_quarter = _parse_quarter(_first_present(row, "fiscalQuarter", "quarter"))
    explicit_period = _first_present(row, "fiscalPeriod")
    raw_period = _first_present(row, "period")

    if isinstance(explicit_period, str) and explicit_period.strip():
        label = explicit_period.strip()
        year = explicit_year or (period_end.year if period_end else None)
        return label, year, explicit_quarter

    if isinstance(raw_period, str) and raw_period.strip() and raw_period.strip().lower() not in ("quarter", "annual"):
        cleaned = raw_period.strip().upper()
        q = explicit_quarter or _parse_quarter(cleaned)
        year = explicit_year or (period_end.year if period_end else None)
        if q is not None and year is not None and cleaned in (f"Q{q}", str(q)):
            return f"{year}Q{q}", year, q
        return cleaned, year, q

    if explicit_year is not None and explicit_quarter is not None:
        return f"{explicit_year}Q{explicit_quarter}", explicit_year, explicit_quarter

    if period_end is not None:
        if period_type == "annual":
            year = explicit_year or period_end.year
            return f"FY{year}", year, None
        q = explicit_quarter or ((period_end.month - 1) // 3 + 1)
        year = explicit_year or period_end.year
        return f"{year}Q{q}", year, q

    if explicit_year is not None:
        return f"FY{explicit_year}", explicit_year, None

    return ("annual" if period_type == "annual" else "quarter"), None, None


class FmpEarningsAdapter:
    """Adapter for Financial Modeling Prep analyst estimates, surprises, and transcripts."""

    provider_id: str = PROVIDER

    def __init__(
        self,
        *,
        api_key: str | None = None,
        credential_owner: str = "user_byok",
        account_id: str | None = None,
        allow_platform_key: bool | None = None,
        env: Mapping[str, str] | None = None,
        transport: Transport | None = None,
        limiter: RateLimitBackend | None = None,
        policy: FinDataPolicy | None = None,
        now_fn: Callable[[], datetime] = utcnow,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        api_version: Literal["stable", "v3"] = "stable",
        require_account_id: bool = False,
    ) -> None:
        if timeout <= 0 or not math.isfinite(timeout):
            raise ValueError("timeout must be a positive finite number")
        if api_version not in ("stable", "v3"):
            raise ValueError("api_version must be 'stable' or 'v3'")
        self._default_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self._default_owner = credential_owner if credential_owner in ("platform", "user_byok") else "user_byok"
        self._default_account = account_id
        self._env = env
        self._allow_platform_key = allow_platform_key
        self._transport = transport or default_transport(MAX_RESPONSE_BYTES)
        self._limiter = limiter or _get_fmp_limiter()
        self._explicit_policy = policy
        self._now = now_fn
        self._timeout = float(timeout)
        self._api_version: Literal["stable", "v3"] = api_version
        self._require_account_id = require_account_id

    def with_runtime_config(
        self,
        *,
        allow_platform_key: bool | None = None,
        env: Mapping[str, str] | None = None,
        policy: FinDataPolicy | None = None,
    ) -> "FmpEarningsAdapter":
        """Return a shallow clone binding unset runtime policy/env options without mutating self."""
        next_allow = self._allow_platform_key if self._allow_platform_key is not None else allow_platform_key
        next_env = self._env if self._env is not None else env
        next_policy = self._explicit_policy if self._explicit_policy is not None else policy
        if (
            next_allow is self._allow_platform_key
            and next_env is self._env
            and next_policy is self._explicit_policy
        ):
            return self
        return FmpEarningsAdapter(
            api_key=self._default_key,
            credential_owner=self._default_owner,
            account_id=self._default_account,
            allow_platform_key=next_allow,
            env=next_env,
            transport=self._transport,
            limiter=self._limiter,
            policy=next_policy,
            now_fn=self._now,
            timeout=self._timeout,
            api_version=self._api_version,
            require_account_id=self._require_account_id,
        )

    def _platform_allowed(self) -> bool:
        if self._allow_platform_key is not None:
            return self._allow_platform_key
        environ = os.environ if self._env is None else self._env
        return parse_env_bool(environ.get(ALLOW_PLATFORM_KEY_ENV), default=False)

    def _credentials(
        self,
        api_key: str | None,
        credential_owner: str | None,
        account_id: str | None,
    ) -> tuple[str | None, str, str | None]:
        owner = credential_owner or self._default_owner
        if owner not in ("platform", "user_byok"):
            owner = "user_byok"
        key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else self._default_key
        account = account_id if account_id is not None else self._default_account
        if key is None and owner == "platform" and self._platform_allowed():
            environ = os.environ if self._env is None else self._env
            key = (environ.get(API_KEY_ENV) or "").strip() or None  # platform key only; never used for BYOK
        return key, owner, account

    def _decide_policy(self, category: str, owner: str) -> Permissions | Denied:
        policy = self._explicit_policy if self._explicit_policy is not None else load_policy(
            dict(self._env) if self._env is not None else None
        )
        if policy.load_error:
            return Denied(
                reason=policy.load_error,
                provider=PROVIDER,
                category=category,
                message="Configured fin_data policy is invalid; failing closed.",
            )
        has_fmp_rules = any(rule.provider == PROVIDER for rule in policy.rules)
        if not has_fmp_rules and policy.version == "2026-11-20-default":
            # Until policy.py is wired with explicit FMP rules, enforce conservative built-in FMP defaults:
            # strict BYOK by default (export=False, owner_scoped=True); platform key gated by allow_platform_key.
            platform_allowed = self._platform_allowed()
            fallback_policy = FinDataPolicy(
                version="fmp-built-in-default",
                rules=[
                    PolicyRule(
                        provider=PROVIDER,
                        category=cat,
                        credential_owner="user_byok",
                        store=True,
                        display=True,
                        model_input=True,
                        export=False,
                        owner_scoped=True,
                    )
                    for cat in (CATEGORY_CONSENSUS, CATEGORY_TRANSCRIPTS)
                ]
                + [
                    PolicyRule(
                        provider=PROVIDER,
                        category=cat,
                        credential_owner="platform",
                        store=platform_allowed,
                        display=platform_allowed,
                        model_input=platform_allowed,
                        export=False,
                        owner_scoped=False,
                        deny=not platform_allowed,
                        deny_reason="license_platform_key_not_permitted",
                        notes="FMP platform key is disabled unless explicitly enabled for personal development use.",
                    )
                    for cat in (CATEGORY_CONSENSUS, CATEGORY_TRANSCRIPTS)
                ],
            )
            return fallback_policy.decide(PROVIDER, category, credential_owner=owner)
        return policy.decide(PROVIDER, category, credential_owner=owner)

    def _gate(
        self,
        category: str,
        key: str | None,
        owner: str,
        account: str | None,
    ) -> Permissions | ToolResult:
        decision = self._decide_policy(category, owner)
        if isinstance(decision, Denied):
            return ToolResult.failure("denied", decision.reason, decision.message)
        if not key:
            reason = "api_key_not_configured"
            msg = (
                "No FMP API key is configured for this request."
                if owner == "user_byok"
                else "FMP platform API key is not configured or not permitted."
            )
            return ToolResult.failure("failed", reason, msg)
        if decision.owner_scoped and self._require_account_id and not account:
            return ToolResult.failure(
                "failed",
                "account_required",
                "Owner-scoped FMP data requires an account id.",
            )
        return decision

    @staticmethod
    def _limiter_key(owner: str, account: str | None, key: str) -> str:
        if owner == "platform":
            return PROVIDER
        if account:
            return f"{PROVIDER}:byok:{account}"
        key_hash = sha256(key.encode("utf-8")).hexdigest()[:12]
        return f"{PROVIDER}:byok:anon:{key_hash}"

    @staticmethod
    def _extract_error_message(response: Any, secrets: tuple[str, ...]) -> str:
        raw_body = getattr(response, "body", b"") or b""
        if raw_body:
            text = raw_body.decode("utf-8", "replace").strip()
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        msg = _first_present(parsed, "Error Message", "message", "error", "detail")
                        if msg:
                            return redact_secrets(str(msg), secrets)
                except ValueError:
                    pass
            return redact_secrets(text, secrets)
        safe_err = getattr(response, "safe_error", None)
        if isinstance(safe_err, Mapping):
            msg = _first_present(safe_err, "Error Message", "message", "error", "detail", "type")
            if msg:
                return redact_secrets(str(msg), secrets)
        return ""

    @classmethod
    def _map_payload_error(
        cls,
        doc: Mapping[str, Any],
        prov: Provenance,
        cost: Cost,
        secrets: tuple[str, ...],
        not_found_reason: str,
    ) -> ToolResult | None:
        raw_msg = _first_present(doc, "Error Message", "error", "message")
        is_error_status = str(doc.get("status", "")).lower() == "error"
        if raw_msg is None and not is_error_status:
            return None
        msg = redact_secrets(str(raw_msg or "FMP returned an error."), secrets)
        lowered = msg.lower()
        if any(kw in lowered for kw in _RATE_ERROR_KEYWORDS):
            return ToolResult.failure(
                "rate_limited",
                "provider_rate_limited",
                f"FMP rate limit reached: {msg}",
                retryable=True,
                provenance=prov,
                cost=cost,
                suggested_actions=[SuggestedAction(action="wait", retry_after_seconds=60.0)],
            )
        if any(kw in lowered for kw in _KEY_ERROR_KEYWORDS):
            return ToolResult.failure(
                "failed",
                "invalid_api_key",
                "The FMP API key was rejected.",
                provenance=prov,
                cost=cost,
            )
        if any(kw in lowered for kw in _PLAN_ERROR_KEYWORDS):
            return ToolResult.failure(
                "denied",
                "provider_plan_restriction",
                msg,
                provenance=prov,
                cost=cost,
            )
        if any(kw in lowered for kw in _NOT_FOUND_KEYWORDS):
            return ToolResult.failure(
                "empty",
                not_found_reason,
                msg,
                provenance=prov,
                cost=cost,
            )
        return ToolResult.failure(
            "failed",
            "provider_error",
            msg,
            provenance=prov,
            cost=cost,
        )

    def _call(
        self,
        url: str,
        *,
        key: str,
        owner: str,
        account: str | None,
        not_found_reason: str,
        timeout_seconds: float | None = None,
    ) -> tuple[Any, Provenance, Cost]:
        effective_timeout = self._timeout if timeout_seconds is None else float(timeout_seconds)
        if effective_timeout <= 0 or not math.isfinite(effective_timeout):
            raise StopWith(
                ToolResult.failure(
                    "failed",
                    "time_budget",
                    "Remaining time budget is insufficient for an FMP request.",
                )
            )

        safe_url = sanitize_endpoint_url(url)
        secrets = (key,)
        decision = self._limiter.acquire(self._limiter_key(owner, account, key), 1)
        if not decision.allowed:
            raise StopWith(rate_limited_result(decision, PROVIDER))

        cost = Cost(provider_calls=1)
        started = monotonic()
        try:
            response = self._transport(
                "GET",
                url,
                {"apikey": key, "Accept": "application/json"},
                b"",
                effective_timeout,
            )
        except _TransportTimeout:
            elapsed_ms = int((monotonic() - started) * 1000)
            logger.warning(
                "fmp request timeout endpoint=%s elapsed_ms=%d",
                safe_url,
                elapsed_ms,
            )
            raise StopWith(
                ToolResult.failure(
                    "unknown",
                    "source_timeout",
                    "FMP request timed out.",
                    retryable=False,
                    cost=cost,
                )
            ) from None
        except (_TransportFailure, OSError) as error:
            elapsed_ms = int((monotonic() - started) * 1000)
            safe_err = redact_secrets(str(error), secrets)
            logger.warning(
                "fmp transport failure endpoint=%s elapsed_ms=%d detail=%s",
                safe_url,
                elapsed_ms,
                safe_err,
            )
            if "response too large" in str(error):
                raise StopWith(
                    ToolResult.failure(
                        "failed",
                        "source_too_large",
                        "FMP response exceeded the size limit.",
                        cost=cost,
                    )
                ) from None
            raise StopWith(
                ToolResult.failure(
                    "unknown",
                    "source_unavailable",
                    "FMP request failed.",
                    retryable=False,
                    cost=cost,
                )
            ) from None

        elapsed_ms = int((monotonic() - started) * 1000)
        body = getattr(response, "body", b"") or b""
        headers = getattr(response, "headers", {}) or {}
        code = int(getattr(response, "status_code", 0))
        prov = Provenance(
            provider=PROVIDER,
            endpoint=safe_url,
            retrieved_at=self._now(),
            response_sha256=sha256(body).hexdigest(),
        )
        logger.debug(
            "fmp response endpoint=%s status=%d elapsed_ms=%d",
            safe_url,
            code,
            elapsed_ms,
        )

        if code == 429:
            wait = retry_after(headers) or 60.0
            detail = self._extract_error_message(response, secrets)
            msg = f"FMP returned HTTP 429. {detail}".strip()
            raise StopWith(
                ToolResult.failure(
                    "rate_limited",
                    "provider_rate_limited",
                    msg,
                    retryable=True,
                    provenance=prov,
                    cost=cost,
                    suggested_actions=[SuggestedAction(action="wait", retry_after_seconds=wait)],
                )
            )
        if code == 401:
            raise StopWith(
                ToolResult.failure(
                    "failed",
                    "invalid_api_key",
                    "The FMP API key was rejected (HTTP 401).",
                    provenance=prov,
                    cost=cost,
                )
            )
        if code == 403:
            detail = self._extract_error_message(response, secrets)
            lowered = detail.lower()
            reason = "provider_plan_restriction" if any(kw in lowered for kw in _PLAN_ERROR_KEYWORDS) else "forbidden"
            msg = detail or "FMP refused the request (HTTP 403)."
            raise StopWith(
                ToolResult.failure(
                    "denied",
                    reason,
                    msg,
                    provenance=prov,
                    cost=cost,
                )
            )
        if code == 404:
            detail = self._extract_error_message(response, secrets)
            raise StopWith(
                ToolResult.failure(
                    "empty",
                    not_found_reason,
                    detail or f"FMP returned no data at {safe_url} (HTTP 404).",
                    provenance=prov,
                    cost=cost,
                )
            )
        if not 200 <= code < 300:
            detail = self._extract_error_message(response, secrets)
            raise StopWith(
                ToolResult.failure(
                    "failed",
                    "source_unavailable",
                    detail or f"FMP returned HTTP {code}.",
                    retryable=code >= 500,
                    provenance=prov,
                    cost=cost,
                )
            )

        try:
            doc = json.loads(body, parse_float=Decimal)
        except (ValueError, UnicodeDecodeError):
            raise StopWith(
                ToolResult.failure(
                    "failed",
                    "invalid_response",
                    "FMP returned invalid JSON.",
                    provenance=prov,
                    cost=cost,
                )
            ) from None

        if isinstance(doc, dict):
            mapped = self._map_payload_error(doc, prov, cost, secrets, not_found_reason)
            if mapped is not None:
                raise StopWith(mapped)

        return doc, prov, cost

    @staticmethod
    def _validate_symbol(symbol: str) -> str | None:
        cleaned = symbol.strip().upper()
        if not cleaned or not _VALID_SYMBOL_RE.match(cleaned):
            return None
        return cleaned

    def _estimates_url(self, symbol: str, period: Literal["quarter", "annual"], limit: int, version: str) -> str:
        if version == "v3":
            params = urlencode({"period": period, "limit": limit})
            return f"{BASE}/api/v3/analyst-estimates/{quote(symbol, safe='')}?{params}"
        params = urlencode({"symbol": symbol, "period": period, "page": 0, "limit": limit})
        return f"{BASE}/stable/analyst-estimates?{params}"

    def _surprises_url(self, symbol: str, version: str) -> str:
        if version == "v3":
            return f"{BASE}/api/v3/earnings-surprises/{quote(symbol, safe='')}"
        params = urlencode({"symbol": symbol})
        return f"{BASE}/stable/earnings?{params}"

    def _transcript_dates_url(self, symbol: str, version: str) -> str:
        if version == "v3":
            return f"{BASE}/api/v4/earning_call_transcript?{urlencode({'symbol': symbol})}"
        return f"{BASE}/stable/earning-call-transcript-dates?{urlencode({'symbol': symbol})}"

    def _transcript_url(
        self,
        symbol: str,
        fiscal_year: int | None,
        fiscal_quarter: int | None,
        version: str,
    ) -> str:
        if version == "v3":
            base = f"{BASE}/api/v3/earning_call_transcript/{quote(symbol, safe='')}"
            if fiscal_year is not None and fiscal_quarter is not None:
                return f"{base}?{urlencode({'year': fiscal_year, 'quarter': fiscal_quarter})}"
            return base
        assert fiscal_year is not None and fiscal_quarter is not None
        params = urlencode({"symbol": symbol, "year": fiscal_year, "quarter": fiscal_quarter})
        return f"{BASE}/stable/earning-call-transcript?{params}"

    @staticmethod
    def _parse_estimate_row(
        row: Mapping[str, Any],
        symbol: str,
        period_type: Literal["quarter", "annual"],
        permissions: Permissions,
        default_as_of: datetime,
    ) -> EarningsConsensus | None:
        period_end = _parse_date(_first_present(row, "period_end", "periodEnd", "fiscalDateEnding", "date"))
        if period_end is None:
            return None
        row_symbol = str(_first_present(row, "symbol") or symbol).strip().upper()
        fiscal_period, fiscal_year, fiscal_quarter = _derive_fiscal_period(row, period_end, period_type)
        rev_avg = _dec(_first_present(row, "revenueAvg", "estimatedRevenueAvg", "revenueEstimated"))
        rev_low = _dec(_first_present(row, "revenueLow", "estimatedRevenueLow"))
        rev_high = _dec(_first_present(row, "revenueHigh", "estimatedRevenueHigh"))
        eps_avg = _dec(_first_present(row, "epsAvg", "estimatedEpsAvg", "epsEstimated", "estimatedEarning"))
        eps_low = _dec(_first_present(row, "epsLow", "estimatedEpsLow"))
        eps_high = _dec(_first_present(row, "epsHigh", "estimatedEpsHigh"))
        rev_actual = _dec(_first_present(row, "revenueActual", "actualRevenue"))
        eps_actual = _dec(_first_present(row, "epsActual", "actualEarningResult", "actualEarningsPerShare"))
        n_rev = _int_or_none(_first_present(row, "numAnalystsRevenue", "numberAnalystEstimatedRevenue", "numberAnalystsEstimatedRevenue"))
        n_eps = _int_or_none(_first_present(row, "numAnalystsEps", "numberAnalystEstimatedEps", "numberAnalystsEstimatedEps"))
        n_total = _int_or_none(_first_present(row, "numAnalysts"))
        reported_date = _parse_date(_first_present(row, "reportedDate", "announcementDate"))
        as_of = _parse_call_datetime(_first_present(row, "lastUpdated", "updatedAt")) or default_as_of
        currency = _first_present(row, "currency")
        return EarningsConsensus(
            symbol=row_symbol,
            fiscal_period=fiscal_period,
            period_end=period_end,
            revenue_estimate_avg=rev_avg,
            revenue_estimate_low=rev_low,
            revenue_estimate_high=rev_high,
            eps_estimate_avg=eps_avg,
            eps_estimate_low=eps_low,
            eps_estimate_high=eps_high,
            revenue_actual=rev_actual,
            eps_actual=eps_actual,
            num_analysts=n_total,
            num_analysts_revenue=n_rev,
            num_analysts_eps=n_eps,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            period_type=period_type,
            reported_date=reported_date,
            currency=str(currency).strip().upper() if isinstance(currency, str) and currency.strip() else None,
            source_provider=PROVIDER,
            as_of=as_of,
            permissions=permissions,
        )

    @staticmethod
    def _parse_surprise_row(
        row: Mapping[str, Any],
        symbol: str,
        permissions: Permissions,
        default_as_of: datetime,
    ) -> dict[str, Any] | None:
        reported_date = _parse_date(_first_present(row, "date", "reportedDate"))
        explicit_period_end = _parse_date(_first_present(row, "fiscalDateEnding", "periodEnd", "period_end"))
        if reported_date is None and explicit_period_end is None:
            return None
        row_symbol = str(_first_present(row, "symbol") or symbol).strip().upper()
        eps_est = _dec(_first_present(row, "epsEstimated", "estimatedEarning", "epsAvg", "estimatedEpsAvg"))
        rev_est = _dec(_first_present(row, "revenueEstimated", "estimatedRevenue", "revenueAvg", "estimatedRevenueAvg"))
        eps_act = _dec(_first_present(row, "epsActual", "actualEarningResult", "actualEarningsPerShare"))
        rev_act = _dec(_first_present(row, "revenueActual", "actualRevenue"))
        as_of = _parse_call_datetime(_first_present(row, "lastUpdated", "updatedAt")) or default_as_of
        period_raw = _first_present(row, "period", "fiscalPeriod")
        return {
            "symbol": row_symbol,
            "reported_date": reported_date,
            "period_end": explicit_period_end,
            "eps_estimate_avg": eps_est,
            "revenue_estimate_avg": rev_est,
            "eps_actual": eps_act,
            "revenue_actual": rev_act,
            "as_of": as_of,
            "period_raw": str(period_raw).strip() if isinstance(period_raw, str) and period_raw.strip() else None,
            "permissions": permissions,
        }

    @staticmethod
    def _merge_actuals(
        estimates: list[EarningsConsensus],
        surprises: list[dict[str, Any]],
        period_type: Literal["quarter", "annual"],
    ) -> list[EarningsConsensus]:
        if not estimates or not surprises:
            return estimates

        # Only attach quarterly earnings announcement rows automatically onto quarterly estimates
        # unless a surprise row explicitly provides an exact matching period_end or annual marker.
        candidates: list[tuple[int, int, int]] = []  # (abs_lag_days, estimate_idx, surprise_idx)
        for est_idx, est in enumerate(estimates):
            if est.period_end is None:
                continue
            for surp_idx, surp in enumerate(surprises):
                if surp["period_end"] is not None:
                    if surp["period_end"] == est.period_end:
                        candidates.append((0, est_idx, surp_idx))
                    continue
                if period_type == "annual":
                    raw = (surp.get("period_raw") or "").lower()
                    if raw not in ("annual", "fy"):
                        continue
                rep_date = surp["reported_date"]
                if rep_date is None:
                    continue
                lag = (rep_date - est.period_end).days
                if -7 <= lag <= 105:
                    # Prefer report dates on or after period_end with smallest lag
                    score = lag if lag >= 0 else abs(lag) + 1000
                    candidates.append((score, est_idx, surp_idx))

        candidates.sort(key=lambda item: item[0])
        matched_est: set[int] = set()
        matched_surp: set[int] = set()
        updates: dict[int, dict[str, Any]] = {}
        for _score, est_idx, surp_idx in candidates:
            if est_idx in matched_est or surp_idx in matched_surp:
                continue
            matched_est.add(est_idx)
            matched_surp.add(surp_idx)
            surp = surprises[surp_idx]
            est = estimates[est_idx]
            patch: dict[str, Any] = {}
            if est.revenue_actual is None and surp["revenue_actual"] is not None:
                patch["revenue_actual"] = surp["revenue_actual"]
            if est.eps_actual is None and surp["eps_actual"] is not None:
                patch["eps_actual"] = surp["eps_actual"]
            if est.revenue_estimate_avg is None and surp["revenue_estimate_avg"] is not None:
                patch["revenue_estimate_avg"] = surp["revenue_estimate_avg"]
            if est.eps_estimate_avg is None and surp["eps_estimate_avg"] is not None:
                patch["eps_estimate_avg"] = surp["eps_estimate_avg"]
            if est.reported_date is None and surp["reported_date"] is not None:
                patch["reported_date"] = surp["reported_date"]
            if patch:
                updates[est_idx] = patch

        return [
            est.model_copy(update=updates[idx]) if idx in updates else est
            for idx, est in enumerate(estimates)
        ]

    def fetch_consensus(
        self,
        symbol: str,
        *,
        period: Literal["quarter", "annual"] = "quarter",
        limit: int = 8,
        include_actuals: bool = True,
        api_key: str | None = None,
        credential_owner: str | None = None,
        account_id: str | None = None,
        api_version: Literal["stable", "v3"] | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        """Fetch analyst consensus estimates (and optional reported actuals) for a symbol."""
        sym = self._validate_symbol(symbol)
        if sym is None:
            return ToolResult.failure("failed", "invalid_request", "symbol must be a valid ticker.")
        if period not in ("quarter", "annual") or limit < 1 or limit > MAX_ESTIMATES_LIMIT:
            return ToolResult.failure(
                "failed",
                "invalid_request",
                f"period must be 'quarter' or 'annual' and limit between 1 and {MAX_ESTIMATES_LIMIT}.",
            )
        version = api_version or self._api_version
        if version not in ("stable", "v3"):
            return ToolResult.failure("failed", "invalid_request", "api_version must be 'stable' or 'v3'.")

        key, owner, account = self._credentials(api_key, credential_owner, account_id)
        gate = self._gate(CATEGORY_CONSENSUS, key, owner, account)
        if isinstance(gate, ToolResult):
            return gate
        assert key is not None

        est_url = self._estimates_url(sym, period, limit, version)
        try:
            est_doc, est_prov, est_cost = self._call(
                est_url,
                key=key,
                owner=owner,
                account=account,
                not_found_reason="symbol_not_found",
                timeout_seconds=timeout_seconds,
            )
        except StopWith as stop:
            return stop.result

        est_rows = [est_doc] if isinstance(est_doc, dict) else est_doc
        if not isinstance(est_rows, list):
            return ToolResult.failure(
                "failed",
                "invalid_response",
                "Unexpected FMP analyst-estimates payload shape.",
                provenance=est_prov,
                cost=est_cost,
            )

        estimates: list[EarningsConsensus] = []
        for row in est_rows:
            if not isinstance(row, Mapping):
                continue
            item = self._parse_estimate_row(row, sym, period, gate, est_prov.retrieved_at)
            if item is not None:
                estimates.append(item)
        estimates.sort(key=lambda item: item.period_end or date.min, reverse=True)
        estimates = estimates[:limit]

        total_calls = est_cost.provider_calls
        endpoints = [est_prov.endpoint]
        notes = [
            f"FMP {version} analyst estimates; period labels derived from period_end when fiscal labels are absent."
        ]
        partial_reason: str | None = None

        if include_actuals and (estimates or not est_rows):
            surp_url = self._surprises_url(sym, version)
            try:
                surp_doc, surp_prov, surp_cost = self._call(
                    surp_url,
                    key=key,
                    owner=owner,
                    account=account,
                    not_found_reason="actuals_not_found",
                    timeout_seconds=timeout_seconds,
                )
                total_calls += surp_cost.provider_calls
                endpoints.append(surp_prov.endpoint)
                surp_rows = [surp_doc] if isinstance(surp_doc, dict) else surp_doc
                if isinstance(surp_rows, list):
                    parsed_surprises = [
                        parsed
                        for row in surp_rows
                        if isinstance(row, Mapping)
                        and (parsed := self._parse_surprise_row(row, sym, gate, surp_prov.retrieved_at)) is not None
                    ]
                    if estimates:
                        estimates = self._merge_actuals(estimates, parsed_surprises, period)
                    elif parsed_surprises and period == "quarter":
                        # Fallback to earnings/surprises rows when estimates endpoint returned []
                        for s in parsed_surprises[:limit]:
                            rep = s["reported_date"]
                            fp = rep.isoformat() if rep else "unknown"
                            estimates.append(
                                EarningsConsensus(
                                    symbol=s["symbol"],
                                    fiscal_period=fp,
                                    period_end=s["period_end"],
                                    reported_date=rep,
                                    revenue_estimate_avg=s["revenue_estimate_avg"],
                                    eps_estimate_avg=s["eps_estimate_avg"],
                                    revenue_actual=s["revenue_actual"],
                                    eps_actual=s["eps_actual"],
                                    period_type="quarter",
                                    source_provider=PROVIDER,
                                    as_of=s["as_of"],
                                    permissions=gate,
                                )
                            )
                        notes.append("Analyst estimates endpoint was empty; populated consensus from FMP earnings surprises.")
            except StopWith as stop:
                total_calls += stop.result.cost.provider_calls
                endpoints.append(sanitize_endpoint_url(surp_url))
                if estimates:
                    partial_reason = "actuals_unavailable"
                    notes.append(f"Reported actuals enrichment failed ({stop.result.reason}); returning consensus estimates only.")
                else:
                    return stop.result.model_copy(update={"cost": Cost(provider_calls=total_calls)})

        prov = Provenance(
            provider=PROVIDER,
            endpoint=est_prov.endpoint,
            retrieved_at=est_prov.retrieved_at,
            response_sha256=est_prov.response_sha256,
            endpoints=endpoints,
        )
        cost = Cost(provider_calls=total_calls)

        if not estimates:
            cov = Coverage(
                entity_id=sym,
                requested={"symbol": sym, "period": period, "limit": str(limit)},
                rows=0,
                data_as_of=est_prov.retrieved_at,
                notes=notes,
            )
            return ToolResult(
                status="empty",
                reason="no_consensus_data",
                message=f"FMP returned no consensus estimates for {sym}.",
                coverage=cov,
                permissions=gate,
                provenance=prov,
                cost=cost,
            )

        dates = [item.period_end or item.reported_date for item in estimates if (item.period_end or item.reported_date) is not None]
        cov = Coverage(
            entity_id=sym,
            requested={"symbol": sym, "period": period, "limit": str(limit)},
            actual={
                "first": min(dates).isoformat() if dates else None,
                "last": max(dates).isoformat() if dates else None,
            },
            rows=len(estimates),
            data_as_of=est_prov.retrieved_at,
            notes=notes,
        )
        return ToolResult(
            status="partial" if partial_reason else "ok",
            reason=partial_reason or "ok",
            message="Consensus estimates retrieved without reported actuals." if partial_reason else "",
            coverage=cov,
            permissions=gate,
            provenance=prov,
            cost=cost,
            data=estimates,
        )

    def fetch_earnings_surprises(
        self,
        symbol: str,
        *,
        limit: int = 8,
        api_key: str | None = None,
        credential_owner: str | None = None,
        account_id: str | None = None,
        api_version: Literal["stable", "v3"] | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        """Fetch historical earnings surprises (estimated vs. actual EPS/revenue) for a symbol."""
        sym = self._validate_symbol(symbol)
        if sym is None:
            return ToolResult.failure("failed", "invalid_request", "symbol must be a valid ticker.")
        if limit < 1 or limit > MAX_ESTIMATES_LIMIT:
            return ToolResult.failure("failed", "invalid_request", f"limit must be between 1 and {MAX_ESTIMATES_LIMIT}.")
        version = api_version or self._api_version
        if version not in ("stable", "v3"):
            return ToolResult.failure("failed", "invalid_request", "api_version must be 'stable' or 'v3'.")

        key, owner, account = self._credentials(api_key, credential_owner, account_id)
        gate = self._gate(CATEGORY_CONSENSUS, key, owner, account)
        if isinstance(gate, ToolResult):
            return gate
        assert key is not None

        url = self._surprises_url(sym, version)
        try:
            doc, prov, cost = self._call(
                url,
                key=key,
                owner=owner,
                account=account,
                not_found_reason="symbol_not_found",
                timeout_seconds=timeout_seconds,
            )
        except StopWith as stop:
            return stop.result

        rows = [doc] if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            return ToolResult.failure(
                "failed",
                "invalid_response",
                "Unexpected FMP earnings-surprises payload shape.",
                provenance=prov,
                cost=cost,
            )

        out: list[EarningsConsensus] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            parsed = self._parse_surprise_row(row, sym, gate, prov.retrieved_at)
            if parsed is None:
                continue
            rep = parsed["reported_date"]
            pe = parsed["period_end"]
            fp = parsed["period_raw"] or (pe or rep).isoformat()
            out.append(
                EarningsConsensus(
                    symbol=parsed["symbol"],
                    fiscal_period=fp,
                    period_end=pe,
                    reported_date=rep,
                    revenue_estimate_avg=parsed["revenue_estimate_avg"],
                    eps_estimate_avg=parsed["eps_estimate_avg"],
                    revenue_actual=parsed["revenue_actual"],
                    eps_actual=parsed["eps_actual"],
                    period_type="quarter",
                    source_provider=PROVIDER,
                    as_of=parsed["as_of"],
                    permissions=gate,
                )
            )
            if len(out) >= limit:
                break

        if not out:
            return ToolResult(
                status="empty",
                reason="no_consensus_data",
                message=f"FMP returned no earnings surprises for {sym}.",
                coverage=Coverage(entity_id=sym, rows=0, data_as_of=prov.retrieved_at),
                permissions=gate,
                provenance=prov,
                cost=cost,
            )

        return ToolResult(
            status="ok",
            reason="ok",
            coverage=Coverage(entity_id=sym, rows=len(out), data_as_of=prov.retrieved_at),
            permissions=gate,
            provenance=prov,
            cost=cost,
            data=out,
        )

    def fetch_transcript(
        self,
        symbol: str,
        *,
        fiscal_year: int | None = None,
        fiscal_quarter: int | None = None,
        api_key: str | None = None,
        credential_owner: str | None = None,
        account_id: str | None = None,
        api_version: Literal["stable", "v3"] | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        """Fetch an earnings call transcript for a specific or latest fiscal quarter."""
        sym = self._validate_symbol(symbol)
        if sym is None:
            return ToolResult.failure("failed", "invalid_request", "symbol must be a valid ticker.")
        if (fiscal_year is None) != (fiscal_quarter is None):
            return ToolResult.failure(
                "failed",
                "invalid_request",
                "Provide both fiscal_year and fiscal_quarter, or omit both to fetch the latest transcript.",
            )
        if fiscal_quarter is not None and fiscal_quarter not in (1, 2, 3, 4):
            return ToolResult.failure("failed", "invalid_request", "fiscal_quarter must be 1, 2, 3, or 4.")
        if fiscal_year is not None and not (1900 <= fiscal_year <= 2200):
            return ToolResult.failure("failed", "invalid_request", "fiscal_year must be between 1900 and 2200.")

        version = api_version or self._api_version
        if version not in ("stable", "v3"):
            return ToolResult.failure("failed", "invalid_request", "api_version must be 'stable' or 'v3'.")

        key, owner, account = self._credentials(api_key, credential_owner, account_id)
        gate = self._gate(CATEGORY_TRANSCRIPTS, key, owner, account)
        if isinstance(gate, ToolResult):
            return gate
        assert key is not None

        endpoints: list[str] = []
        total_calls = 0
        target_year = fiscal_year
        target_quarter = fiscal_quarter

        if version == "stable" and target_year is None and target_quarter is None:
            dates_url = self._transcript_dates_url(sym, version)
            try:
                dates_doc, dates_prov, dates_cost = self._call(
                    dates_url,
                    key=key,
                    owner=owner,
                    account=account,
                    not_found_reason="transcript_not_found",
                    timeout_seconds=timeout_seconds,
                )
            except StopWith as stop:
                return stop.result
            total_calls += dates_cost.provider_calls
            endpoints.append(dates_prov.endpoint)
            if not isinstance(dates_doc, list) or not dates_doc:
                return ToolResult(
                    status="empty",
                    reason="transcript_not_found",
                    message=f"FMP has no earnings call transcript dates for {sym}.",
                    coverage=Coverage(entity_id=sym, rows=0, data_as_of=dates_prov.retrieved_at),
                    permissions=gate,
                    provenance=dates_prov,
                    cost=Cost(provider_calls=total_calls),
                )
            best_entry: tuple[date, int, int] | None = None
            for entry in dates_doc:
                if not isinstance(entry, Mapping):
                    continue
                y = _int_or_none(_first_present(entry, "fiscalYear", "year"))
                q = _parse_quarter(_first_present(entry, "quarter", "fiscalQuarter"))
                d = _parse_date(_first_present(entry, "date")) or date.min
                if y is not None and q is not None:
                    cand = (d, y, q)
                    if best_entry is None or cand > best_entry:
                        best_entry = cand
            if best_entry is None:
                return ToolResult(
                    status="empty",
                    reason="transcript_not_found",
                    message=f"FMP returned no usable transcript dates for {sym}.",
                    coverage=Coverage(entity_id=sym, rows=0, data_as_of=dates_prov.retrieved_at),
                    permissions=gate,
                    provenance=dates_prov,
                    cost=Cost(provider_calls=total_calls),
                )
            _, target_year, target_quarter = best_entry

        url = self._transcript_url(sym, target_year, target_quarter, version)
        try:
            doc, prov, cost = self._call(
                url,
                key=key,
                owner=owner,
                account=account,
                not_found_reason="transcript_not_found",
                timeout_seconds=timeout_seconds,
            )
        except StopWith as stop:
            return stop.result.model_copy(update={"cost": Cost(provider_calls=total_calls + stop.result.cost.provider_calls)})

        total_calls += cost.provider_calls
        endpoints.append(prov.endpoint)
        combined_prov = Provenance(
            provider=PROVIDER,
            endpoint=prov.endpoint,
            retrieved_at=prov.retrieved_at,
            response_sha256=prov.response_sha256,
            endpoints=endpoints,
        )
        combined_cost = Cost(provider_calls=total_calls)

        rows = [doc] if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            return ToolResult.failure(
                "failed",
                "invalid_response",
                "Unexpected FMP transcript payload shape.",
                provenance=combined_prov,
                cost=combined_cost,
            )

        candidates: list[tuple[int, int, date, Mapping[str, Any], str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            content = _first_present(row, "content", "transcript")
            if not isinstance(content, str) or not content.strip():
                continue
            y = _int_or_none(_first_present(row, "year", "fiscalYear")) or target_year
            q = _parse_quarter(_first_present(row, "quarter", "fiscalQuarter")) or target_quarter
            if y is None or q is None:
                continue
            if target_year is not None and y != target_year:
                continue
            if target_quarter is not None and q != target_quarter:
                continue
            d = _parse_date(_first_present(row, "date", "callDate")) or date.min
            candidates.append((y, q, d, row, content.strip()))

        if not candidates:
            return ToolResult(
                status="empty",
                reason="transcript_not_found",
                message=f"FMP returned no transcript content for {sym}.",
                coverage=Coverage(entity_id=sym, rows=0, data_as_of=prov.retrieved_at),
                permissions=gate,
                provenance=combined_prov,
                cost=combined_cost,
            )

        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        picked_year, picked_quarter, _d, picked_row, raw_content = candidates[0]
        picked_symbol = str(_first_present(picked_row, "symbol") or sym).strip().upper()
        call_dt = _parse_call_datetime(_first_present(picked_row, "date", "callDate"))

        transcript = EarningsTranscript.from_raw_text(
            symbol=picked_symbol,
            fiscal_year=picked_year,
            fiscal_quarter=picked_quarter,
            call_date=call_dt,
            full_text=raw_content,
            source_provider=PROVIDER,
            as_of=prov.retrieved_at,
            permissions=gate,
        )
        cov = Coverage(
            entity_id=picked_symbol,
            requested={
                "symbol": sym,
                "fiscal_year": str(target_year) if target_year is not None else "latest",
                "fiscal_quarter": str(target_quarter) if target_quarter is not None else "latest",
            },
            actual={
                "fiscal_year": str(transcript.fiscal_year),
                "fiscal_quarter": str(transcript.fiscal_quarter),
                "call_date": transcript.call_date.isoformat() if transcript.call_date else None,
            },
            rows=1,
            data_as_of=prov.retrieved_at,
            notes=[f"Parsed {len(transcript.sections)} sections and {len(transcript.speakers)} speakers from FMP {version}."],
        )
        return ToolResult(
            status="ok",
            reason="ok",
            coverage=cov,
            permissions=gate,
            provenance=combined_prov,
            cost=combined_cost,
            data=[transcript],
        )


__all__ = [
    "ALLOW_PLATFORM_KEY_ENV",
    "API_KEY_ENV",
    "BASE",
    "CATEGORY_CONSENSUS",
    "CATEGORY_TRANSCRIPTS",
    "FmpEarningsAdapter",
    "PROVIDER",
]
