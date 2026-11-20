"""Versioned registry of site terms for automated retrieval (plan 6.10).

A host is blocked for automated access when its published terms prohibit it
('prohibited') or when permission for a public commercial product has not been
verified ('unverified_commercial'). Operators can record written permission through
FORESIGHTNINE_SOURCE_TERMS_OVERRIDES; user uploads and the issuer's own
investor-relations copy remain the compliant routes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

TERMS_REGISTRY_VERSION = "2026-11-20.1"
OVERRIDES_ENV = "FORESIGHTNINE_SOURCE_TERMS_OVERRIDES"

Status = Literal["prohibited", "unverified_commercial", "allowed"]


@dataclass(frozen=True)
class HostTermsRule:
    host: str
    status: Status
    reason_code: str
    note: str
    evidence_url: str
    checked_on: str


TERMS_RULES: tuple[HostTermsRule, ...] = (
    HostTermsRule(
        "hkex.com.hk", "prohibited", "hkex_terms_prohibit_automated_access",
        "HKEX Terms of Use (updated 19 Aug 2025) prohibit robots, scrapers, programmatic or scripted access, and text or data mining.",
        "https://www.hkex.com.hk/global/exchange/terms-of-use", "2026-11-20"),
    HostTermsRule(
        "hkexnews.hk", "prohibited", "hkex_terms_prohibit_automated_access",
        "HKEXnews is an HKEX site covered by the HKEX Terms of Use, which prohibit automated access and data mining.",
        "https://www.hkex.com.hk/global/exchange/terms-of-use", "2026-11-20"),
    HostTermsRule(
        "cninfo.com.cn", "unverified_commercial", "cninfo_terms_unverified",
        "cninfo (Shenzhen Securities Information) publishes only a copyright notice and disclaimer; automated commercial access is not verified.",
        "https://www.cninfo.com.cn/", "2026-11-20"),
    HostTermsRule(
        "sse.com.cn", "unverified_commercial", "sse_terms_unverified",
        "SSE legal notice allows non-commercial browsing and downloading and prohibits commercial use and electronic capture without written permission.",
        "https://www.sse.com.cn/home/legal/", "2026-11-20"),
    HostTermsRule(
        "szse.cn", "unverified_commercial", "szse_terms_unverified",
        "No SZSE terms permitting automated access to szse.cn or disc.static.szse.cn were found.",
        "https://www.szse.cn/", "2026-11-20"),
)

BLOCKED_MESSAGE = (
    "This site's terms do not permit automated access. Use the issuer's own investor-relations website copy, "
    "an SEC filing if the company files with the SEC, or ask the user to upload the document."
)


@dataclass(frozen=True)
class TermsDecision:
    allowed: bool
    status: str
    reason_code: str | None
    message: str
    evidence_url: str | None = None


_ALLOWED = TermsDecision(True, "allowed", None, "No site-terms restriction is registered for this host.")


def _normalize_host(value: str) -> str:
    host = (value or "").strip().lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return host


def _host_of(url: str) -> str:
    try:
        parts = urlsplit(url if "//" in url else "//" + url)
        return _normalize_host(parts.hostname or "")
    except ValueError:
        return ""


def _matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _overrides() -> list[dict]:
    raw = os.environ.get(OVERRIDES_ENV, "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return []
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        if not isinstance(item, dict) or item.get("status") != "allowed":
            continue
        host = _normalize_host(item.get("host") if isinstance(item.get("host"), str) else "")
        reference = item.get("permission_reference")
        if not host or not isinstance(reference, str) or not reference.strip():
            continue
        result.append({"host": host, "written": item.get("written_permission") is True})
    return result


def rule_for_host(host: str) -> HostTermsRule | None:
    host = _normalize_host(host)
    matches = [rule for rule in TERMS_RULES if _matches(host, rule.host)]
    return max(matches, key=lambda rule: len(rule.host)) if matches else None


def terms_decision(url: str) -> TermsDecision:
    """Decide whether automated retrieval of ``url`` is permitted by registered site terms."""
    host = _host_of(url)
    rule = rule_for_host(host) if host else None
    if rule is None:
        return _ALLOWED
    for override in _overrides():
        if not _matches(host, override["host"]):
            continue
        # A prohibited host is only released by an override that asserts written permission.
        if rule.status == "prohibited" and not override["written"]:
            continue
        return TermsDecision(True, "allowed", None, "Allowed by an operator permission override.", rule.evidence_url)
    return TermsDecision(False, rule.status, rule.reason_code, f"{rule.note} {BLOCKED_MESSAGE}", rule.evidence_url)
