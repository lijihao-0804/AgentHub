from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet

from packages.core.config.settings import Settings, get_settings
from packages.model_gateway.credentials import (
    CREDENTIAL_CIPHERTEXT_VERSION,
    CredentialEncryptionError,
    ProviderCredentialCipher,
)
from tests.support.ae6b_queue import QUEUE_REDIS_URL_ENV, QueueWorker, queue_settings

_REDIS_URL = "redis://localhost:6379/3"
_PLAINTEXT = "ae6b-queue-credential-round-trip"


@pytest.fixture
def isolated_queue_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    # Avoid a developer .env supplying a key that the test intentionally removes.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTHUB_ENVIRONMENT", "testing")
    monkeypatch.setenv(QUEUE_REDIS_URL_ENV, _REDIS_URL)
    monkeypatch.delenv("AGENTHUB_CREDENTIAL_MASTER_KEY", raising=False)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("explicit_key", [False, True], ids=["absent", "explicit"])
def test_queue_settings_preserves_the_effective_global_credential_cipher(
    isolated_queue_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    explicit_key: bool,
) -> None:
    del isolated_queue_environment
    configured_key = Fernet.generate_key().decode("ascii") if explicit_key else None
    if configured_key is not None:
        monkeypatch.setenv("AGENTHUB_CREDENTIAL_MASTER_KEY", configured_key)
        get_settings.cache_clear()

    global_settings = get_settings()
    settings = queue_settings()

    assert settings.redis_url == _REDIS_URL
    assert settings.credential_master_key == global_settings.credential_master_key
    assert global_settings.credential_master_key == configured_key

    queue_cipher = ProviderCredentialCipher.from_settings(settings)
    global_cipher = ProviderCredentialCipher.from_settings(global_settings)
    ciphertext = queue_cipher.encrypt(_PLAINTEXT)

    assert ciphertext.startswith(f"v{CREDENTIAL_CIPHERTEXT_VERSION}:")
    assert _PLAINTEXT not in ciphertext
    assert queue_cipher.decrypt(ciphertext) == _PLAINTEXT
    assert global_cipher.decrypt(ciphertext) == _PLAINTEXT

    wrong_cipher = ProviderCredentialCipher.from_settings(
        Settings(
            environment="testing",
            testing=True,
            credential_master_key=Fernet.generate_key().decode("ascii"),
            _env_file=None,
        )
    )
    with pytest.raises(CredentialEncryptionError):
        wrong_cipher.decrypt(ciphertext)


@pytest.mark.parametrize("explicit_key", [False, True], ids=["absent", "explicit"])
def test_queue_worker_env_reconstructs_the_same_development_or_explicit_cipher(
    isolated_queue_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    explicit_key: bool,
) -> None:
    del isolated_queue_environment
    configured_key = Fernet.generate_key().decode("ascii") if explicit_key else None
    if configured_key is not None:
        monkeypatch.setenv("AGENTHUB_CREDENTIAL_MASTER_KEY", configured_key)
        get_settings.cache_clear()

    settings = queue_settings()
    worker = QueueWorker(
        settings,
        tmp_path / "worker",
        sandbox_workspaces=[],
        audit_path=tmp_path / "provider-audit.jsonl",
    )
    worker_key = worker.env["AGENTHUB_CREDENTIAL_MASTER_KEY"]
    worker_settings = Settings(
        environment="testing",
        testing=True,
        process_role="worker",
        database_url=worker.env["AGENTHUB_DATABASE_URL"],
        redis_url=worker.env["AGENTHUB_REDIS_URL"],
        credential_master_key=worker_key,
        auth_jwt_secret=worker.env["AGENTHUB_AUTH_JWT_SECRET"],
        _env_file=None,
    )

    assert worker_key == (configured_key or "")
    parent_cipher = ProviderCredentialCipher.from_settings(settings)
    worker_cipher = ProviderCredentialCipher.from_settings(worker_settings)

    parent_ciphertext = parent_cipher.encrypt(_PLAINTEXT)
    worker_ciphertext = worker_cipher.encrypt(_PLAINTEXT)
    assert worker_cipher.decrypt(parent_ciphertext) == _PLAINTEXT
    assert parent_cipher.decrypt(worker_ciphertext) == _PLAINTEXT
    assert parent_ciphertext.startswith(f"v{CREDENTIAL_CIPHERTEXT_VERSION}:")
    assert worker_ciphertext.startswith(f"v{CREDENTIAL_CIPHERTEXT_VERSION}:")


def test_production_queue_worker_without_master_key_keeps_encryption_gate(
    tmp_path,
) -> None:
    settings = Settings(
        environment="production",
        testing=False,
        process_role="worker",
        credential_master_key=None,
        auth_jwt_secret="x" * 32,
        _env_file=None,
    )

    with pytest.raises(CredentialEncryptionError) as rejected:
        QueueWorker(
            settings,
            tmp_path / "worker",
            sandbox_workspaces=[],
            audit_path=tmp_path / "provider-audit.jsonl",
        )

    assert str(rejected.value) == "Credential encryption is not configured."
