"""Protocol-neutral OpenAI-compatible embedding client, bounded LRU cache, and VectorScorer.

All endpoint, model, and credential settings come from explicit configuration or server
environment variables (never hardcoded vendor defaults).  Network access uses the repository's
pinned public-DNS transport and fails open with content-free diagnostics so lexical BM25
retrieval continues working even when an embedding endpoint is unavailable or times out.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
import operator
import os
import re
from threading import Lock, RLock, local as thread_local
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from .openai_compatible import (
    _LOCAL_GATEWAY_HOSTS,
    OpenAICompatibleTransport,
    _TransportFailure,
    _TransportTimeout,
    _default_transport,
    _parse_allowlist,
    _parse_bool,
)
from .phase3 import ClientSearchEvidenceProvider, ProviderFailure
from .research_diagnostics import safe_exception_diagnostic
from .research_library import Passage, VectorCandidate
from .research_source_policy import is_fred_source

if TYPE_CHECKING:  # pragma: no cover
    from .research_sources import ResearchSource

EMBEDDING_BASE_URL_ENV = "FORESIGHTNINE_EMBEDDING_BASE_URL"
EMBEDDING_MODEL_ENV = "FORESIGHTNINE_EMBEDDING_MODEL"
EMBEDDING_API_KEY_ENV = "FORESIGHTNINE_EMBEDDING_API_KEY"
EMBEDDING_TIMEOUT_ENV = "FORESIGHTNINE_EMBEDDING_TIMEOUT_SECONDS"
EMBEDDING_DIMENSIONS_ENV = "FORESIGHTNINE_EMBEDDING_DIMENSIONS"
EMBEDDING_ALLOWED_HOSTS_ENV = "FORESIGHTNINE_EMBEDDING_ALLOWED_HOSTS"
EMBEDDING_ALLOW_PRIVATE_NETWORK_ENV = "FORESIGHTNINE_EMBEDDING_ALLOW_PRIVATE_NETWORK"
EMBEDDING_BATCH_SIZE_ENV = "FORESIGHTNINE_EMBEDDING_BATCH_SIZE"
EMBEDDING_CACHE_SIZE_ENV = "FORESIGHTNINE_EMBEDDING_CACHE_SIZE"

DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 15.0
DEFAULT_EMBEDDING_BATCH_SIZE = 32
MAX_EMBEDDING_BATCH_SIZE = 256
DEFAULT_EMBEDDING_CACHE_SIZE = 4096
MAX_EMBEDDING_CACHE_SIZE = 100_000
MAX_EMBEDDING_RESPONSE_BYTES = 5_000_000

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NON_PUBLIC_SUFFIXES = (".localhost", ".local", ".internal", ".invalid", ".arpa")
_FORBIDDEN_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata.azure.internal"})
_NON_CANONICAL_IP_LABEL = re.compile(r"(?:0x[0-9a-f]+|0[0-7]+|\d+)")
_logger = logging.getLogger(__name__)

CacheKey = tuple[str, str, int | None, str]


class EmbeddingConfigError(ProviderFailure, ValueError):
    """Raised when embedding client configuration violates protocol or SSRF rules."""

    def __init__(self, message: str) -> None:
        super().__init__("provider_configuration_invalid", message, retryable=False)


class EmbeddingClientError(RuntimeError):
    """Content-free transport/protocol error raised internally before fail-open handling."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _normalize_public_hostname(hostname: str, *, allow_local_gateway: bool = False) -> str:
    try:
        normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise EmbeddingConfigError("Embedding base URL hostname is invalid.") from error
    if not normalized or len(normalized) > 253:
        raise EmbeddingConfigError("Embedding base URL hostname is invalid.")
    if allow_local_gateway and normalized in _LOCAL_GATEWAY_HOSTS:
        return normalized
    if (
        normalized == "localhost"
        or normalized in _FORBIDDEN_METADATA_HOSTS
        or normalized.endswith(_NON_PUBLIC_SUFFIXES)
    ):
        raise EmbeddingConfigError("Non-public embedding hostnames are not permitted.")
    if ClientSearchEvidenceProvider._literal_address(normalized) is not None:
        raise EmbeddingConfigError("Embedding base URL must use a public DNS hostname.")
    labels = normalized.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise EmbeddingConfigError("Embedding base URL must be a valid public DNS hostname.")
    if all(_NON_CANONICAL_IP_LABEL.fullmatch(label) for label in labels) and any(
        label.startswith("0x") or (len(label) > 1 and label.startswith("0")) for label in labels
    ):
        raise EmbeddingConfigError("Non-canonical IPv4 hostnames are not permitted.")
    return normalized


