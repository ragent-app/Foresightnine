"""Typed document storage primitives for the API's next persistence boundary.

This module deliberately does not select or install a database driver at import
time.  Development and tests can use :class:`InMemoryDocumentRepository`, while
the PostgreSQL implementation fails closed when its driver or database is not
available.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, ItemsView, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
import os
import re
from typing import Any, Generic, Protocol, TypeVar


ValueT = TypeVar("ValueT")
ValueLoader = Callable[[Any], ValueT]
ValueDumper = Callable[[ValueT], Any]

DOCUMENT_TABLE = "research_state_documents"
_COLLECTION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class StorageMode(StrEnum):
    """The only supported runtime storage modes."""

    MEMORY = "memory"
    POSTGRES = "postgres"


class StorageConfigurationError(RuntimeError):
    """Raised when the selected storage mode is unsafe or incomplete."""


class StorageRuntimeError(RuntimeError):
    """Raised when selected storage cannot serve a repository operation."""


class StorageConflictError(StorageRuntimeError):
    """Raised when an optimistic-concurrency version no longer matches."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Validated storage selection derived from server-side configuration."""

    mode: StorageMode
    dsn: str | None = field(default=None, repr=False)
    production: bool = False
    public_hardening: bool = False

    @property
    def requires_postgres(self) -> bool:
        return self.production or self.public_hardening

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "StorageConfig":
        """Parse storage settings without exposing secrets in error messages.

        ``FORESIGHTNINE_STORAGE_MODE`` is intentionally explicit for hardened
        deployments.  The mode defaults to memory only outside production and
        public-hardening runtimes.
        """
        env = environ if environ is not None else os.environ
        production = env.get("FORESIGHTNINE_ENV", "").strip().lower() == "production"
        public_hardening = _truthy(env.get("FORESIGHTNINE_PUBLIC_HARDENING"))
        raw_mode = env.get("FORESIGHTNINE_STORAGE_MODE", "").strip().lower()

        if not raw_mode:
            if production or public_hardening:
                raise StorageConfigurationError(
                    "A PostgreSQL storage mode and server-side DSN are required in the selected runtime."
                )
            mode = StorageMode.MEMORY
        else:
            try:
                mode = StorageMode(raw_mode)
            except ValueError as error:
                raise StorageConfigurationError("Unknown storage mode.") from error

        dsn = env.get("FORESIGHTNINE_POSTGRES_DSN")
        if mode is StorageMode.MEMORY:
            if production or public_hardening:
                raise StorageConfigurationError("The memory storage mode is not permitted in the selected runtime.")
            return cls(mode=mode, production=production, public_hardening=public_hardening)

        if not dsn or not dsn.strip():
            raise StorageConfigurationError("PostgreSQL storage requires a nonblank server-side DSN.")
        return cls(
            mode=mode,
            dsn=dsn.strip(),
            production=production,
            public_hardening=public_hardening,
        )


class JsonDocumentRepository(Protocol, Generic[ValueT]):
    """Small typed mapping boundary used by the future entity-map wiring."""

    def __getitem__(self, key: str) -> ValueT: ...

    def __setitem__(self, key: str, value: ValueT) -> None: ...

    def __delitem__(self, key: str) -> None: ...

    def __iter__(self) -> Iterator[str]: ...

    def __len__(self) -> int: ...

    def get(self, key: str, default: ValueT | None = None) -> ValueT | None: ...

    def items(self) -> ItemsView[str, ValueT] | Iterator[tuple[str, ValueT]]: ...

    def items_for_retention(self) -> Iterator[tuple[str, ValueT]]:
        """Enumerate non-deleted documents, including expired documents, for cleanup only."""
        ...

    def delete(self, key: str) -> bool: ...

    def clear(self) -> None: ...

    def put(self, key: str, value: ValueT, *, expected_version: int | None = None) -> int: ...


