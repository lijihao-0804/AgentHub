"""Explicitly migrate legacy plaintext ProviderCredential rows to ciphertext.

Run only as a deployment operation after setting AGENTHUB_CREDENTIAL_MASTER_KEY.
The normal ModelGateway path refuses legacy plaintext unless the transitional
AGENTHUB_CREDENTIAL_ALLOW_LEGACY_PLAINTEXT flag is explicitly enabled.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import func, select

from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.models import ProviderCredential


async def migrate(*, dry_run: bool = False, verify_only: bool = False) -> int:
    master_key = os.environ.get("AGENTHUB_CREDENTIAL_MASTER_KEY", "").strip()
    if not master_key:
        print(
            "AGENTHUB_CREDENTIAL_MASTER_KEY must be configured before credential migration.",
            file=sys.stderr,
        )
        return 2
    settings = get_settings()
    cipher = ProviderCredentialCipher.from_settings(settings)
    engine, factory = create_database(settings.database_url)
    migrated = 0
    try:
        async with factory() as session:
            if not verify_only:
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
                if not dry_run:
                    for credential in credentials:
                        assert credential.secret is not None
                        credential.secret_ciphertext = cipher.encrypt(credential.secret)
                        credential.secret_version = 1
                        credential.secret = None
                migrated = len(credentials)
                if not dry_run:
                    await session.commit()
            legacy_remaining = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ProviderCredential)
                    .where(
                        ProviderCredential.secret_ciphertext.is_(None),
                        ProviderCredential.secret.is_not(None),
                    )
                )
            )
            print(f"migrated_provider_credentials={migrated}")
            print(f"legacy_remaining={legacy_remaining}")
            return 1 if legacy_remaining else 0
    finally:
        await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report rows without writing")
    parser.add_argument(
        "--verify-only", action="store_true", help="only verify that no plaintext rows remain"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    raise SystemExit(
        asyncio.run(migrate(dry_run=args.dry_run, verify_only=args.verify_only))
    )