def validate_embedding_base_url(
    base_url: str | None,
    *,
    allowed_hosts: str | None = None,
    allow_private_network: bool = False,
) -> tuple[str, frozenset[tuple[str, int]] | None]:
    """Validate protocol-neutral embedding base URL against SSRF and FRED restrictions."""
    raw = (base_url or "").strip()
    if not raw or len(raw) > 2048 or "\\" in raw or any(ord(ch) < 0x20 for ch in raw):
        raise EmbeddingConfigError("Embedding base URL is invalid.")
    parsed = urlsplit(raw)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise EmbeddingConfigError("Embedding base URL port is invalid.") from error
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise EmbeddingConfigError("Embedding base URL contains unsupported components.")
    local_gateway = allow_private_network and hostname.rstrip(".").lower() in _LOCAL_GATEWAY_HOSTS
    scheme = parsed.scheme.lower()
    if scheme not in ({"https", "http"} if local_gateway else {"https"}):
        raise EmbeddingConfigError("Embedding base URL must use HTTPS unless an explicit local gateway is enabled.")
    normalized_host = _normalize_public_hostname(hostname, allow_local_gateway=local_gateway)
    normalized_netloc = f"{normalized_host}:{port}" if port is not None else normalized_host
    normalized_path = parsed.path.rstrip("/")
    normalized_url = urlunsplit((scheme, normalized_netloc, normalized_path, "", ""))
    if is_fred_source(raw) or is_fred_source(normalized_url):
        raise EmbeddingConfigError("FRED endpoints are not permitted.")

    parsed_allowlist: frozenset[tuple[str, int]] | None = None
    if allowed_hosts and allowed_hosts.strip():
        try:
            parsed_allowlist = _parse_allowlist(allowed_hosts)
        except ProviderFailure as error:
            raise EmbeddingConfigError("Embedding host allowlist is invalid.") from error
        effective_port = port or 443
        if (normalized_host, effective_port) not in parsed_allowlist:
            raise EmbeddingConfigError("Embedding base URL host is not in the configured allowlist.")
    return normalized_url, parsed_allowlist


def embedding_endpoint(base_url: str) -> str:
    """Resolve the OpenAI-compatible ``/embeddings`` URL from a validated base URL."""
    base = base_url.rstrip("/")
    path = urlsplit(base).path.rstrip("/")
    if path.endswith("/embeddings"):
        return base
    if not path:
        return f"{base}/v1/embeddings"
    return f"{base}/embeddings"


def _parse_optional_float(raw: str | None, *, default: float, upper: float, label: str) -> float:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError as error:
        raise EmbeddingConfigError(f"Embedding {label} is invalid.") from error
    if not math.isfinite(value) or not 0 < value <= upper:
        raise EmbeddingConfigError(f"Embedding {label} is out of bounds.")
    return value


def _parse_optional_int(
    raw: str | int | None,
    *,
    default: int | None,
    minimum: int,
    maximum: int,
    label: str,
) -> int | None:
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise EmbeddingConfigError(f"Embedding {label} is invalid.")
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip()
        if not text:
            return default
        try:
            value = int(text)
        except ValueError as error:
            raise EmbeddingConfigError(f"Embedding {label} is invalid.") from error
    if not minimum <= value <= maximum:
        raise EmbeddingConfigError(f"Embedding {label} is out of bounds.")
    return value


