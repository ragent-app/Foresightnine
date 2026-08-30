"""Request boundary, trusted-proxy and generic CAPTCHA abstractions."""
from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .abuse import AbuseBackend
from .telemetry import record_captcha_denial


class CaptchaVerifier(Protocol):
    def verify(self, token: str | None, request: Request) -> bool | None:
        """Return True, False, or None when the verifier is unavailable."""


class DeterministicCaptchaVerifier:
    def __init__(self, result: bool | None) -> None:
        self.result = result

    def verify(self, token: str | None, request: Request) -> bool | None:
        return self.result


class AcceptCaptchaVerifier(DeterministicCaptchaVerifier):
    def __init__(self) -> None:
        super().__init__(True)


class RejectCaptchaVerifier(DeterministicCaptchaVerifier):
    def __init__(self) -> None:
        super().__init__(False)


class UnavailableCaptchaVerifier(DeterministicCaptchaVerifier):
    def __init__(self) -> None:
        super().__init__(None)


class BodyTooLarge(Exception):
    """Raised while consuming a request stream that exceeds the byte cap."""


class SecurityConfigurationError(RuntimeError):
    """Raised when hardened transport configuration is unsafe."""


@dataclass(frozen=True)
class TrustedProxyConfig:
    networks: tuple[ipaddress._BaseNetwork, ...] = ()
    forwarded_header: str = "X-Forwarded-For"


class TrustedProxyResolver:
    def __init__(self, config: TrustedProxyConfig | None = None) -> None:
        self.config = config or TrustedProxyConfig()

    def resolve(self, request: Request) -> str:
        peer = request.client.host if request.client else "0.0.0.0"
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return "0.0.0.0"
        if not any(peer_address in network for network in self.config.networks):
            return peer
        forwarded = request.headers.get(self.config.forwarded_header, "")
        candidates: list[str] = []
        for raw in forwarded.split(","):
            value = raw.strip()
            try:
                ipaddress.ip_address(value)
            except ValueError:
                continue
            candidates.append(value)
        # The right-most address is the proxy closest to this server. Walk
        # left until the first address not in a configured proxy network.
        for value in reversed(candidates):
            address = ipaddress.ip_address(value)
            if not any(address in network for network in self.config.networks):
                return value
        return candidates[0] if candidates else peer


def _parse_networks(raw: str) -> tuple[ipaddress._BaseNetwork, ...]:
    networks: list[ipaddress._BaseNetwork] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _parse_origins(raw: str, *, hardened: bool) -> tuple[str, ...]:
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not hardened or not values:
        return values
    normalized: list[str] = []
    for value in values:
        try:
            parts = urlsplit(value)
            _ = parts.port
        except ValueError as error:
            raise SecurityConfigurationError("Hardened CORS origins must be valid HTTPS origins.") from error
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise SecurityConfigurationError("Hardened CORS origins must be valid HTTPS origins.")
        normalized.append(value[:-1] if value.endswith("/") else value)
    return tuple(normalized)


@dataclass(frozen=True)
class SecurityConfig:
    max_body_bytes: int = 256 * 1024
    allowed_origins: tuple[str, ...] = ()
    trusted_proxies: TrustedProxyConfig = TrustedProxyConfig()
    require_captcha: bool = False


def build_security_config() -> SecurityConfig:
    environment = os.getenv("FORESIGHTNINE_ENV", "development").lower()
    require_captcha = os.getenv("FORESIGHTNINE_PUBLIC_HARDENING", "").lower() in {"1", "true", "yes"} or environment == "production"
    try:
        max_body = int(os.getenv("FORESIGHTNINE_MAX_BODY_BYTES", str(256 * 1024)))
    except ValueError:
        max_body = 256 * 1024
    origins = _parse_origins(
        os.getenv("FORESIGHTNINE_CORS_ORIGINS", ""),
        hardened=require_captcha,
    )
    return SecurityConfig(
        max_body_bytes=max(1024, max_body),
        allowed_origins=origins,
        trusted_proxies=TrustedProxyConfig(_parse_networks(os.getenv("FORESIGHTNINE_TRUSTED_PROXY_CIDRS", ""))),
        require_captcha=require_captcha,
    )


def build_captcha_verifier(config: SecurityConfig | None = None) -> CaptchaVerifier:
    config = config or build_security_config()
    mode = os.getenv("FORESIGHTNINE_CAPTCHA_MODE", "unavailable" if config.require_captcha else "accept").lower()
    if mode == "accept" and not config.require_captcha:
        return AcceptCaptchaVerifier()
    if mode == "reject":
        return RejectCaptchaVerifier()
    return UnavailableCaptchaVerifier()


