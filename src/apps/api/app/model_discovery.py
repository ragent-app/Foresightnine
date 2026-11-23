"""Read model metadata through the same public-HTTPS boundary as personal generation."""
import json
import os
import re
from time import monotonic
from typing import Literal
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict, Field

from .anthropic_messages import _endpoint as messages_endpoint
from .model_credentials import AccountModelSettings, ModelCredentialError
from .openai_compatible import _default_transport, _endpoint as chat_endpoint, personal_model_endpoint
from .phase3 import ProviderFailure


class ModelDiscoveryRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    protocol: Literal["openai_chat_completions", "anthropic_messages"]
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, min_length=8, max_length=512, repr=False)
    version: str | None = Field(default=None, pattern=r"^[!-~]{1,64}$")
    auth_scheme: Literal["bearer", "x-api-key"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)


class DiscoveredModel(BaseModel):
    id: str
    efforts: list[str] | None = None
    effort_source: Literal["model_api", "official_docs"] | None = None


class ModelCatalog(BaseModel):
    models: list[DiscoveredModel]
    upstream_status: int | None = None


def _metadata(item: object, secret: str) -> DiscoveredModel | None:
    if not isinstance(item, dict):
        return None
    name = item.get("id")
    if not isinstance(name, str) or not 0 < len(name) <= 128 or not name.isprintable() or secret in name:
        return None
    levels = item.get("supported_reasoning_efforts", item.get("supported_reasoning_levels", item.get("reasoning_efforts")))
    capabilities = item.get("capabilities")
    if isinstance(capabilities, dict):
        effort = capabilities.get("effort")
        if isinstance(effort, dict):
            levels = [] if effort.get("supported") is False else [
                key for key, value in effort.items() if isinstance(value, dict) and value.get("supported") is True
            ]
        elif levels is None:
            levels = capabilities.get("reasoning_efforts")
    choices = None
    if isinstance(levels, list):
        choices = []
        for level in levels:
            if isinstance(level, dict):
                level = level.get("effort")
            if isinstance(level, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", level) and secret not in level and level not in choices:
                choices.append(level)
    return DiscoveredModel(id=name, efforts=choices, effort_source="model_api" if choices is not None else None)


def _official_efforts(model_id: str, timeout: float) -> list[str] | None:
    """Use the exact public model card when the official OpenAI catalog omits efforts."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", model_id):
        return None
    try:
        reply = _default_transport("GET", f"https://developers.openai.com/api/docs/models/{model_id}.md",
                                   {"Accept": "text/markdown"}, b"", min(timeout, 5))
    except (OSError, ValueError):
        return None
    if reply.status_code != 200 or f"Model ID: `{model_id}`" not in reply.body:
        return None
    match = re.search(r"reasoning[. _]effort supports?:\s*([^.!?\n]{1,256})", reply.body, re.I)
    if not match:
        return None
    listing = re.sub(r"\s*\(default\)", "", match[1], flags=re.I).replace("`", "")
    values = re.split(r",\s*(?:and\s+)?|\s+and\s+", listing.strip())
    if all(re.fullmatch(r"[a-z0-9_-]{1,32}", value) for value in values):
        return list(dict.fromkeys(values))
    return None


def discover_models(account_id: str, request: ModelDiscoveryRequest, accounts: AccountModelSettings) -> ModelCatalog:
    try:
        base, _ = personal_model_endpoint(request.base_url)
    except ProviderFailure:
        raise ModelCredentialError(422, "validation_error", "A public HTTPS model endpoint is required.") from None
    messages = request.protocol == "anthropic_messages"
    if messages:
        allowed = {url.strip().rstrip("/") for url in os.getenv("FORESIGHTNINE_MESSAGES_PERSONAL_BASE_URL", "").split(",") if url.strip()}
        if (allowed and base not in allowed) or not request.auth_scheme or not request.version:
            raise ModelCredentialError(422, "validation_error", "Check Messages endpoint permissions, version and authentication.")
    key = request.api_key or accounts.saved_discovery_key(account_id, request.protocol, base)
    if not re.fullmatch(r"[!-~]+", key):
        raise ModelCredentialError(422, "validation_error", "Invalid API key.")
    headers = {"Accept": "application/json"}
    if messages:
        headers["anthropic-version"] = request.version
    if messages and request.auth_scheme == "x-api-key":
        headers["x-api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    url = (messages_endpoint(base).removesuffix("/messages") if messages
           else chat_endpoint(base).removesuffix("/chat/completions")) + "/models"
    if request.model:
        if any(part in {".", ".."} for part in request.model.split("/")):
            raise ModelCredentialError(422, "validation_error", "Invalid model identifier.")
        url += "/" + quote(request.model, safe="")
    models = {}
    cursor = None
    seen = set()
    deadline = monotonic() + 12
    try:
        while True:
            target = url + ("?" + urlencode({"after_id": cursor}) if cursor else "")
            reply = _default_transport("GET", target, headers, b"", deadline - monotonic(), max_response_bytes=4_000_000)
            if not 200 <= reply.status_code < 300:
                return ModelCatalog(models=[], upstream_status=reply.status_code)
            payload = json.loads(reply.body)
            rows = [payload] if request.model else payload.get("data")
            if not isinstance(rows, list):
                raise ValueError()
            for row in rows:
                model = _metadata(row, key)
                if model and (not request.model or request.model == model.id):
                    if (request.model and model.efforts is None and url.startswith("https://api.openai.com/v1/models/")
                            and row.get("owned_by") in ("openai", "system")):
                        efforts = _official_efforts(model.id, deadline - monotonic())
                        if efforts:
                            model = model.model_copy(update={"efforts": efforts, "effort_source": "official_docs"})
                    models[model.id] = model
            if request.model or payload.get("has_more") is not True:
                return ModelCatalog(models=list(models.values()))
            cursor = payload.get("last_id")
            if not isinstance(cursor, str) or not 0 < len(cursor) <= 128 or cursor in seen or len(seen) >= 100:
                raise ValueError()
            seen.add(cursor)
    except (OSError, ValueError, AttributeError, RecursionError):
        raise ModelCredentialError(502, "provider_unavailable", "Unable to read the model catalog.") from None