@dataclass(frozen=True)
class EmbeddingSettings:
    """Validated settings for a protocol-neutral OpenAI-compatible embeddings endpoint."""

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS
    dimensions: int | None = None
    allowed_hosts: frozenset[tuple[str, int]] | None = None
    allow_private_network: bool = False
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    cache_size: int = DEFAULT_EMBEDDING_CACHE_SIZE

    @classmethod
    def from_values(
        cls,
        *,
        base_url: str | None,
        model: str | None,
        api_key: str | None = None,
        timeout_seconds: float | str | None = None,
        dimensions: int | str | None = None,
        allowed_hosts: str | None = None,
        allow_private_network: bool = False,
        batch_size: int | str | None = None,
        cache_size: int | str | None = None,
    ) -> "EmbeddingSettings":
        clean_model = (model or "").strip()
        if not clean_model or len(clean_model) > 128 or any(ord(ch) < 0x20 for ch in clean_model):
            raise EmbeddingConfigError("Embedding model configuration is invalid.")
        clean_key = (api_key or "").strip() or None
        normalized_url, parsed_allowlist = validate_embedding_base_url(
            base_url,
            allowed_hosts=allowed_hosts,
            allow_private_network=allow_private_network,
        )
        timeout = (
            _parse_optional_float(
                str(timeout_seconds) if timeout_seconds is not None else None,
                default=DEFAULT_EMBEDDING_TIMEOUT_SECONDS,
                upper=600.0,
                label="timeout",
            )
            if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
            else float(timeout_seconds)
        )
        if not math.isfinite(timeout) or not 0 < timeout <= 600.0:
            raise EmbeddingConfigError("Embedding timeout is out of bounds.")
        parsed_dims = _parse_optional_int(dimensions, default=None, minimum=1, maximum=65536, label="dimensions")
        parsed_batch = _parse_optional_int(
            batch_size, default=DEFAULT_EMBEDDING_BATCH_SIZE, minimum=1, maximum=MAX_EMBEDDING_BATCH_SIZE, label="batch_size"
        )
        parsed_cache = _parse_optional_int(
            cache_size, default=DEFAULT_EMBEDDING_CACHE_SIZE, minimum=0, maximum=MAX_EMBEDDING_CACHE_SIZE, label="cache_size"
        )
        return cls(
            base_url=normalized_url,
            model=clean_model,
            api_key=clean_key,
            timeout_seconds=timeout,
            dimensions=parsed_dims,
            allowed_hosts=parsed_allowlist,
            allow_private_network=allow_private_network,
            batch_size=int(parsed_batch or DEFAULT_EMBEDDING_BATCH_SIZE),
            cache_size=DEFAULT_EMBEDDING_CACHE_SIZE if parsed_cache is None else parsed_cache,
        )

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "EmbeddingSettings | None":
        """Load embedding settings from ``environ``; returns ``None`` when unconfigured."""
        env = environ if environ is not None else os.environ
        base_url = (env.get(EMBEDDING_BASE_URL_ENV) or "").strip()
        model = (env.get(EMBEDDING_MODEL_ENV) or "").strip()
        api_key = (env.get(EMBEDDING_API_KEY_ENV) or "").strip()
        allow_private = _parse_bool(env.get(EMBEDDING_ALLOW_PRIVATE_NETWORK_ENV))
        if not base_url or not model or (not api_key and not allow_private):
            return None
        return cls.from_values(
            base_url=base_url,
            model=model,
            api_key=api_key or None,
            timeout_seconds=env.get(EMBEDDING_TIMEOUT_ENV),
            dimensions=env.get(EMBEDDING_DIMENSIONS_ENV),
            allowed_hosts=env.get(EMBEDDING_ALLOWED_HOSTS_ENV),
            allow_private_network=allow_private,
            batch_size=env.get(EMBEDDING_BATCH_SIZE_ENV),
            cache_size=env.get(EMBEDDING_CACHE_SIZE_ENV),
        )


