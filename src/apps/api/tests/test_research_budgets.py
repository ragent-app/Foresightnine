from threading import Event
from time import monotonic

import pytest

from app.glm_mcp_search import GlmCodingMcpSearchEvidenceProvider
from app.phase3 import ProviderFailure


def test_expired_search_deadline_and_cancellation_do_not_send_requests():
    calls = []
    provider = GlmCodingMcpSearchEvidenceProvider("synthetic-key", transport=lambda *args: calls.append(args))
    with pytest.raises(ProviderFailure, match="time budget"):
        provider.search("test query", deadline=monotonic() - 1)
    event = Event(); event.set()
    with pytest.raises(ProviderFailure, match="cancelled"):
        provider.search("test query", deadline=monotonic() + 20, cancel_event=event)
    assert calls == []
