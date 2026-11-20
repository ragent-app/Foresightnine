"""Shared fakes for runtime tests of the financial data tools (no network)."""
from __future__ import annotations

from hashlib import sha256
from typing import Any

from app.research_fin_tools import FinDataToolbox
from app.research_sources import ResearchSource

from tests.fin_data.helpers import NOW, UA, FakeTransport, Reply, json_reply, open_limiter, fixture_bytes  # noqa: F401

ACCOUNT = 'acct-1'


def memory_persist(store: list[ResearchSource] | None = None, owner: str = ACCOUNT):
    store = store if store is not None else []

    def persist(item, **kwargs):
        source = ResearchSource(source_id=f'src_{len(store) + 1}', owner_id=owner, obtained_at=NOW,
                                content_sha256=sha256(item.text.encode()).hexdigest(),
                                retrieval_method=kwargs.get('retrieval_method', 'user_supplied'),
                                response_sha256=kwargs.get('response_sha256'),
                                provenance=kwargs.get('provenance') or {}, **item.model_dump())
        store.append(source)
        return source
    persist.store = store  # type: ignore[attr-defined]
    return persist


def make_toolbox(*, market_key: str | None = 'byok-secret-123', twelve: FakeTransport | None = None,
                 sec: FakeTransport | None = None, sec_stream=None, persist=None, user_agent: str | None = UA,
                 disabled_reason: str | None = None, reserve_call=lambda: None, reserve_sec=lambda: True,
                 policy=None, limiter=None, **kwargs: Any) -> FinDataToolbox:
    return FinDataToolbox(
        account_id=ACCOUNT, market_key=market_key, sec_user_agent=user_agent, persist=persist or memory_persist(),
        reserve_call=reserve_call, reserve_sec=reserve_sec, now=lambda: NOW, disabled_reason=disabled_reason,
        limiter=limiter or open_limiter(), twelve_transport=twelve or FakeTransport(Reply()),
        sec_transport=sec or FakeTransport(Reply()), sec_stream_transport=sec_stream, policy=policy, **kwargs)


def sec_binary_transport(plan=None, calls=None):
    """Fake for app.fin_data.adapters.base._default_binary_transport serving SEC fixtures.

    ``plan`` is a list of HTTP statuses consumed per data request (default 200). Honors the streaming sink."""
    from app.openai_compatible import BinaryHttpResponse
    plan = list(plan or [])

    def fake(method, url, headers, body, timeout, **kwargs):
        if calls is not None:
            calls.append(url)
        status = plan.pop(0) if plan else 200
        if status != 200:
            return BinaryHttpResponse(status, b'', {})
        if '/companyconcept/' in url:
            import json as _json
            doc = _json.loads(fixture_bytes('sec_companyfacts.json'))
            taxonomy, tag = url.rsplit('/', 2)[-2], url.rsplit('/', 1)[-1].removesuffix('.json')
            node = doc['facts'].get(taxonomy, {}).get(tag)
            if node is None:
                return BinaryHttpResponse(404, b'', {})
            data = _json.dumps({'cik': doc['cik'], 'taxonomy': taxonomy, 'tag': tag, **node}).encode()
            return BinaryHttpResponse(200, data, {'content-type': 'application/json'})
        name = ('sec_companyfacts.json' if 'companyfacts' in url else 'sec_submissions.json' if 'submissions' in url
                else 'sec_tickers.json')
        data = fixture_bytes(name)
        sink = kwargs.get('sink')
        if sink is not None:
            sink.write(data)
            return BinaryHttpResponse(200, b'', {'content-type': 'application/json'})
        return BinaryHttpResponse(200, data, {'content-type': 'application/json'})
    return fake
