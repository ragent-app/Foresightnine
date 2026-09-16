from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "google_play_internal_release.py"
SPEC = importlib.util.spec_from_file_location("google_play_internal_release", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, *, fail_update: bool = False):
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_update = fail_update

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "/subscriptions/" in url:
            return FakeResponse(200, {"basePlans": [{"basePlanId": "monthly", "state": "ACTIVE"}]})
        return FakeResponse(200, {"releases": [{"releaseName": "0.1.0"}]})

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/edits"):
            return FakeResponse(200, {"id": "edit-1"})
        if "/bundles" in url:
            return FakeResponse(200, {"versionCode": 2})
        if url.endswith(":commit"):
            return FakeResponse(200, {"id": "edit-1"})
        raise AssertionError(url)

    def put(self, url: str, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return FakeResponse(409 if self.fail_update else 200, {"track": MODULE.TRACK})

    def delete(self, url: str, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        return FakeResponse(204)


def artifact(tmp_path: Path, *, signed: bool) -> object:
    path = tmp_path / "app.aab"
    path.write_bytes(b"synthetic-aab")
    return MODULE.ArtifactCheck(path=path, sha256="0" * 64, size=13, signed=signed)


def test_unsigned_artifact_stops_before_any_play_mutation(tmp_path: Path):
    session = FakeSession()
    publisher = MODULE.InternalTrackPublisher(session)
    with pytest.raises(ValueError, match="unsigned"):
        publisher.publish(artifact(tmp_path, signed=False), "0.2.0")
    assert session.calls == []


def test_signed_artifact_is_committed_only_to_internal(tmp_path: Path):
    session = FakeSession()
    publisher = MODULE.InternalTrackPublisher(session)
    assert publisher.publish(artifact(tmp_path, signed=True), "0.2.0") == 2
    methods = [call[0] for call in session.calls]
    assert methods == ["POST", "POST", "PUT", "POST"]
    track_call = session.calls[2]
    assert track_call[1].endswith("/tracks/internal")
    assert "/production" not in track_call[1]
    assert track_call[2]["json"] == {
        "track": "internal",
        "releases": [{"name": "0.2.0", "versionCodes": ["2"], "status": "completed"}],
    }


def test_failed_track_update_discards_the_edit(tmp_path: Path):
    session = FakeSession(fail_update=True)
    publisher = MODULE.InternalTrackPublisher(session)
    with pytest.raises(MODULE.PlayApiError, match="HTTP 409"):
        publisher.publish(artifact(tmp_path, signed=True), "0.2.0")
    assert [call[0] for call in session.calls][-1] == "DELETE"
    assert session.calls[-1][1].endswith("/edits/edit-1")


def test_readiness_requires_both_active_products_and_reads_internal():
    session = FakeSession()
    result = MODULE.InternalTrackPublisher(session).readiness()
    assert result == {
        "products": {"foresightnine_pro": "ACTIVE", "foresightnine_max": "ACTIVE"},
        "release_count": 1,
    }
    assert all("production" not in url for _, url, _ in session.calls)


def test_artifact_inspection_treats_unsigned_jar_as_unsigned(tmp_path: Path):
    path = tmp_path / "candidate.aab"
    path.write_bytes(b"bundle")
    completed = SimpleNamespace(returncode=0, stdout="jar is unsigned.", stderr="")
    with patch.object(MODULE.subprocess, "run", return_value=completed):
        result = MODULE.inspect_artifact(path)
    assert result.signed is False


def test_artifact_inspection_accepts_verified_self_signed_upload_key(tmp_path: Path):
    path = tmp_path / "candidate.aab"
    path.write_bytes(b"bundle")
    completed = SimpleNamespace(
        returncode=4,
        stdout="jar verified, with signer errors.",
        stderr="This jar contains a self-signed certificate.",
    )
    with patch.object(MODULE.subprocess, "run", return_value=completed):
        result = MODULE.inspect_artifact(path)
    assert result.signed is True