def _boundary_error(status: int, code: str, message: str, request: Request, *, retry_after: int | None = None) -> JSONResponse:
    from hashlib import sha256
    request_id = sha256(f"{request.method}:{request.url.path}:{code}".encode()).hexdigest()[:32]
    response = JSONResponse(status_code=status, content={
        "schema_version": "v1", "error_code": code, "message": message,
        "request_id": request_id, "retryable": status in {429, 503}, "details": [],
    })
    if retry_after is not None:
        response.headers["Retry-After"] = str(max(1, retry_after))
    _set_security_headers(response)
    return response


def _set_security_headers(response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


class SecurityBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, config: SecurityConfig, abuse: AbuseBackend, captcha: CaptchaVerifier, resolver: TrustedProxyResolver, cleanup: Callable[[], object] | None = None) -> None:
        super().__init__(app)
        self.config = config
        self.abuse = abuse
        self.captcha = captcha
        self.resolver = resolver
        self.cleanup = cleanup

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self.cleanup is not None:
            try:
                self.cleanup()
            except Exception:
                # Cleanup is best effort at the transport boundary; its own
                # hook records failures without making healthy requests fail.
                pass
        client_ip = self.resolver.resolve(request)
        request.state.client_ip = client_ip
        session_id = request.headers.get("X-Session-ID")
        body: bytes = b""
        if request.method in {"POST", "PUT", "PATCH"}:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return _boundary_error(415, "unsupported_media_type", "JSON requests are required.", request)
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > self.config.max_body_bytes:
                        return _boundary_error(413, "request_too_large", "Request body exceeds the configured limit.", request)
                except ValueError:
                    return _boundary_error(400, "invalid_request", "Request headers are invalid.", request)
            # Content-Length is only a fast rejection.  ASGI servers may
            # receive chunked requests without it, so cap the receive stream
            # before Starlette's body helper can concatenate an unbounded
            # payload in memory.  A single transport chunk is owned by the
            # server already; we never retain more than max_body_bytes here.
            original_receive = request._receive
            received_bytes = 0

            async def limited_receive() -> dict[str, Any]:
                nonlocal received_bytes
                message = await original_receive()
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"")
                    received_bytes += len(chunk)
                    if received_bytes > self.config.max_body_bytes:
                        raise BodyTooLarge
                return message

            request._receive = limited_receive
            try:
                body = await request.body()
            except BodyTooLarge:
                return _boundary_error(413, "request_too_large", "Request body exceeds the configured limit.", request)
            if len(body) > self.config.max_body_bytes:
                return _boundary_error(413, "request_too_large", "Request body exceeds the configured limit.", request)
            try:
                decoded = json.loads(body) if body else {}
                if isinstance(decoded, dict) and isinstance(decoded.get("session_id"), str):
                    session_id = decoded["session_id"]
            except (ValueError, TypeError):
                pass
            request.state.session_id = session_id
        else:
            request.state.session_id = session_id
        # The bounded market adapter is intentionally kept outside the
        # generated /v1 contract, but it is still a public data-plane route
        # and must receive the same abuse controls as every API endpoint.
        if request.url.path.startswith("/v1/") or request.url.path == "/snapshots":
            decision = self.abuse.check_request(client_ip=client_ip, session_id=session_id)
            if not decision.available:
                return _boundary_error(
                    503,
                    "abuse_backend_unavailable",
                    "Abuse controls are temporarily unavailable.",
                    request,
                    retry_after=decision.retry_after or 1,
                )
            if not decision.allowed:
                return _boundary_error(429, "rate_limited", "Request rate exceeded; retry later.", request, retry_after=decision.retry_after)
            if request.method in {"POST", "PUT", "PATCH"}:
                verifier = self.captcha() if callable(self.captcha) else self.captcha
                if self.config.require_captcha or not isinstance(verifier, AcceptCaptchaVerifier):
                    result = verifier.verify(request.headers.get("X-Captcha-Token"), request)
                    if result is not True:
                        record_captcha_denial(reason="unavailable" if result is None else "rejected")
                        return _boundary_error(403, "captcha_required", "Request could not be verified.", request, retry_after=60)
        response = await call_next(request)
        _set_security_headers(response)
        return response
