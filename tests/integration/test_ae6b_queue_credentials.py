from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from packages.agent_runtime.models import AgentVersion
from packages.core.config.settings import get_settings
from packages.model_gateway.credentials import (
    CREDENTIAL_CIPHERTEXT_VERSION,
    ProviderCredentialCipher,
)
from packages.model_gateway.models import ProviderCredential
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
from tests.support.ae6b_queue import (
    QUEUE_REDIS_URL_ENV,
    queue_settings,
    seed_queue_agent,
)

__all__ = ["db_factory", "migrated_database"]
pytestmark = pytest.mark.integration

_REDIS_URL = "redis://localhost:6379/3"
_EXPECTED_SEED_SECRET = "controlled-not-a-provider-key"


@pytest.mark.asyncio
async def test_missing_global_master_key_publishes_with_the_shared_dev_cipher(
    db_factory,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_key = os.environ.get("AGENTHUB_CREDENTIAL_MASTER_KEY")
    with monkeypatch.context() as patch:
        # The migrated_database fixture owns the test database URL. Isolating the
        # working directory removes any developer .env while retaining that URL.
        patch.chdir(tmp_path)
        patch.setenv("AGENTHUB_ENVIRONMENT", "testing")
        patch.setenv("AGENTHUB_TESTING", "true")
        patch.setenv(QUEUE_REDIS_URL_ENV, _REDIS_URL)
        patch.delenv("AGENTHUB_CREDENTIAL_MASTER_KEY", raising=False)
        get_settings.cache_clear()
        try:
            settings = queue_settings()
            assert settings.credential_master_key is None
            assert get_settings().credential_master_key is None

            agent = await seed_queue_agent(db_factory, settings)

            async with db_factory() as session:
                credentials = list(
                    await session.scalars(
                        select(ProviderCredential).where(
                            ProviderCredential.workspace_id == agent.workspace_id
                        )
                    )
                )
                assert len(credentials) == 1
                credential = credentials[0]
                version = await session.get(AgentVersion, agent.agent_version_id)

                assert credential.secret is None
                assert credential.secret_ciphertext is not None
                assert credential.secret_version == CREDENTIAL_CIPHERTEXT_VERSION
                assert credential.secret_ciphertext.startswith(f"v{CREDENTIAL_CIPHERTEXT_VERSION}:")
                assert version is not None
                assert version.workspace_id == agent.workspace_id
                assert version.resolved_spec["model"]["credential_ref"] == str(credential.id)

                ciphertext = credential.secret_ciphertext

            assert (
                ProviderCredentialCipher.from_settings(get_settings()).decrypt(ciphertext)
                == _EXPECTED_SEED_SECRET
            )
        finally:
            # The cached Settings object was built from the temporary environment.
            get_settings.cache_clear()

    assert os.environ.get("AGENTHUB_CREDENTIAL_MASTER_KEY") == original_key
    get_settings.cache_clear()
