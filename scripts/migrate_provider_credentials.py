"""Explicitly migrate legacy plaintext ProviderCredential rows to ciphertext.

Run only as a deployment operation after setting AGENTHUB_CREDENTIAL_MASTER_KEY.
The normal ModelGateway path refuses legacy plaintext unless the transitional
AGENTHUB_CREDENTIAL_ALLOW_LEGACY_PLAINTEXT flag is explicitly enabled.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.models import ProviderCredential


async def migrate() -> int:
    settings = get_settings()
    cipher = ProviderCredentialCipher.from_settings(settings)
    engine, factory = create_database(settings.database_url)
    migrated = 0
    try:
        async with factory() as session:
            credentials = list(
                (
                    await session.scalars(
                        select(ProviderCredential).where(
                            ProviderCredential.secret_ciphertext.is_(None),
                            ProviderCredential.secret.is_not(None),
                        )
                    )
                ).all()
            )
            for credential in credentials:
                assert credential.secret is not None
                credential.secret_ciphertext = cipher.encrypt(credential.secret)
                credential.secret_version = 1
                credential.secret = None
                migrated += 1
            await session.commit()
    finally:
        await engine.dispose()
    return migrated


if __name__ == "__main__":
    print(f"migrated_provider_credentials={asyncio.run(migrate())}")
