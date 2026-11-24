"""License and permission policy for data providers.

The policy is configuration, not code: per provider, data category and
optional exchange / asset type / interval / credential owner it declares
store, display, model_input and export flags. A request can never widen what
the policy grants (``decide(..., requested=...)`` intersects).

Credential owners: ``platform`` is the operator's key (the operator is the
licensee for a public product); ``user_byok`` is the end user's own key (the
user is the licensee, personal use) and its data is owner-scoped.

The runtime uses the account's own Twelve Data key first. It falls back to the platform key
(FORESIGHTNINE_TWELVEDATA_API_KEY) only when this policy allows provider twelve_data for
credential_owner "platform" (identity / bars_daily / bars_intraday); the default policy denies it.

Yahoo Finance daily bars (A-shares and Hong Kong, provider ``yahoo``, category bars_daily): the default policy
denies them (license_terms_restrict_commercial_use); Yahoo's terms do not allow commercial or automated use. The
personal-development example below allows them (store/display/model_input, no export).

Example for personal development use (env FORESIGHTNINE_FIN_DATA_POLICY replaces the whole default
policy, so SEC rules are repeated). REMOVE IT BEFORE ANY PUBLIC RELEASE: Twelve Data free/individual
plans are personal, non-commercial use and forbid redistribution:

    {"version":"dev-personal-platform-key","rules":[{"provider":"sec_edgar","category":"identity","store":true,"display":true,"model_input":true,"export":true},{"provider":"sec_edgar","category":"filings","store":true,"display":true,"model_input":true,"export":true},{"provider":"sec_edgar","category":"financials","store":true,"display":true,"model_input":true,"export":true},{"provider":"sec_edgar","category":"ownership","store":true,"display":true,"model_input":true,"export":true},{"provider":"twelve_data","category":"identity","credential_owner":"platform","store":true,"display":true,"model_input":true},{"provider":"twelve_data","category":"identity","credential_owner":"user_byok","store":true,"display":true,"model_input":true,"owner_scoped":true},{"provider":"twelve_data","category":"bars_daily","credential_owner":"platform","store":true,"display":true,"model_input":true},{"provider":"twelve_data","category":"bars_daily","credential_owner":"user_byok","store":true,"display":true,"model_input":true,"owner_scoped":true},{"provider":"twelve_data","category":"bars_intraday","credential_owner":"platform","store":true,"display":true,"model_input":true},{"provider":"twelve_data","category":"bars_intraday","credential_owner":"user_byok","store":true,"display":true,"model_input":true,"owner_scoped":true},{"provider":"tushare","category":"identity","credential_owner":"platform","store":true,"display":true,"model_input":true},{"provider":"tushare","category":"bars_daily","credential_owner":"platform","store":true,"display":true,"model_input":true},{"provider":"tushare","category":"financials","credential_owner":"platform","store":true,"display":true,"model_input":true},{"provider":"tushare","category":"identity","credential_owner":"user_byok","store":true,"display":true,"model_input":true,"owner_scoped":true},{"provider":"tushare","category":"bars_daily","credential_owner":"user_byok","store":true,"display":true,"model_input":true,"owner_scoped":true},{"provider":"tushare","category":"financials","credential_owner":"user_byok","store":true,"display":true,"model_input":true,"owner_scoped":true},{"provider":"baostock","category":"identity","store":true,"display":true,"model_input":true},{"provider":"baostock","category":"bars_daily","store":true,"display":true,"model_input":true},{"provider":"baostock","category":"financials","store":true,"display":true,"model_input":true},{"provider":"yahoo","category":"bars_daily","store":true,"display":true,"model_input":true,"export":false}]}
"""
from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .models import Permissions

POLICY_ENV = "FORESIGHTNINE_FIN_DATA_POLICY"
CredentialOwner = Literal["platform", "user_byok"]
DAILY_INTERVALS = frozenset({"1day", "1week", "1month"})