class EmbeddingCache:
    """Thread-safe bounded LRU cache keyed by ``(base_url, model, dimensions, sha256(text))``."""

    def __init__(self, max_entries: int = DEFAULT_EMBEDDING_CACHE_SIZE) -> None:
        if type(max_entries) is not int or max_entries < 0:
            raise ValueError("max_entries must be a non-negative integer.")
        self.max_entries = max_entries
        self._entries: OrderedDict[CacheKey, tuple[float, ...]] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @staticmethod
    def make_key(base_url: str, model: str, dimensions: int | None, text: str) -> CacheKey:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return (base_url.strip().rstrip("/"), model, dimensions, digest)

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        with self._lock:
            return self._misses

    @property
    def evictions(self) -> int:
        with self._lock:
            return self._evictions

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._entries),
                "max_entries": self.max_entries,
                "evictions": self._evictions,
            }

    def get(self, key: CacheKey) -> tuple[float, ...] | None:
        with self._lock:
            if self.max_entries == 0:
                self._misses += 1
                return None
            vec = self._entries.get(key)
            if vec is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return vec

    def put(self, key: CacheKey, vector: Sequence[float]) -> None:
        if self.max_entries == 0:
            return
        vec = tuple(float(x) for x in vector)
        with self._lock:
            if key in self._entries:
                self._entries[key] = vec
                self._entries.move_to_end(key)
                return
            while len(self._entries) >= self.max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1
            self._entries[key] = vec

    def clear(self, *, reset_stats: bool = True) -> None:
        with self._lock:
            self._entries.clear()
            if reset_stats:
                self._hits = 0
                self._misses = 0
                self._evictions = 0


_DEFAULT_EMBEDDING_CACHE = EmbeddingCache(DEFAULT_EMBEDDING_CACHE_SIZE)


def default_embedding_cache() -> EmbeddingCache:
    """Return the process-wide bounded LRU cache shared across turns and runs."""
    return _DEFAULT_EMBEDDING_CACHE


def _parse_embedding_response(raw_body: str, expected_count: int) -> list[tuple[float, ...]]:
    try:
        payload = json.loads(raw_body)
    except (ValueError, TypeError) as error:
        raise EmbeddingClientError("embedding_response_invalid") from error
    if not isinstance(payload, dict):
        raise EmbeddingClientError("embedding_response_invalid")
    items = payload.get("data")
    if not isinstance(items, list) or len(items) != expected_count:
        raise EmbeddingClientError("embedding_response_invalid")

    ordered: list[tuple[float, ...] | None] = [None] * expected_count
    has_indices = all(isinstance(item, dict) and type(item.get("index")) is int for item in items)
    dim: int | None = None
    for pos, item in enumerate(items):
        if not isinstance(item, dict):
            raise EmbeddingClientError("embedding_response_invalid")
        idx = item["index"] if has_indices else pos
        if not (0 <= idx < expected_count) or ordered[idx] is not None:
            raise EmbeddingClientError("embedding_response_invalid")
        raw_vec = item.get("embedding")
        if not isinstance(raw_vec, list) or not raw_vec:
            raise EmbeddingClientError("embedding_response_invalid")
        parsed_vec: list[float] = []
        for value in raw_vec:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EmbeddingClientError("embedding_response_invalid")
            fval = float(value)
            if not math.isfinite(fval):
                raise EmbeddingClientError("embedding_response_invalid")
            parsed_vec.append(fval)
        if dim is None:
            dim = len(parsed_vec)
        elif len(parsed_vec) != dim:
            raise EmbeddingClientError("embedding_response_invalid")
        ordered[idx] = tuple(parsed_vec)

    if any(vec is None for vec in ordered):
        raise EmbeddingClientError("embedding_response_invalid")
    return [vec for vec in ordered if vec is not None]


