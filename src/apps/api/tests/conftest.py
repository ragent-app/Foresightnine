from __future__ import annotations

from pathlib import Path
import ipaddress
import os
import socket
import sys

import os
import pytest
os.environ.setdefault("FORESIGHTNINE_GDELT_ENABLED", "0")

API_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = API_ROOT.parents[1]
sys.path.insert(0, str(API_ROOT))

from app.main import reset_state


@pytest.fixture(autouse=True)
def clean_in_memory_state() -> None:
    from app.research_news_tool import reset_news_search_cache
    reset_state()
    reset_news_search_cache()
    yield
    reset_news_search_cache()


@pytest.fixture(autouse=True)
def hermetic_cn_probe(monkeypatch):
    """Tushare/BaoStock reachability is probed over TCP in production; tests never open sockets for it."""
    from app.fin_data import reachability
    monkeypatch.setattr(reachability, "tcp_connect", lambda host, port, timeout: None)
    reachability.default_probe().clear()
    yield
    reachability.default_probe().clear()


@pytest.fixture(autouse=True)
def enforce_optional_offline_network(monkeypatch):
    """For offline acceptance, fail even if application code catches a blocked request."""
    if os.getenv('FORESIGHTNINE_OFFLINE_TESTS') != '1':
        yield
        return
    attempted = []
    original_resolve = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def local(host):
        if host is None or host == 'localhost':
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            return False

    def reject(host):
        attempted.append(str(host))
        raise AssertionError('Offline acceptance blocked an unexpected external network request')

    def resolve(host, *args, **kwargs):
        if not local(host):
            reject(host)
        return original_resolve(host, *args, **kwargs)

    def connect(sock, address):
        if isinstance(address, tuple) and not local(address[0]):
            reject(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if isinstance(address, tuple) and not local(address[0]):
            reject(address[0])
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)
    yield
    assert not attempted, 'Offline acceptance attempted an external network request; mock its provider or adapter.'