def pydantic_json_dumper(value: Any) -> Any:
    """Serialize Pydantic-like values using their JSON-compatible model dump."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    legacy_dump = getattr(value, "dict", None)
    if callable(legacy_dump):
        return legacy_dump()
    return value


def _json_document(value: Any) -> Any:
    """Normalize a dumped value and reject values JSON cannot represent."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise StorageRuntimeError("Storage document is not JSON serializable.") from error


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError("Repository keys must be nonblank strings.")


class InMemoryDocumentRepository(MutableMapping[str, ValueT], Generic[ValueT]):
    """Deterministic, service-free repository for development and tests."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._values: dict[str, ValueT] = {}
        self._versions: dict[str, int] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _current_time(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current

    def _is_visible(self, value: ValueT, *, now: datetime) -> bool:
        expires_at = getattr(value, "expires_at", None)
        if expires_at is None:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at > now

    def __getitem__(self, key: str) -> ValueT:
        try:
            value = self._values[key]
        except KeyError:
            raise KeyError(key) from None
        if not self._is_visible(value, now=self._current_time()):
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: ValueT) -> None:
        self.put(key, value)

    def __delitem__(self, key: str) -> None:
        if not self.delete(key):
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        now = self._current_time()
        return (key for key, value in self._values.items() if self._is_visible(value, now=now))

    def __len__(self) -> int:
        now = self._current_time()
        return sum(1 for value in self._values.values() if self._is_visible(value, now=now))

    def put(self, key: str, value: ValueT, *, expected_version: int | None = None) -> int:
        _validate_key(key)
        current = self._versions.get(key)
        if expected_version is not None and current != expected_version:
            raise StorageConflictError("Storage document version conflict.")
        version = (current or 0) + 1
        self._values[key] = value
        self._versions[key] = version
        return version

    def version(self, key: str) -> int | None:
        return self._versions.get(key)

    def delete(self, key: str) -> bool:
        existed = key in self._values
        self._values.pop(key, None)
        self._versions.pop(key, None)
        return existed

    def clear(self) -> None:
        self._values.clear()
        self._versions.clear()

    def items_for_retention(self) -> ItemsView[str, ValueT]:
        """Enumerate all stored documents for retention, including expired values."""
        return self._values.items()


_GET_SQL = f"""
SELECT document, version
FROM {DOCUMENT_TABLE}
WHERE collection = %s AND document_key = %s
  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
""".strip()

_ITEMS_SQL = f"""
SELECT document_key, document, version
FROM {DOCUMENT_TABLE}
WHERE collection = %s
  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
ORDER BY document_key
""".strip()

_RETENTION_ITEMS_SQL = f"""
SELECT document_key, document, version
FROM {DOCUMENT_TABLE}
WHERE collection = %s
ORDER BY document_key
""".strip()

_PUT_SQL = f"""
INSERT INTO {DOCUMENT_TABLE}
    (collection, document_key, document, expires_at, created_at, updated_at, version)
VALUES (%s, %s, %s::jsonb, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)
ON CONFLICT (collection, document_key) DO UPDATE
SET document = EXCLUDED.document,
    expires_at = EXCLUDED.expires_at,
    updated_at = CURRENT_TIMESTAMP,
    version = {DOCUMENT_TABLE}.version + 1
RETURNING version
""".strip()

_UPDATE_SQL = f"""
UPDATE {DOCUMENT_TABLE}
SET document = %s::jsonb,
    expires_at = %s,
    updated_at = CURRENT_TIMESTAMP,
    version = version + 1
WHERE collection = %s AND document_key = %s
  AND version = CAST(%s AS bigint)
