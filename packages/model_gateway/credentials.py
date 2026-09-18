"""Versioned provider credential encryption at rest."""

from __future__ import annotations

import base64
import hashlib
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config.settings import DEVELOPMENT_ENVIRONMENTS, Settings, get_settings
from packages.model_gateway.models import ProviderCredential

CREDENTIAL_CIPHERTEXT_VERSION = 1


class CredentialEncryptionError(Exception):
    """Safe local error; never includes plaintext, ciphertext, or provider details."""


class ProviderCredentialCipher:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ProviderCredentialCipher:
        settings = settings or get_settings()
        configured = settings.credential_master_key
        if not configured:
            if settings.environment.lower() not in DEVELOPMENT_ENVIRONMENTS:
                raise CredentialEncryptionError("Credential encryption is not configured.")
            configured = "agenthub-local-credential-master-key"
        try:
            decoded = base64.urlsafe_b64decode(configured.encode("ascii"))
        except (UnicodeError, ValueError):
            decoded = b""
        key = configured.encode("ascii") if len(decoded) == 32 else _derived_key(configured)
        try:
            return cls(key)
        except (TypeError, ValueError) as exc:
            raise CredentialEncryptionError("Credential encryption is not configured.") from exc

    def encrypt(self, secret: str) -> str:
        if not isinstance(secret, str) or not secret:
            raise CredentialEncryptionError("Credential secret is invalid.")
        try:
            token = self._fernet.encrypt(secret.encode("utf-8")).decode("ascii")
        except (UnicodeError, ValueError) as exc:
            raise CredentialEncryptionError("Credential encryption failed.") from exc
        return f"v{CREDENTIAL_CIPHERTEXT_VERSION}:{token}"

    def decrypt(self, ciphertext: str) -> str:
        prefix = f"v{CREDENTIAL_CIPHERTEXT_VERSION}:"
        if not isinstance(ciphertext, str) or not ciphertext.startswith(prefix):
            raise CredentialEncryptionError("Credential ciphertext is invalid.")
        try:
            return self._fernet.decrypt(ciphertext[len(prefix) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise CredentialEncryptionError(
                "Credential ciphertext could not be decrypted."
            ) from exc


def encrypt_provider_credential_secret(
    credential: ProviderCredential,
    secret: str,
    *,
    cipher: ProviderCredentialCipher,
) -> None:
    """Apply an explicit encrypted-at-rest write for create or secret rotation."""

    credential.secret_ciphertext = cipher.encrypt(secret)
    credential.secret_version = CREDENTIAL_CIPHERTEXT_VERSION
    credential.secret = None


def _derived_key(value: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode("utf-8")).digest())


async def encrypt_legacy_provider_credential(
    session: AsyncSession,
    credential_id: UUID,
    *,
    cipher: ProviderCredentialCipher,
) -> None:
    """Explicitly migrate one legacy plaintext row; never run implicitly."""

    credential = await session.get(ProviderCredential, credential_id)
    if credential is None or credential.secret is None or credential.secret_ciphertext is not None:
        raise CredentialEncryptionError("Legacy provider credential is unavailable.")
    encrypt_provider_credential_secret(credential, credential.secret, cipher=cipher)
    await session.commit()


__all__ = [
    "CREDENTIAL_CIPHERTEXT_VERSION",
    "CredentialEncryptionError",
    "ProviderCredentialCipher",
    "encrypt_provider_credential_secret",
    "encrypt_legacy_provider_credential",
]
