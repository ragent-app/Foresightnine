from __future__ import annotations

import pytest

from app import main
from app.storage import (
    InMemoryDocumentRepository,
    PostgresDocumentRepository,
    StorageConfigurationError,
    StorageMode,
)


def test_development_api_entity_maps_are_service_free_memory_repositories() -> None:
    config = main._storage_config({})
    repositories = main._build_entity_repositories(config)

    assert config.mode is StorageMode.MEMORY
    assert all(isinstance(repository, InMemoryDocumentRepository) for repository in repositories)


def test_preflight_postgres_url_names_select_lazy_typed_postgres_maps() -> None:
    config = main._storage_config(
        {
            "FORESIGHTNINE_ENV": "production",
            "FORESIGHTNINE_STORAGE_MODE": "postgres",
            "FORESIGHTNINE_POSTGRES_URL": "postgresql://db.example.test/foresightnine",
        }
    )
    repositories = main._build_entity_repositories(config)

    assert config.mode is StorageMode.POSTGRES
    assert config.dsn == "postgresql://db.example.test/foresightnine"
    assert all(isinstance(repository, PostgresDocumentRepository) for repository in repositories)
    assert repositories[3]._value_loader("7") == 7
    assert repositories[7]._value_loader("challenge-1") == "challenge-1"


def test_database_url_is_a_compatible_postgres_alias() -> None:
    config = main._storage_config(
        {
            "FORESIGHTNINE_PUBLIC_HARDENING": "true",
            "FORESIGHTNINE_STORAGE_MODE": "postgres",
            "DATABASE_URL": "postgresql://db.example.test/foresightnine",
        }
    )

    assert config.dsn == "postgresql://db.example.test/foresightnine"


@pytest.mark.parametrize(
    "environment",
    [
        {"FORESIGHTNINE_ENV": "production"},
        {"FORESIGHTNINE_ENV": "production", "FORESIGHTNINE_STORAGE_MODE": "memory"},
        {"FORESIGHTNINE_PUBLIC_HARDENING": "true"},
    ],
)
def test_production_and_public_hardening_refuse_unconfigured_or_memory_storage(
    environment: dict[str, str],
) -> None:
    with pytest.raises(StorageConfigurationError):
        main._storage_config(environment)

