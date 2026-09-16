"""Fail-closed Google Play internal-track release publisher.

The command validates a signed Android App Bundle by default. A release is
created only when ``--publish`` is supplied. Package and track are constants so
this tool cannot be repointed at another app or the production track.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account


PACKAGE_NAME = "com.foresightnine.mobile"
TRACK = "internal"
ANDROIDPUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
CANONICAL_CREDENTIAL = Path("/home/lxx/.config/google-play/service-account.json")
API_ROOT = "https://androidpublisher.googleapis.com/androidpublisher/v3"
UPLOAD_ROOT = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3"
PRODUCTS = ("foresightnine_pro", "foresightnine_max")


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> dict[str, Any]: ...


class HttpSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...
    def post(self, url: str, **kwargs: Any) -> HttpResponse: ...
    def put(self, url: str, **kwargs: Any) -> HttpResponse: ...
    def delete(self, url: str, **kwargs: Any) -> HttpResponse: ...


class PlayApiError(RuntimeError):
    def __init__(self, operation: str, status_code: int):
        super().__init__(f"{operation} returned HTTP {status_code}")
        self.operation = operation
        self.status_code = status_code


@dataclass(frozen=True)
class ArtifactCheck:
    path: Path
    sha256: str
    size: int
    signed: bool


def _response_json(response: HttpResponse, operation: str, expected: tuple[int, ...]) -> dict[str, Any]:
    if response.status_code not in expected:
        raise PlayApiError(operation, response.status_code)
    try:
        payload = response.json()
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise RuntimeError(f"{operation} returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{operation} returned an invalid response shape")
    return payload


def inspect_artifact(path: Path, *, jarsigner: str = "jarsigner") -> ArtifactCheck:
    resolved = path.expanduser().resolve(strict=True)
    if resolved.suffix.lower() != ".aab" or not resolved.is_file():
        raise ValueError("release artifact must be an existing .aab file")
    digest_state = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest_state.update(chunk)
    digest = digest_state.hexdigest()
    result = subprocess.run(
        [jarsigner, "-verify", "-strict", str(resolved)],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    output = f"{result.stdout}\n{result.stderr}".lower()
    # Upload keys are commonly self-signed, for which jarsigner -strict exits
    # 4 even though the archive signature is intact. Treat the explicit
    # verification sentence as authoritative and still reject unsigned or
    # malformed archives before opening a Play edit.
    signed = "jar verified" in output and "jar is unsigned" not in output
    return ArtifactCheck(path=resolved, sha256=digest, size=resolved.stat().st_size, signed=signed)


class InternalTrackPublisher:
    def __init__(self, session: HttpSession):
        self.session = session
        self.app_root = f"{API_ROOT}/applications/{PACKAGE_NAME}"

    def readiness(self) -> dict[str, Any]:
        product_states: dict[str, str] = {}
        for product_id in PRODUCTS:
            response = self.session.get(f"{self.app_root}/subscriptions/{product_id}", timeout=30)
            payload = _response_json(response, f"read subscription {product_id}", (200,))
            plan = next(
                (item for item in payload.get("basePlans", []) if item.get("basePlanId") == "monthly"),
                None,
            )
            product_states[product_id] = str(plan.get("state")) if isinstance(plan, dict) else "MISSING"
        response = self.session.get(f"{self.app_root}/tracks/{TRACK}/releases", timeout=30)
        track_payload = _response_json(response, "read internal track", (200,))
        return {"products": product_states, "release_count": len(track_payload.get("releases", []))}

    def create_edit(self) -> str:
        response = self.session.post(f"{self.app_root}/edits", json={}, timeout=30)
        payload = _response_json(response, "create edit", (200,))
        edit_id = payload.get("id")
        if not isinstance(edit_id, str) or not edit_id:
            raise RuntimeError("create edit returned no edit id")
        return edit_id

    def upload_bundle(self, edit_id: str, stream: BinaryIO) -> int:
        url = f"{UPLOAD_ROOT}/applications/{PACKAGE_NAME}/edits/{edit_id}/bundles"
        response = self.session.post(
            url,
            params={"uploadType": "media"},
            data=stream,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120,
        )
        payload = _response_json(response, "upload bundle", (200,))
        version_code = payload.get("versionCode")
        try:
            parsed = int(version_code)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("upload bundle returned no version code") from exc
        if parsed <= 0:
            raise RuntimeError("upload bundle returned an invalid version code")
        return parsed

    def update_internal_track(self, edit_id: str, version_code: int, release_name: str) -> None:
        payload = {
            "track": TRACK,
            "releases": [{
                "name": release_name,
                "versionCodes": [str(version_code)],
                "status": "completed",
            }],
        }
        response = self.session.put(
            f"{self.app_root}/edits/{edit_id}/tracks/{TRACK}",
            json=payload,
            timeout=30,
        )
        _response_json(response, "update internal track", (200,))

    def commit(self, edit_id: str) -> None:
        response = self.session.post(f"{self.app_root}/edits/{edit_id}:commit", json={}, timeout=30)
        _response_json(response, "commit edit", (200,))

    def discard(self, edit_id: str) -> None:
        response = self.session.delete(f"{self.app_root}/edits/{edit_id}", timeout=30)
        if response.status_code not in (200, 204, 404):
            raise PlayApiError("discard edit", response.status_code)

    def publish(self, artifact: ArtifactCheck, release_name: str) -> int:
        if not artifact.signed:
            raise ValueError("AAB is unsigned; no Google Play edit was created")
        edit_id: str | None = None
        try:
            edit_id = self.create_edit()
            with artifact.path.open("rb") as stream:
                version_code = self.upload_bundle(edit_id, stream)
            self.update_internal_track(edit_id, version_code, release_name)
            self.commit(edit_id)
            return version_code
        except Exception:
            if edit_id is not None:
                try:
                    self.discard(edit_id)
                except Exception:
                    pass
            raise


def authorized_session(credential_path: Path = CANONICAL_CREDENTIAL) -> AuthorizedSession:
    credentials = service_account.Credentials.from_service_account_file(
        str(credential_path), scopes=[ANDROIDPUBLISHER_SCOPE]
    )
    return AuthorizedSession(credentials)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate or publish the signed ForesightNine AAB to internal testing.")
    parser.add_argument("--aab", required=True, type=Path)
    parser.add_argument("--release-name", default="0.2.0")
    parser.add_argument("--publish", action="store_true", help="Create, upload, update internal, and commit the edit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        artifact = inspect_artifact(args.aab)
        print(f"PACKAGE={PACKAGE_NAME}")
        print(f"TRACK={TRACK}")
        print(f"AAB_SHA256={artifact.sha256}")
        print(f"AAB_BYTES={artifact.size}")
        print(f"AAB_SIGNATURE={'SIGNED' if artifact.signed else 'UNSIGNED'}")
        if not artifact.signed:
            print("RESULT=STOPPED_BEFORE_NETWORK_MUTATION_UNSIGNED_AAB")
            return 2
        publisher = InternalTrackPublisher(authorized_session())
        readiness = publisher.readiness()
        for product_id in PRODUCTS:
            print(f"{product_id}_MONTHLY_STATE={readiness['products'][product_id]}")
        print(f"INTERNAL_RELEASE_COUNT={readiness['release_count']}")
        if any(readiness["products"][product_id] != "ACTIVE" for product_id in PRODUCTS):
            print("RESULT=STOPPED_BEFORE_EDIT_INACTIVE_PRODUCT")
            return 3
        if not args.publish:
            print("RESULT=VALIDATION_PASS_NO_MUTATION")
            return 0
        version_code = publisher.publish(artifact, args.release_name)
        print(f"PUBLISHED_VERSION_CODE={version_code}")
        print("RESULT=INTERNAL_RELEASE_COMMITTED")
        return 0
    except (OSError, ValueError, RuntimeError, PlayApiError) as exc:
        print(f"ERROR_TYPE={type(exc).__name__}", file=sys.stderr)
        print(f"ERROR={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
