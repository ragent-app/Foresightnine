"""Run only on loopback with synthetic, isolated in-memory data."""
from api_fixture import build, install
from app import main
topics,sources=build();install(topics,sources)
main._authenticated_account=lambda authorization: __import__('types').SimpleNamespace(account_id=(authorization or '').removeprefix('Bearer ').split('_token')[0])
app=main.app
