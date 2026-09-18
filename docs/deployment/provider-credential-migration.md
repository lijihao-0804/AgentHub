# Provider credential migration

Provider credentials are stored as versioned Fernet ciphertext. Complete this sequence
before starting the API in an environment that contains legacy plaintext credentials:

1. Configure `AGENTHUB_CREDENTIAL_MASTER_KEY` in the deployment secret store. The key is
   required for this operation even when `AGENTHUB_ENVIRONMENT=local`; do not rely on the
   development fallback.
2. Apply the schema with `uv run --locked alembic upgrade head`.
3. Migrate legacy rows:

   ```powershell
   uv run --locked python -m scripts.migrate_provider_credentials
   ```

4. Verify that `legacy_remaining=0` was reported. The command exits non-zero when plaintext
   rows remain; `--verify-only` can be used as a separate deployment gate.
5. Start the API only after verification succeeds. Keep
   `AGENTHUB_CREDENTIAL_ALLOW_LEGACY_PLAINTEXT=false` (the default).

Use `--dry-run` to count rows without writing ciphertext. Migration and verification errors
never print credential plaintext or ciphertext.
