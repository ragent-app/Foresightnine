"""Probe a synthetic anonymous workflow using mock provider and in-memory state.

Run from src/: uv run --project apps/api python docs/product/phase-a/probe_anonymous_isolation.py
This process explicitly disables real providers/storage and never writes to user data.
"""
import datetime
import json
import os
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SOURCE / "apps/api"))
os.environ.update({"FORESIGHTNINE_ENV": "development", "FORESIGHTNINE_STORAGE_MODE": "memory",
                   "FORESIGHTNINE_LLM_PROVIDER": "mock", "FORESIGHTNINE_SEARCH_PROVIDER": "client",
                   "FORESIGHTNINE_ABUSE_MODE": "memory", "FORESIGHTNINE_PUBLIC_HARDENING": "false"})

from fastapi.testclient import TestClient  # noqa: E402
import app.main as main  # noqa: E402

if __name__ == "__main__":
    main.reset_state()
    a, b = TestClient(main.app), TestClient(main.app)
    session = a.post("/v1/sessions", headers={"Idempotency-Key":"phase-a-anon-session"}, json={})
    session.raise_for_status()
    challenge = a.post("/v1/challenges", headers={"Idempotency-Key":"phase-a-anon-challenge"},
                       json={"session_id":session.json()["session"]["session_id"],
                             "thesis":"Synthetic research record for isolation check only", "capital":1000,
                             "base_currency":"USD", "user_private_evidence_claims":False})
    challenge.raise_for_status()
    response = b.get("/v1/challenges/" + challenge.json()["challenge"]["challenge_id"])
    record = {"checked_at_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "environment":"two unauthenticated TestClients; in-memory storage; deterministic mock provider",
              "session_create_status":session.status_code, "challenge_create_status":challenge.status_code,
              "other_client_get_known_id_status":response.status_code,
              "limitations":["Does not assess whether identifiers are guessable.",
                             "Does not establish behavior with production storage or authenticated accounts."]}
    path = Path(__file__).with_name("anonymous-isolation-probe-2026-11-11.json")
    with path.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    print(json.dumps(record, indent=2))