class EmbeddingClient:
    """Protocol-neutral OpenAI-compatible ``/embeddings`` client with LRU caching."""

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        cache: EmbeddingCache | None = None,
        transport: OpenAICompatibleTransport | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache if cache is not None else EmbeddingCache(settings.cache_size)
        self._transport = transport

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.settings.base_url!r}, "
            f"model={self.settings.model!r}, configured={bool(self.settings.api_key)!r})"
        )

    def cache_stats(self) -> dict[str, int]:
        return self.cache.stats()

    def embed_text(self, text: str) -> tuple[float, ...]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        if not texts:
            return []
        unique_texts = list(dict.fromkeys(texts))
        resolved: dict[str, tuple[float, ...]] = {}
        uncached: list[tuple[str, CacheKey]] = []
        for item in unique_texts:
            key = self.cache.make_key(self.settings.base_url, self.settings.model, self.settings.dimensions, item)
            cached = self.cache.get(key)
            if cached is not None:
                resolved[item] = cached
            else:
                uncached.append((item, key))

        batch_size = self.settings.batch_size
        for start in range(0, len(uncached), batch_size):
            chunk = uncached[start : start + batch_size]
            vectors = self._fetch_batch([item for item, _ in chunk])
            for (item, key), vec in zip(chunk, vectors):
                self.cache.put(key, vec)
                resolved[item] = vec

        return [resolved[item] for item in texts]

    def _fetch_batch(self, batch: Sequence[str]) -> list[tuple[float, ...]]:
        payload: dict[str, Any] = {"model": self.settings.model, "input": list(batch)}
        if self.settings.dimensions is not None:
            payload["dimensions"] = self.settings.dimensions
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        endpoint = embedding_endpoint(self.settings.base_url)
        try:
            if self._transport is not None:
                response = self._transport("POST", endpoint, headers, body, self.settings.timeout_seconds)
            else:
                response = _default_transport(
                    "POST",
                    endpoint,
                    headers,
                    body,
                    self.settings.timeout_seconds,
                    allow_private_network=self.settings.allow_private_network,
                    max_response_bytes=MAX_EMBEDDING_RESPONSE_BYTES,
                )
        except _TransportTimeout as error:
            raise EmbeddingClientError("embedding_timeout") from error
        except (_TransportFailure, OSError) as error:
            raise EmbeddingClientError("embedding_transport_error") from error

        status = getattr(response, "status_code", 0)
        if not isinstance(status, int) or not 200 <= status < 300:
            raise EmbeddingClientError("embedding_http_error", status_code=status if isinstance(status, int) else None)
        raw_body = getattr(response, "body", "")
        if not isinstance(raw_body, str):
            raise EmbeddingClientError("embedding_response_invalid")
        return _parse_embedding_response(raw_body, len(batch))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute bounded cosine similarity; returns ``0.0`` on empty, mismatched, or zero vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0 or not (math.isfinite(dot) and math.isfinite(norm_a) and math.isfinite(norm_b)):
        return 0.0
    sim = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    return max(-1.0, min(1.0, sim)) if math.isfinite(sim) else 0.0


def _unit_vector(vec: Sequence[float]) -> tuple[float, ...] | None:
    if not vec:
        return None
    norm_sq = sum(x * x for x in vec)
    if norm_sq <= 0.0 or not math.isfinite(norm_sq):
        return None
    inv_norm = 1.0 / math.sqrt(norm_sq)
    unit = tuple(x * inv_norm for x in vec)
    return unit if all(math.isfinite(x) for x in unit) else None


def _log_safe_warning(event: str, code: str, error: BaseException | None = None, *, status_code: int | None = None) -> None:
    diag: dict[str, Any] = {"code": code}
    if type(status_code) is int and 100 <= status_code <= 599:
        diag["status_code"] = status_code
    if error is not None:
        diag.update(safe_exception_diagnostic(error))
    _logger.warning("%s metadata=%s", event, diag)