def bar_category(interval: str) -> str:
    return "bars_daily" if interval in DAILY_INTERVALS else "bars_intraday"


class Denied(BaseModel):
    model_config = ConfigDict(frozen=True)
    reason: str
    message: str = ""
    provider: str = ""
    category: str = ""


class PolicyRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: str
    category: str
    exchange: str | None = None
    asset_type: str | None = None
    interval: str | None = None
    credential_owner: CredentialOwner | None = None
    store: bool = False
    display: bool = False
    model_input: bool = False
    export: bool = False
    owner_scoped: bool = False
    deny: bool = False
    deny_reason: str = "denied_by_policy"
    retention_days: int | None = Field(default=None, ge=0)
    notes: str = ""

    @field_validator("provider", "category", "asset_type", "interval")
    @classmethod
    def lower(cls, value: str | None) -> str | None:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("exchange")
    @classmethod
    def upper(cls, value: str | None) -> str | None:
        return value.strip().upper() if isinstance(value, str) else value

    def specificity(self) -> int:
        return sum(item is not None for item in (self.exchange, self.asset_type, self.interval, self.credential_owner))

    def matches(self, provider: str, category: str, exchange: str | None, asset_type: str | None,
                interval: str | None, owner: str) -> bool:
        return (self.provider == provider and self.category == category
                and (self.exchange is None or self.exchange == exchange)
                and (self.asset_type is None or self.asset_type == asset_type)
                and (self.interval is None or self.interval == interval)
                and (self.credential_owner is None or self.credential_owner == owner))


class FinDataPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = "custom"
    notes: str = ""
    rules: list[PolicyRule] = Field(default_factory=list)
    load_error: str | None = None

    def _winner(self, provider, category, exchange, asset_type, interval, owner) -> list[PolicyRule]:
        found = [r for r in self.rules if r.matches(provider, category, exchange, asset_type, interval, owner)]
        if not found:
            return []
        top = max(r.specificity() for r in found)
        return [r for r in found if r.specificity() == top]

    def decide(self, provider: str, category: str, exchange: str | None = None, asset_type: str | None = None,
               interval: str | None = None, *, credential_owner: str = "platform",
               requested: Permissions | None = None) -> Permissions | Denied:
        provider, category = provider.strip().lower(), category.strip().lower()
        exchange = exchange.strip().upper() if exchange else None
        asset_type = asset_type.strip().lower() if asset_type else None
        interval = interval.strip().lower() if interval else None
        winners = self._winner(provider, category, exchange, asset_type, interval, credential_owner)
        if not winners:
            return Denied(reason="not_in_policy", provider=provider, category=category,
                          message=f"{provider}/{category} is not declared in the data policy.")
        denying = [r for r in winners if r.deny]
        if denying:
            return Denied(reason=denying[0].deny_reason, provider=provider, category=category,
                          message=denying[0].notes or "Denied by data policy.")
        granted = Permissions(store=all(r.store for r in winners), display=all(r.display for r in winners),
                              model_input=all(r.model_input for r in winners), export=all(r.export for r in winners),
                              owner_scoped=any(r.owner_scoped for r in winners))
        return granted.narrow(requested) if requested is not None else granted

    def retention_days(self, provider: str, category: str, exchange: str | None = None,
                       asset_type: str | None = None, interval: str | None = None,
                       credential_owner: str = "platform") -> int | None:
        winners = self._winner(provider.lower(), category.lower(), exchange.upper() if exchange else None,
                               asset_type.lower() if asset_type else None, interval, credential_owner)
        values = [r.retention_days for r in winners if r.retention_days is not None]
        return min(values) if values else None


