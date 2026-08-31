from __future__ import annotations

from datetime import datetime, timezone

from app.oauth_identity import OAuthIdentity


def test_oauth_identity_hydrates_serialized_postgres_datetimes() -> None:
    created = "2026-08-31T03:25:08.505491Z"
    identity = OAuthIdentity.model_validate(
        {
            "schema_version": "v1",
            "identity_id": "oauth_identity_test1234",
            "provider": "github",
            "subject": "provider-subject",
            "account_id": "account_test1234",
            "email": "user@example.test",
            "created_at": created,
            "updated_at": created,
        }
    )

    assert identity.created_at == datetime(2026, 8, 31, 3, 25, 8, 505491, tzinfo=timezone.utc)
    assert identity.updated_at == identity.created_at