class EmbeddingVectorScorer:
    """``VectorScorer`` implementation backed by ``EmbeddingClient`` and fail-open cosine search."""

    def __init__(self, client: EmbeddingClient, *, min_similarity: float = 0.0) -> None:
        self.client = client
        self._min_similarity = float(min_similarity)
        self._lock = RLock()
        self._vectors: dict[str, list[tuple[int, tuple[float, ...]]]] = {}
        self._index_failures: dict[str, str] = {}
        self._local = thread_local()

    @property
    def last_query_error(self) -> str | None:
        val = getattr(self._local, "last_query_error", None)
        return val if isinstance(val, str) else None

    def cache_stats(self) -> dict[str, int]:
        return self.client.cache_stats()

    def index_source(self, source: "ResearchSource", passages: Sequence[Passage]) -> None:
        source_id = source.source_id
        if not passages:
            with self._lock:
                self._vectors[source_id] = []
                self._index_failures.pop(source_id, None)
            return
        text = source.text or ""
        indexed_items: list[tuple[int, str]] = []
        for p in passages:
            span = text[p.char_start : p.char_end]
            passage_text = f"{p.context}\n{span}" if p.context else span
            if passage_text.strip():
                indexed_items.append((p.index, passage_text))
        if not indexed_items:
            with self._lock:
                self._vectors[source_id] = []
                self._index_failures.pop(source_id, None)
            return

        try:
            vectors = self.client.embed_texts([item_text for _, item_text in indexed_items])
        except EmbeddingClientError as error:
            _log_safe_warning("embedding_index_failed", error.code, error, status_code=error.status_code)
            with self._lock:
                self._vectors[source_id] = []
                self._index_failures[source_id] = error.code
            return
        except Exception as error:
            _log_safe_warning("embedding_index_failed", "embedding_unexpected_error", error)
            with self._lock:
                self._vectors[source_id] = []
                self._index_failures[source_id] = "embedding_unexpected_error"
            return

        unit_entries: list[tuple[int, tuple[float, ...]]] = []
        for (pid, _), vec in zip(indexed_items, vectors):
            unit = _unit_vector(vec)
            if unit is not None:
                unit_entries.append((pid, unit))
        with self._lock:
            self._vectors[source_id] = unit_entries
            self._index_failures.pop(source_id, None)

    def remove_source(self, source_id: str) -> None:
        with self._lock:
            self._vectors.pop(source_id, None)
            self._index_failures.pop(source_id, None)

    def query(self, text: str, *, source_ids: Sequence[str], limit: int) -> Sequence[VectorCandidate]:
        self._local.last_query_error = None
        if limit <= 0 or not text or not text.strip() or not source_ids:
            return []

        unique_sids = sorted(dict.fromkeys(source_ids))
        with self._lock:
            snapshot: list[tuple[str, list[tuple[int, tuple[float, ...]]]]] = [
                (sid, list(self._vectors.get(sid, ()))) for sid in unique_sids
            ]
            failure_code = next(
                (self._index_failures[sid] for sid in unique_sids if sid in self._index_failures),
                None,
            )

        if not any(entries for _, entries in snapshot):
            if failure_code is not None:
                self._local.last_query_error = failure_code
            return []

        try:
            query_vec = self.client.embed_text(text)
        except EmbeddingClientError as error:
            self._local.last_query_error = error.code
            _log_safe_warning("embedding_query_failed", error.code, error, status_code=error.status_code)
            return []
        except Exception as error:
            self._local.last_query_error = "embedding_unexpected_error"
            _log_safe_warning("embedding_query_failed", "embedding_unexpected_error", error)
            return []

        q_unit = _unit_vector(query_vec)
        if q_unit is None:
            return []
        q_dim = len(q_unit)

        candidates: list[VectorCandidate] = []
        for sid, entries in snapshot:
            for pid, p_unit in entries:
                if len(p_unit) != q_dim:
                    continue
                sim = sum(map(operator.mul, q_unit, p_unit))
                if not math.isfinite(sim):
                    continue
                if sim > 1.0:
                    sim = 1.0
                elif sim < -1.0:
                    sim = -1.0
                if sim > self._min_similarity:
                    candidates.append(VectorCandidate(source_id=sid, passage_index=pid, score=sim))

        candidates.sort(key=lambda c: (-c.score, c.source_id, c.passage_index))
        return candidates[:limit]
