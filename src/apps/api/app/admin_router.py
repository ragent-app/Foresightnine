"""Restricted platform credential management endpoints."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .admin_auth import AdminAuthError, AdminGate
from .platform_secrets import (GLM_ENV, TUSHARE_ENV, TWELVEDATA_ENV, PlatformSecretError,
                               PlatformSecretStore, managed_secret, managed_secrets)


class ElevationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    passphrase: str | None = Field(default=None, max_length=256)


class SecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str = Field(min_length=5, max_length=512)

    @field_validator("value")
    @classmethod
    def safe_value(cls, value: str) -> str:
        if not value.isascii() or not all(33 <= ord(c) <= 126 for c in value):
            raise ValueError("Credential must contain printable non-space ASCII characters.")
        return value


def _probe(name: str, provider_id: str | None, key: str) -> tuple[bool, str]:
    # Only fixed targets and fixed request bodies are permitted. Never surface provider output.
    if provider_id:
        try:
            from .news.providers import probe_provider
            result = probe_provider(provider_id, key, timeout=10)
            return (True, "ok") if result.get("ok") is True else (False, "provider_check_failed")
        except Exception:
            return False, "provider_check_failed"
    try:
        if name == TWELVEDATA_ENV:
            response = requests.get("https://api.twelvedata.com/price", params={"symbol": "AAPL"}, headers={"Authorization": f"apikey {key}"}, timeout=10, allow_redirects=False)
            data = response.json()
            ok = response.status_code == 200 and isinstance(data, dict) and "price" in data
        elif name == TUSHARE_ENV:
            response = requests.post("https://api.tushare.pro/", json={"api_name": "trade_cal", "token": key, "params": {"exchange": "SSE", "start_date": "20240102", "end_date": "20240102"}, "fields": "cal_date"}, timeout=10, allow_redirects=False)
            data = response.json()
            ok = response.status_code == 200 and isinstance(data, dict) and data.get("code") == 0
        elif name == GLM_ENV:
            response = requests.post("https://open.bigmodel.cn/api/paas/v4/web_search", headers={"Authorization": f"Bearer {key}"}, json={"search_engine": "search_std", "search_query": "financial markets", "count": 1}, timeout=10, allow_redirects=False)
            data = response.json()
            ok = response.status_code == 200 and isinstance(data, dict) and isinstance(data.get("search_result"), list)
        else:
            return False, "unsupported"
        return (True, "ok") if ok else (False, "provider_check_failed")
    except Exception:
        return False, "provider_check_failed"


def create_admin_router(gate: AdminGate, store: PlatformSecretStore,
                        authenticate: Callable[[str | None], tuple[Any, Any]],
                        problem: Callable[..., Exception]) -> APIRouter:
    router = APIRouter(prefix="/v1/admin", tags=["admin"])

    def context(authorization: str | None, elevation: str | None, require: bool = True):
        account, session = authenticate(authorization)
        try:
            if require:
                gate.require(account, session, elevation)
        except AdminAuthError as error:
            store.log(account.account_id, "authorize", None, error.code)
            raise problem(error.status_code, error.code, error.message, retry_after=error.retry_after) from None
        return account, session

    def status_item(name: str) -> dict[str, Any]:
        spec = managed_secret(name)
        if spec is None:
            raise problem(404, "admin_key_not_found", "Unknown platform key.")
        try:
            value, source = store.resolve(name)
            record = store.record(name)
        except PlatformSecretError as error:
            raise problem(error.status_code, error.code, error.message) from None
        return {**asdict(spec), "configured": value is not None, "source": source,
                "key_hint": record.key_hint if source == "db" and record else None,
                "updated_at": record.updated_at if record else None,
                "last_test": record.last_test if record else None}

    @router.get("/status", operation_id="adminStatus")
    def admin_status(response: Response, authorization: str | None = Header(default=None), x_admin_elevation: str | None = Header(default=None)):
        response.headers["Cache-Control"] = "no-store"
        account, session = context(authorization, x_admin_elevation, False)
        return gate.status(account, session, x_admin_elevation)

    @router.post("/elevation", operation_id="openAdminElevation")
    def admin_open(body: ElevationInput, request: Request, response: Response, authorization: str | None = Header(default=None)):
        response.headers["Cache-Control"] = "no-store"
        account, session = context(authorization, None, False)
        try:
            result = gate.open(account, session, body.passphrase, request.client.host if request.client else "unknown")
            store.log(account.account_id, "elevation", None, "ok")
            return result
        except AdminAuthError as error:
            store.log(account.account_id, "elevation", None, error.code)
            raise problem(error.status_code, error.code, error.message, retry_after=error.retry_after) from None

    @router.delete("/elevation", operation_id="closeAdminElevation")
    def admin_close(response: Response, authorization: str | None = Header(default=None), x_admin_elevation: str | None = Header(default=None)):
        response.headers["Cache-Control"] = "no-store"
        account, session = context(authorization, x_admin_elevation, False)
        gate.revoke(account, session, x_admin_elevation)
        store.log(account.account_id, "revoke_elevation", None, "ok")
        return {"ok": True}

    @router.get("/keys", operation_id="listAdminKeys")
    def admin_keys(response: Response, authorization: str | None = Header(default=None), x_admin_elevation: str | None = Header(default=None)):
        response.headers["Cache-Control"] = "no-store"
        context(authorization, x_admin_elevation)
        return {"keys": [status_item(item.name) for item in managed_secrets()]}

    @router.put("/keys/{name}", operation_id="setAdminKey")
    def admin_set(name: str, body: SecretInput, response: Response, authorization: str | None = Header(default=None), x_admin_elevation: str | None = Header(default=None)):
        response.headers["Cache-Control"] = "no-store"
        account, _ = context(authorization, x_admin_elevation)
        if managed_secret(name) is None:
            raise problem(404, "admin_key_not_found", "Unknown platform key.")
        try:
            store.set(name, body.value, actor=account.account_id)
        except PlatformSecretError as error:
            store.log(account.account_id, "set", name, error.code)
            raise problem(error.status_code, error.code, error.message) from None
        return status_item(name)

    @router.delete("/keys/{name}", operation_id="clearAdminKey")
    def admin_clear(name: str, response: Response, authorization: str | None = Header(default=None), x_admin_elevation: str | None = Header(default=None)):
        response.headers["Cache-Control"] = "no-store"
        account, _ = context(authorization, x_admin_elevation)
        if managed_secret(name) is None:
            raise problem(404, "admin_key_not_found", "Unknown platform key.")
        store.clear(name, actor=account.account_id)
        return status_item(name)

    @router.post("/keys/{name}/test", operation_id="testAdminKey")
    def admin_test(name: str, response: Response, authorization: str | None = Header(default=None), x_admin_elevation: str | None = Header(default=None)):
        response.headers["Cache-Control"] = "no-store"
        account, _ = context(authorization, x_admin_elevation)
        spec = managed_secret(name)
        if spec is None:
            raise problem(404, "admin_key_not_found", "Unknown platform key.")
        try:
            record = store.record(name)
            value, _ = store.resolve(name)
        except PlatformSecretError as error:
            raise problem(error.status_code, error.code, error.message) from None
        ok, reason = _probe(name, spec.provider_id, value) if value else (False, "not_configured")
        at = store._now()
        store.record_test(name, ok, reason, actor=account.account_id, expected_ciphertext=record.encrypted_value if record else None)
        return {"ok": ok, "reason": reason, "at": at}

    @router.get("/audit", operation_id="listAdminAudit")
    def admin_audit(response: Response, authorization: str | None = Header(default=None), x_admin_elevation: str | None = Header(default=None)):
        response.headers["Cache-Control"] = "no-store"
        context(authorization, x_admin_elevation)
        return {"entries": [v for _, v in sorted(store.audit.items(), reverse=True)[:200]]}

    return router