_TD_NOTES = (
    "Twelve Data licence (checked 2026-11-20): Basic/free and individual plans are personal, non-commercial use "
    "and forbid displaying or redistributing data to third parties; external display needs a business plan; "
    "non-US price data needs approval for commercial use; ASX data is internal-use only on all tiers. "
    "Sources: https://support.twelvedata.com/en/articles/5332349-commercial-and-personal-usage and "
    "https://twelvedata.com/pricing.md . ForesightNine is a public product, so the platform key is denied "
    "(license_platform_key_not_permitted) until the operator configures a policy entry stating a verified "
    "commercial plan. A user's own key (user_byok) lets that user store/display/model-input their own data; "
    "export is off and cache keys must include the owner account. Terms still to be re-verified before enabling."
)
# (A-share sources, same pattern) see _TS_NOTES / _BS_NOTES below.


_TS_NOTES = (
    "Tushare Pro (checked 2026-11-21 against the public service description only; terms still to be re-verified): "
    "a user's own token is for that user's own use. The platform token is denied by default like twelve_data; an "
    "operator enables it by policy (personal development use only, remove before public release)."
)
_BS_NOTES = (
    "BaoStock (baostock.com, keyless): no terms of use were found when this rule was written. Personal "
    "development use is assumed; export is off. NEEDS REVIEW BEFORE ANY PUBLIC RELEASE."
)
_YAHOO_NOTES = (
    "Yahoo Finance (checked 2026-11-21): the terms do not allow commercial use or automated/bulk access of the "
    "data. Only an operator running a personal development deployment may enable it by policy; remove before any "
    "public release. Export stays off."
)
_FMP_NOTES = (
    "Financial Modeling Prep (FMP): user's own BYOK key is allowed for personal research with store/display/model_input "
    "and owner-scoped caching (export=False). Platform key (FORESIGHTNINE_FMP_API_KEY) is denied by default "
    "(license_platform_key_not_permitted) unless explicitly opted in for personal/development use via "
    "FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY=true or an explicit FinDataPolicy."
)
CN_CATEGORIES = ("identity", "bars_daily", "financials")
FMP_CATEGORIES = ("consensus", "transcripts", "earnings_consensus", "earnings_transcript")
FMP_ALLOW_PLATFORM_ENV = "FORESIGHTNINE_FMP_ALLOW_PLATFORM_KEY"


