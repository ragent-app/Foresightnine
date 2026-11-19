from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from app.models import SourceRef
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
    postgres_unit_of_work,
    pydantic_json_dumper,
)


def test_development_defaults_to_deterministic_memory_repository() -> None:
    config = StorageConfig.from_env({})
    repository = build_repository(config, "sessions")

    assert config.mode is StorageMode.MEMORY
    assert isinstance(repository, InMemoryDocumentRepository)
    repository["session-1"] = {"status": "DRAFT"}
    assert repository.get("session-1") == {"status": "DRAFT"}


def test_pydantic_json_dumper_omits_optional_nulls_for_strict_round_trip() -> None:
    reference = SourceRef(ref_id="source:session", source_type="model_inference", label="Deterministic session")

    dumped = pydantic_json_dumper(reference)

    assert dumped == {
        "ref_id": "source:session",
        "source_type": "model_inference",
        "label": "Deterministic session",
    }
    assert SourceRef.model_validate(dumped) == reference


def test_postgres_repository_default_dumper_and_legacy_null_read_compatibility() -> None:
    reference = SourceRef(ref_id="source:session", source_type="model_inference", label="Deterministic session")
    connection = FakeConnection(
        (1,),
        ({"ref_id": "source:session", "source_type": "model_inference", "label": "Deterministic session", "url": None}, 1),
    )
    repository = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "sessions",
        value_loader=SourceRef.model_validate,
        connection_factory=lambda _dsn: connection,
    )

    assert repository.put("source:session", reference) == 1
    assert repository["source:session"] == reference

    put_document = connection.calls[0][1][2]
    assert put_document == {
        "ref_id": "source:session",
        "source_type": "model_inference",
        "label": "Deterministic session",
    }


def test_postgres_repository_preserves_decoded_jsonb_scalar_strings() -> None:
    connection = FakeConnection(("challenge:one", 1))
    repository = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "worldline_challenges",
        value_loader=lambda value: value,
        connection_factory=lambda _dsn: connection,
    )

    assert repository["worldline:one"] == "challenge:one"


def test_postgres_repository_reuses_ambient_unit_of_work_for_atomic_lock_and_put() -> None:
    dsn = "postgresql://db.example.test/app"
    connection = FakeConnection((1,))
    factory_calls = 0

    def unexpected_factory(_dsn: str) -> Any:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("ambient unit of work must reuse the operation connection")

    repository = PostgresDocumentRepository(dsn, "sessions", connection_factory=unexpected_factory)
    cursor = connection.cursor()
    with postgres_unit_of_work(dsn, connection, cursor):
        version = repository.with_advisory_lock("billing-account:test", lambda: repository.put("session-1", {"status": "DRAFT"}))

    assert version == 1
    assert factory_calls == 0
    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert connection.calls[1][0].lstrip().upper().startswith("INSERT")


def test_pydantic_json_dumper_retains_present_optional_url() -> None:
    reference = SourceRef(
        ref_id="source:session",
        source_type="public",
        label="Public source",
        url="https://example.com/source",
    )

    assert pydantic_json_dumper(reference)["url"] == "https://example.com/source"


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


def test_in_memory_ordinary_reads_hide_expired_values_but_retention_sees_them() -> None:
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    repository = InMemoryDocumentRepository(clock=lambda: now)
    repository["expired"] = type("DocumentWithExpiry", (), {"expires_at": now})()
    repository["active"] = type("DocumentWithExpiry", (), {"expires_at": now.replace(second=1)})()
    repository["persistent"] = {"name": "persistent"}

    with pytest.raises(KeyError):
        repository["expired"]
    assert list(repository) == ["active", "persistent"]
    assert len(repository) == 2
    ordinary_items = dict(repository.items())
    assert set(ordinary_items) == {"active", "persistent"}
    assert ordinary_items["active"] == repository["active"]
    assert set(dict(repository.items_for_retention())) == {"expired", "active", "persistent"}


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


def test_postgres_retention_enumeration_includes_expired_documents_without_expiry_filter() -> None:
    connection = FakeConnection()
    connection.all_results = [[("expired-session", {"name": "Expired"}, 4)]]
    repository = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "sessions",
        value_loader=lambda value: Document(**value),
        connection_factory=lambda _dsn: connection,
    )

    assert dict(repository.items_for_retention()) == {"expired-session": Document("Expired")}

    statement, parameters = connection.calls[0]
    assert "expires_at" not in statement.lower()
    assert "current_timestamp" not in statement.lower()
    assert parameters == ("sessions",)


def test_postgres_iteration_and_len_use_ordinary_expiry_filtered_query() -> None:
    connection = FakeConnection()
    connection.all_results = [
        [("active-session", {"name": "Active"}, 2)],
        [("active-session", {"name": "Active"}, 2)],
    ]
    repository = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "sessions",
        value_loader=lambda value: Document(**value),
        connection_factory=lambda _dsn: connection,
    )

    assert next(iter(repository)) == "active-session"
    assert len(repository) == 1
    assert len(connection.calls) == 2
    assert all("expires_at" in statement.lower() for statement, _parameters in connection.calls)
    assert all("current_timestamp" in statement.lower() for statement, _parameters in connection.calls)


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


def test_postgres_items_for_owner_pushes_owner_filter_to_sql() -> None:
    connection = FakeConnection()
    connection.all_results = [[("topic_a", {"owner_id": "owner-a", "name": "Ada"}, 1)]]
    repository = PostgresDocumentRepository(
        "postgresql://db.example.test/app",
        "research_topics",
        value_loader=lambda value: value["name"],
        connection_factory=lambda _dsn: connection,
    )

    assert list(repository.items_for_owner("owner-a")) == [("topic_a", "Ada")]

    statement, parameters = connection.calls[0]
    assert "document->>'owner_id' = %s" in statement
    assert "expires_at" in statement.lower()
    assert parameters == ("research_topics", "owner-a")


def test_in_memory_items_for_owner_filters_expired_and_other_owners() -> None:
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    repository: InMemoryDocumentRepository[Any] = InMemoryDocumentRepository(clock=lambda: now)
    repository["mine"] = {"owner_id": "owner-a", "name": "Mine"}
    repository["expired"] = type("ExpiredDoc", (), {"owner_id": "owner-a", "expires_at": now})()
    repository["other"] = {"owner_id": "owner-b", "name": "Other"}

    assert list(repository.items_for_owner("owner-a")) == [("mine", {"owner_id": "owner-a", "name": "Mine"})]
