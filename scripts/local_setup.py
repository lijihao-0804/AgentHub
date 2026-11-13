"""Initialize ordinary development secrets and explicitly prepare both database schemas."""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from pydantic import ValidationError

from packages.core.config.settings import (
    DEFAULT_AUTH_JWT_SECRET,
    EXAMPLE_AUTH_JWT_SECRET,
    Settings,
)
from scripts.bootstrap_checkpoint import bootstrap

JWT = "AGENTHUB_AUTH_JWT_SECRET"
MASTER_KEY = "AGENTHUB_CREDENTIAL_MASTER_KEY"


def check_env(path: Path) -> None:
    if not path.is_file():
        raise ValueError("Missing .env. Run the init command for a new installation.")
    values = dotenv_values(path)
    file_master = values.get(MASTER_KEY)
    master_key = os.environ.get(MASTER_KEY, file_master) or ""
    if file_master and master_key != file_master:
        raise ValueError(
            f"{MASTER_KEY} differs between .env and this shell. Restore the key used with "
            "this database and remove the conflicting override before starting."
        )
    jwt = os.environ.get(JWT, values.get(JWT)) or ""
    if len(jwt.strip()) < 32 or jwt in {DEFAULT_AUTH_JWT_SECRET, EXAMPLE_AUTH_JWT_SECRET}:
        raise ValueError(f"Set a private, randomly generated {JWT} of at least 32 characters.")
    if not master_key.strip():
        raise ValueError(
            f"Missing {MASTER_KEY}. Restore the key used with this database. "
            "For a new, empty database follow the fresh installation steps in README.md."
        )
    if jwt == master_key:
        raise ValueError(f"{JWT} and {MASTER_KEY} must be independent.")


def initialize_env(directory: Path) -> None:
    path = directory / ".env"
    if not path.exists():
        template = (directory / ".env.example").read_text(encoding="utf-8")
        keys = {JWT: secrets.token_urlsafe(48), MASTER_KEY: Fernet.generate_key().decode("ascii")}
        for name, value in keys.items():
            # Store only keys created here. A secret supplied by the shell stays
            # external; each subsequent shell must supply it again.
            if name not in os.environ:
                template = template.replace(f"{name}=\n", f"{name}={value}\n", 1)
        # Publish only a fully written file and never replace an existing key,
        # including when two copies of setup are run at once.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".env.", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            # The temporary file already has private permissions on POSIX.
            temporary_path.write_text(template, encoding="utf-8")
            check_env(temporary_path)
            try:
                if os.name == "nt":
                    temporary_path.rename(path)  # Windows rename() never replaces an existing file.
                else:
                    os.link(temporary_path, path)
            except FileExistsError:
                pass
        finally:
            temporary_path.unlink(missing_ok=True)
    check_env(path)
    print("Local .env is configured.")


def prepare_database() -> None:
    # A deployment step, called by both launch modes before serving requests.
    # Both operations consult the same database setting, including custom URLs.
    database_url = Settings().database_url
    command.upgrade(Config("alembic.ini"), "head")
    bootstrap(database_url)
    print("AgentHub migrations and LangGraph checkpoint schema are ready.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "check-env", "prepare"))
    action = parser.parse_args().action
    try:
        if action == "init":
            initialize_env(Path.cwd())
        elif action == "check-env":
            check_env(Path(".env"))
        else:
            prepare_database()
    except ValidationError as exc:
        # Settings errors can include the entire input mapping, including secrets.
        fields = ", ".join(".".join(map(str, error["loc"])) or "settings" for error in exc.errors())
        print(f"Invalid AgentHub configuration ({fields}).", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
