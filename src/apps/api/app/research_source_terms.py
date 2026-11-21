"""Versioned registry of site terms for automated retrieval (plan 6.10).

A host is blocked for automated access when its published terms prohibit it
('prohibited') or when permission for a public commercial product has not been
verified ('unverified_commercial'). Operators can record written permission through
FORESIGHTNINE_SOURCE_TERMS_OVERRIDES; user uploads and the issuer's own
investor-relations copy remain the compliant routes.

Personal mode (``user_initiated_only``). Hosts whose status is 'unverified_commercial' (cninfo, SSE, SZSE) can be
released for personal, non-commercial, user-initiated retrieval of single documents. An override entry such as::

    FORESIGHTNINE_SOURCE_TERMS_OVERRIDES=[{"host":"cninfo.com.cn","status":"user_initiated_only",
      "permission_reference":"personal non-commercial use, user-initiated single documents"},
      {"host":"sse.com.cn","status":"user_initiated_only","permission_reference":"..."},
      {"host":"szse.cn","status":"user_initiated_only","permission_reference":"..."}]

allows only a URL the user pasted into the manual fetch endpoint or wrote in the research question (one document per
URL, no crawling). Leads discovered by the agent on those hosts stay non-acquirable. HKEX and HKEXnews ('prohibited')
ignore this mode; only an override with ``"status":"allowed"`` and ``"written_permission":true`` releases them.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Literal
from urllib.parse import urlsplit

TERMS_REGISTRY_VERSION = "2026-11-21.1"
OVERRIDES_ENV = "FORESIGHTNINE_SOURCE_TERMS_OVERRIDES"

USER_INITIATED_ONLY = "user_initiated_only"
Status = Literal["prohibited", "unverified_commercial", "allowed", "user_initiated_only"]


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
        if not isinstance(item, dict) or item.get("status") not in ("allowed", USER_INITIATED_ONLY):
            continue
        host = _normalize_host(item.get("host") if isinstance(item.get("host"), str) else "")
        reference = item.get("permission_reference")
        if not host or not isinstance(reference, str) or not reference.strip():
            continue
        result.append({"host": host, "written": item.get("written_permission") is True, "mode": item["status"],
                       "reference": reference.strip()[:300]})
    return result


# Rule hosts for which the current call chain is a single user-initiated retrieval (manual fetch or a URL the user wrote).
_USER_SCOPE: ContextVar[frozenset[str] | None] = ContextVar("foresightnine_user_initiated_scope", default=None)


@contextmanager
def user_initiated_scope(url: str) -> Iterator[None]:
    """Mark the enclosed retrieval of one user-supplied ``url`` as user-initiated (same-registry-host redirects included)."""
    rule = rule_for_host(_host_of(url))
    token = _USER_SCOPE.set(frozenset({rule.host}) if rule is not None else frozenset())
    try:
        yield
    finally:
        _USER_SCOPE.reset(token)


def _user_mode_override(host: str, rule: HostTermsRule | None) -> dict | None:
    """The personal-mode override covering ``host``; never applies to prohibited hosts."""
    if rule is None or rule.status != "unverified_commercial":
        return None
    return next((o for o in _overrides() if o["mode"] == USER_INITIATED_ONLY and _matches(host, o["host"])), None)


def _is_user_initiated(rule: HostTermsRule | None, initiator: str | None) -> bool:
    scope = _USER_SCOPE.get()
    return initiator == "user" or (rule is not None and scope is not None and rule.host in scope)


def user_initiated_basis(url: str, initiator: str | None = None) -> dict | None:
    """Provenance fields for a retrieval allowed only because it was user-initiated, else None."""
    host = _host_of(url)
    rule = rule_for_host(host) if host else None
    override = _user_mode_override(host, rule)
    if override is None or not _is_user_initiated(rule, initiator):
        return None
    return {"retrieval_initiator": "user", "terms_mode": USER_INITIATED_ONLY, "permission_reference": override["reference"]}


def rule_for_host(host: str) -> HostTermsRule | None:
    host = _normalize_host(host)
    matches = [rule for rule in TERMS_RULES if _matches(host, rule.host)]
    return max(matches, key=lambda rule: len(rule.host)) if matches else None


USER_INITIATED_MESSAGE = (
    "Personal mode: this site may be fetched only as a single document that the user supplied (pasted in the manual "
    "fetch form or written in the question). Agent-discovered links on it are not fetched automatically; ask the user "
    "to paste the exact document link or upload the file."
)


def terms_decision(url: str, *, initiator: str | None = None) -> TermsDecision:
    """Decide whether automated retrieval of ``url`` is permitted by registered site terms.

    ``initiator='user'`` (or an enclosing ``user_initiated_scope``) marks a URL the user supplied themselves.
    """
    host = _host_of(url)
    rule = rule_for_host(host) if host else None
    if rule is None:
        return _ALLOWED
    for override in _overrides():
        if override["mode"] != "allowed" or not _matches(host, override["host"]):
            continue
        # A prohibited host is only released by an override that asserts written permission.
        if rule.status == "prohibited" and not override["written"]:
            continue
        return TermsDecision(True, "allowed", None, "Allowed by an operator permission override.", rule.evidence_url)
    if _user_mode_override(host, rule) is not None:
        if _is_user_initiated(rule, initiator):
            return TermsDecision(True, USER_INITIATED_ONLY, None,
                                 "Allowed for a user-initiated single document (personal mode).", rule.evidence_url)
        return TermsDecision(False, USER_INITIATED_ONLY, "source_user_initiated_only",
                             f"{rule.note} {USER_INITIATED_MESSAGE}", rule.evidence_url)
    return TermsDecision(False, rule.status, rule.reason_code, f"{rule.note} {BLOCKED_MESSAGE}", rule.evidence_url)