RETURNING version
""".strip()

_DELETE_SQL = f"DELETE FROM {DOCUMENT_TABLE} WHERE collection = %s AND document_key = %s RETURNING document_key"
_CLEAR_SQL = f"DELETE FROM {DOCUMENT_TABLE} WHERE collection = %s"
_PING_SQL = "SELECT 1"


def _default_connection_factory(dsn: str) -> Any:
    try:
        import psycopg
    except ImportError as error:
        raise StorageRuntimeError("The PostgreSQL driver is unavailable.") from error
    try:
        return psycopg.connect(dsn)
    except Exception as error:
        raise StorageRuntimeError("PostgreSQL storage is unavailable.") from error


class PostgresDocumentRepository(MutableMapping[str, ValueT], Generic[ValueT]):
    """Parameterized JSONB document repository with lazy PostgreSQL access."""

    def __init__(
        self,
        dsn: str,
        collection: str,
        *,
        value_loader: ValueLoader[ValueT] | None = None,
        value_dumper: ValueDumper[ValueT] | None = None,
        connection_factory: Callable[[str], Any] | None = None,
    ) -> None:
        if not dsn or not dsn.strip():
            raise StorageConfigurationError("PostgreSQL storage requires a nonblank server-side DSN.")
        if not _COLLECTION_PATTERN.fullmatch(collection):
            raise StorageConfigurationError("Storage collection name is invalid.")
        self._dsn = dsn.strip()
        self.collection = collection
        self._value_loader = value_loader or (lambda value: value)
        self._value_dumper = value_dumper or pydantic_json_dumper
        self._connection_factory = connection_factory or _default_connection_factory
        self._uses_injected_connection = connection_factory is not None

    def _connection(self) -> Any:
        try:
            return self._connection_factory(self._dsn)
        except StorageRuntimeError:
            raise
        except Exception as error:
            raise StorageRuntimeError("PostgreSQL storage is unavailable.") from error

    def _jsonb_parameter(self, document: Any) -> Any:
        if self._uses_injected_connection:
            return document
        try:
            from psycopg.types.json import Jsonb
        except ImportError as error:
            raise StorageRuntimeError("The PostgreSQL driver is unavailable.") from error
        return Jsonb(document)

    @staticmethod
    def _document_from_row(row: Any) -> Any:
        value = row[0] if not isinstance(row, Mapping) else row["document"]
        if isinstance(value, str):
            return json.loads(value)
        return value

    def __getitem__(self, key: str) -> ValueT:
        _validate_key(key)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_GET_SQL, (self.collection, key))
                    row = cursor.fetchone()
        except KeyError:
            raise
        except StorageRuntimeError:
            raise
        except Exception as error:
            raise StorageRuntimeError("PostgreSQL storage operation failed.") from error
        if row is None:
            raise KeyError(key)
        try:
            return self._value_loader(self._document_from_row(row))
        except Exception as error:
            raise StorageRuntimeError("Stored document could not be loaded.") from error

    def __setitem__(self, key: str, value: ValueT) -> None:
        self.put(key, value)

    def __delitem__(self, key: str) -> None:
        if not self.delete(key):
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        for key, _value, _version in self._items_with_versions(_ITEMS_SQL):
            yield key

    def __len__(self) -> int:
        return sum(1 for _ in self._items_with_versions(_ITEMS_SQL))

    def _items_with_versions(self, statement: str) -> list[tuple[str, ValueT, int]]:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement, (self.collection,))
                    rows = cursor.fetchall()
        except StorageRuntimeError:
            raise
        except Exception as error:
            raise StorageRuntimeError("PostgreSQL storage operation failed.") from error
        result: list[tuple[str, ValueT, int]] = []
        try:
            for row in rows:
                if isinstance(row, Mapping):
                    key, version = row["document_key"], row["version"]
                else:
                    key, version = row[0], row[2]
                result.append((key, self._value_loader(self._document_from_row(row[1:] if not isinstance(row, Mapping) else row)), int(version)))
        except Exception as error:
            raise StorageRuntimeError("Stored document could not be loaded.") from error
        return result

    def items(self) -> Iterator[tuple[str, ValueT]]:
        return ((key, value) for key, value, _version in self._items_with_versions(_ITEMS_SQL))

    def items_for_retention(self) -> Iterator[tuple[str, ValueT]]:
        """Enumerate non-deleted documents, including expired documents, for cleanup only."""
        return ((key, value) for key, value, _version in self._items_with_versions(_RETENTION_ITEMS_SQL))

    def put(self, key: str, value: ValueT, *, expected_version: int | None = None) -> int:
        _validate_key(key)
        document = _json_document(self._value_dumper(value))
        expires_at = getattr(value, "expires_at", None)
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expected_version is None:
            statement = _PUT_SQL
            parameters = (self.collection, key, self._jsonb_parameter(document), expires_at)
        else:
            # An expected version is an update precondition.  Use UPDATE rather
            # than an upsert so a missing key cannot be created accidentally.
            statement = _UPDATE_SQL
            parameters = (self._jsonb_parameter(document), expires_at, self.collection, key, expected_version)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(statement, parameters)
                    row = cursor.fetchone()
        except StorageConflictError:
            raise
        except StorageRuntimeError:
            raise
        except Exception as error:
            raise StorageRuntimeError("PostgreSQL storage operation failed.") from error
        if row is None:
            raise StorageConflictError("Storage document version conflict.")
        return int(row[0] if not isinstance(row, Mapping) else row["version"])

    def version(self, key: str) -> int | None:
        _validate_key(key)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_GET_SQL, (self.collection, key))
                    row = cursor.fetchone()
        except StorageRuntimeError:
            raise
        except Exception as error:
            raise StorageRuntimeError("PostgreSQL storage operation failed.") from error
        if row is None:
            return None
        return int(row[1] if not isinstance(row, Mapping) else row["version"])

    def delete(self, key: str) -> bool:
        _validate_key(key)
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_DELETE_SQL, (self.collection, key))
                    return cursor.fetchone() is not None
        except StorageRuntimeError:
            raise
        except Exception as error:
            raise StorageRuntimeError("PostgreSQL storage operation failed.") from error

    def clear(self) -> None:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_CLEAR_SQL, (self.collection,))
        except StorageRuntimeError:
            raise
        except Exception as error:
            raise StorageRuntimeError("PostgreSQL storage operation failed.") from error

    def check_ready(self) -> None:
        """Perform an explicit connectivity check for a future app lifecycle hook."""
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_PING_SQL)
                    cursor.fetchone()
        except StorageRuntimeError:
            raise
        except Exception as error:
            raise StorageRuntimeError("PostgreSQL storage is unavailable.") from error


def build_repository(
    config_or_mode: StorageConfig | StorageMode | str,
    collection: str,
    *,
    value_loader: ValueLoader[ValueT] | None = None,
    value_dumper: ValueDumper[ValueT] | None = None,
) -> JsonDocumentRepository[ValueT]:
    """Build one collection repository without a production memory fallback."""
    if isinstance(config_or_mode, StorageConfig):
        config = config_or_mode
    else:
        try:
            mode = config_or_mode if isinstance(config_or_mode, StorageMode) else StorageMode(config_or_mode)
        except ValueError as error:
            raise StorageConfigurationError("Unknown storage mode.") from error
        config = StorageConfig(mode=mode)

    if config.mode is StorageMode.MEMORY:
        if config.requires_postgres:
            raise StorageConfigurationError("The memory storage mode is not permitted in the selected runtime.")
        return InMemoryDocumentRepository()

    if not config.dsn or not config.dsn.strip():
        raise StorageConfigurationError("PostgreSQL storage requires a nonblank server-side DSN.")
    return PostgresDocumentRepository(
        config.dsn,
        collection,
        value_loader=value_loader,
        value_dumper=value_dumper,
    )


__all__ = [
    "DOCUMENT_TABLE",
    "InMemoryDocumentRepository",
    "JsonDocumentRepository",
    "PostgresDocumentRepository",
    "StorageConfig",
    "StorageConfigurationError",
    "StorageConflictError",
    "StorageMode",
    "StorageRuntimeError",
    "build_repository",
    "pydantic_json_dumper",
]
