import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.fin_data.policy import FinDataPolicy, PolicyRule, default_policy
from app.fin_data.ratelimit import InProcessRateLimiter, ProviderLimit

FIX = Path(__file__).parent / "fixtures"
UA = "ForesightNine Test ops@example.com"
NOW = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)  # Friday 11:00 in Shanghai, session open


def fixture_bytes(name: str) -> bytes:
    return (FIX / name).read_bytes()


@dataclass
class Reply:
    status_code: int = 200
    body: bytes = b"{}"
    headers: dict = field(default_factory=lambda: {"content-type": "application/json"})


class FakeTransport:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((url, dict(headers)))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def json_reply(name: str, status: int = 200) -> Reply:
    return Reply(status, fixture_bytes(name))


def open_limiter() -> InProcessRateLimiter:
    return InProcessRateLimiter({"sec_edgar": ProviderLimit(1000, 1000), "twelve_data": ProviderLimit(1000, 1000, 1000)},
                                now=lambda: NOW)


def policy_with(*rules: PolicyRule) -> FinDataPolicy:
    return FinDataPolicy(rules=list(rules))


def efts_policy() -> FinDataPolicy:
    base = default_policy()
    return FinDataPolicy(version="t", rules=[*base.rules, PolicyRule(provider="sec_edgar", category="filing_search",
                                                                    store=True, display=True, model_input=True)])


class ConceptTransport:
    """Serves SEC companyconcept files sliced from a companyfacts fixture (404 for tags the issuer never reported).

    Any other URL is answered from ``others`` (``{substring: Reply}``) so identity lookups can share the transport.
    """

    def __init__(self, facts_fixture: str = "sec_companyfacts.json", others: dict | None = None):
        self.doc = json.loads(fixture_bytes(facts_fixture))
        self.others, self.calls = others or {}, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((url, dict(headers)))
        if "/companyconcept/" in url:
            taxonomy, tag = url.rsplit("/", 2)[-2], url.rsplit("/", 1)[-1].removesuffix(".json")
            node = self.doc["facts"].get(taxonomy, {}).get(tag)
            if node is None:
                return Reply(404, b"{}")
            payload = {"cik": self.doc["cik"], "taxonomy": taxonomy, "tag": tag, "entityName": self.doc["entityName"], **node}
            return Reply(200, json.dumps(payload).encode())
        for key, reply in self.others.items():
            if key in url:
                return reply
        return Reply(404, b"{}")

    @property
    def concept_urls(self) -> list[str]:
        return [u for u, _ in self.calls if "/companyconcept/" in u]
