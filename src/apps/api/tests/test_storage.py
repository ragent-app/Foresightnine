from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.storage import (
    DOCUMENT_TABLE,
    InMemoryDocumentRepository,
    PostgresDocumentRepository,
    StorageConfig,
    StorageConfigurationError,
    StorageConflictError,
    StorageMode,
    StorageRuntimeError,
    build_repository,
)


def test_development_defaults_to_deterministic_memory_repository() -> None:
    config = StorageConfig.from_env({})
    repository = build_repository(config, "sessions")

    assert config.mode is StorageMode.MEMORY
    assert isinstance(repository, InMemoryDocumentRepository)
    repository["session-1"] = {"status": "DRAFT"}
    assert repository.get("session-1") == {"status": "DRAFT"}


@pytest.mark.parametrize(
    "environment",
    [
        {"FORESIGHTNINE_ENV": "production"},
        {"FORESIGHTNINE_ENV": "production", "FORESIGHTNINE_STORAGE_MODE": "memory"},
        {"FORESIGHTNINE_PUBLIC_HARDENING": "true"},
        {"FORESIGHTNINE_STORAGE_MODE": "postgres"},
        {"FORESIGHTNINE_STORAGE_MODE": "postgres", "FORESIGHTNINE_POSTGRES_DSN": "   "},
    ],
)
def test_hardened_configuration_never_falls_back_to_memory(environment: dict[str, str]) -> None:
    with pytest.raises(StorageConfigurationError) as error:
        StorageConfig.from_env(environment)

    assert "postgresql://" not in str(error.value)
    assert "password" not in str(error.value).lower()


def test_production_memory_config_is_rejected_by_factory() -> None:
    config = StorageConfig(mode=StorageMode.MEMORY, production=True)

    with pytest.raises(StorageConfigurationError):
        build_repository(config, "sessions")


@dataclass(frozen=True)
class Document:
    name: str


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self.connection.last_statement = statement
        self.connection.calls.append((statement, parameters))

    def fetchone(self) -> Any:
        result = self.connection.one_results.pop(0)
        if result is not None and self.connection.last_statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            self.connection.accepted_mutations += 1
        return result

    def fetchall(self) -> list[Any]:
        return self.connection.all_results.pop(0)


class FakeConnection:
    def __init__(self, *one_results: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.one_results = list(one_results)
        self.all_results: list[list[Any]] = []
        self.last_statement = ""
        self.accepted_mutations = 0

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def test_postgres_repository_uses_parameterized_jsonb_and_typed_loading() -> None:
    connection = FakeConnection((2,), ({"name": "Ada"}, 2))
    repository = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "sessions",
        value_loader=lambda value: Document(**value),
        value_dumper=lambda value: {"name": value.name},
        connection_factory=lambda _dsn: connection,
    )

    assert repository.put("session-1", Document("Ada"), expected_version=1) == 2
    assert repository["session-1"] == Document("Ada")

    put_statement, put_parameters = connection.calls[0]
    assert DOCUMENT_TABLE in put_statement
    assert put_statement.lstrip().upper().startswith("UPDATE")
    assert "%s::jsonb" in put_statement
    assert "sessions" not in put_statement
    assert "session-1" not in put_statement
    assert put_parameters[0] == {"name": "Ada"}
    assert put_parameters[2:4] == ("sessions", "session-1")
    assert put_parameters[-1] == 1


def test_postgres_repository_surfaces_optimistic_version_conflict() -> None:
    connection = FakeConnection(None)
    repository = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "sessions",
        connection_factory=lambda _dsn: connection,
    )

    with pytest.raises(StorageConflictError):
        repository.put("session-1", {"status": "DRAFT"}, expected_version=7)


def test_postgres_repository_expected_version_cannot_insert_missing_key() -> None:
    connection = FakeConnection(None)
    repository = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "sessions",
        connection_factory=lambda _dsn: connection,
    )

    with pytest.raises(StorageConflictError):
        repository.put("missing-session", {"status": "DRAFT"}, expected_version=7)

    statement, parameters = connection.calls[0]
    assert statement.lstrip().upper().startswith("UPDATE")
    assert "INSERT" not in statement.upper()
    assert parameters[2:4] == ("sessions", "missing-session")
    assert parameters[-1] == 7
    assert connection.accepted_mutations == 0


def test_postgres_connection_failures_are_not_swallowed_or_replaced_with_memory() -> None:
    def failing_connection(_dsn: str) -> Any:
        raise OSError("connection refused")

    repository = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "sessions",
        connection_factory=failing_connection,
    )

    with pytest.raises(StorageRuntimeError) as error:
        repository.get("missing")

    assert "connection refused" not in str(error.value)