def _parse_env_flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def default_policy(*, allow_fmp_platform: bool = False) -> FinDataPolicy:
    sec = lambda cat: PolicyRule(provider="sec_edgar", category=cat, store=True, display=True,
                                 model_input=True, export=True, notes="SEC public disclosure; cite source on export.")
    fmp_platform_rules = [
        PolicyRule(
            provider="fmp",
            category=cat,
            credential_owner="platform",
            store=allow_fmp_platform,
            display=allow_fmp_platform,
            model_input=allow_fmp_platform,
            export=False,
            owner_scoped=False,
            deny=not allow_fmp_platform,
            deny_reason="license_platform_key_not_permitted",
            notes=_FMP_NOTES,
        )
        for cat in FMP_CATEGORIES
    ]
    fmp_byok_rules = [
        PolicyRule(
            provider="fmp",
            category=cat,
            credential_owner="user_byok",
            store=True,
            display=True,
            model_input=True,
            export=False,
            owner_scoped=True,
            notes="User's own FMP API key, personal use; owner-scoped, no export, no cross-account cache.",
        )
        for cat in FMP_CATEGORIES
    ]
    return FinDataPolicy(
        version="2026-11-24-default",
        notes=("Conservative built-in policy. SEC EDGAR public data allowed incl. export (SEC fair-access: declare a "
               "User-Agent with contact, max 10 req/s; verify terms periodically). Unscoped EDGAR full-text search "
               "('filing_search') remains unlisted and denied by default; strictly single-CIK search "
               "('filing_search_cik') is allowed. FMP defaults to user_byok (export=False). " + _TD_NOTES),
        rules=[
            sec("identity"), sec("filings"), sec("financials"), sec("ownership"), sec("filing_search_cik"),
            # Twelve Data: platform key denied; the same rule applies to every category.
            *[PolicyRule(provider="twelve_data", category=cat, credential_owner="platform", deny=True,
                         deny_reason="license_platform_key_not_permitted",
                         notes="Twelve Data free/individual plans are personal non-commercial; platform key not permitted.")
              for cat in ("identity", "bars_daily", "bars_intraday")],
            *[PolicyRule(provider="twelve_data", category=cat, credential_owner="user_byok", store=True, display=True,
                         model_input=True, export=False, owner_scoped=True,
                         notes="User's own key, personal use; owner-scoped, no export, no cross-account cache.")
              for cat in ("identity", "bars_daily", "bars_intraday")],
            *[PolicyRule(provider="twelve_data", category=cat, exchange="ASX", store=True, display=False,
                         model_input=True, export=False, owner_scoped=True,
                         notes="Twelve Data ASX data is internal-use only on all tiers: no display.")
              for cat in ("bars_daily", "bars_intraday")],
            *[PolicyRule(provider="tushare", category=cat, credential_owner="platform", deny=True,
                         deny_reason="license_platform_key_not_permitted", notes=_TS_NOTES) for cat in CN_CATEGORIES],
            *[PolicyRule(provider="tushare", category=cat, credential_owner="user_byok", store=True, display=True,
                         model_input=True, export=False, owner_scoped=True,
                         notes="User's own Tushare token, personal use; owner-scoped, no export.") for cat in CN_CATEGORIES],
            *[PolicyRule(provider="baostock", category=cat, store=True, display=True, model_input=True, export=False,
                         notes=_BS_NOTES) for cat in CN_CATEGORIES],
            *fmp_platform_rules,
            *fmp_byok_rules,
            PolicyRule(provider="yahoo", category="bars_daily", deny=True, deny_reason="license_terms_restrict_commercial_use",
                       notes=_YAHOO_NOTES),
        ])


def deny_all_policy(error: str) -> FinDataPolicy:
    return FinDataPolicy(version="deny-all", notes="Configured policy was invalid; everything is denied.",
                         rules=[], load_error=error)


def load_policy(env: dict[str, str] | None = None) -> FinDataPolicy:
    """Env JSON replaces the default entirely; an invalid value denies everything."""
    environ = env if env is not None else os.environ
    raw = environ.get(POLICY_ENV, "").strip()
    if not raw:
        return default_policy(allow_fmp_platform=_parse_env_flag(environ.get(FMP_ALLOW_PLATFORM_ENV)))
    try:
        policy = FinDataPolicy.model_validate_json(raw)
    except ValidationError:
        return deny_all_policy("invalid_policy_json")
    # Backward-compatible inheritance: if a custom policy allows sec_edgar/filings and has no
    # explicit rule (allow or deny) for single-CIK sec_edgar/filing_search_cik, inherit permissions
    # from sec_edgar/filings while keeping unscoped filing_search denied by default.
    has_cik_rule = any(r.provider == "sec_edgar" and r.category == "filing_search_cik" for r in policy.rules)
    if not has_cik_rule:
        filings_rules = [
            r for r in policy.rules
            if r.provider == "sec_edgar" and r.category == "filings" and not r.deny
        ]
        if filings_rules:
            inherited = [
                r.model_copy(update={
                    "category": "filing_search_cik",
                    "notes": "Inherited from sec_edgar/filings for strictly single-CIK EFTS search.",
                })
                for r in filings_rules
            ]
            policy = policy.model_copy(update={"rules": [*policy.rules, *inherited]})
    return policy


def cache_scope_key(permissions: Permissions, account_id: str | None) -> str:
    """Cache namespace: owner-scoped (BYOK) data must never be shared across accounts."""
    if not permissions.owner_scoped:
        return "global"
    if not account_id:
        raise ValueError("owner-scoped data requires an account id in its cache key")
    return f"owner:{account_id}"
