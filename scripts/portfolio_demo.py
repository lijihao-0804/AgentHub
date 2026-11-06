"""Run only the explicitly isolated, no-paid-model portfolio demonstration."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

from scripts.portfolio_runtime import API, ROOT, STATE, configure


def initialize():
    from cryptography.fernet import Fernet

    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "private-config.json"
    if path.exists():
        raise SystemExit("Existing demo state retained; do not overwrite its keys or database.")
    password = secrets.token_urlsafe(24)
    cfg = {
        "mode": "PORTFOLIO_LOCAL_ONLY",
        "env": {
            "AGENTHUB_DATABASE_URL": f"postgresql+asyncpg://portfolio:{password}@127.0.0.1:56416/agenthub_portfolio_test",
            "AGENTHUB_REDIS_URL": "redis://127.0.0.1:56417/0",
            "AGENTHUB_ENVIRONMENT": "test",
            "AGENTHUB_TESTING": "false",
            "AGENTHUB_AUTH_JWT_SECRET": secrets.token_urlsafe(48),
            "AGENTHUB_CREDENTIAL_MASTER_KEY": Fernet.generate_key().decode(),
            "AGENTHUB_RUN_EXECUTION_IN_WORKER": "true",
            "AGENTHUB_MCP_ALLOW_PRIVATE_TARGETS": "true",
            "AGENTHUB_KNOWLEDGE_WARM_MODELS_ON_START": "false",
            "AGENTHUB_BLOB_ROOT": str(STATE / "blobs"),
            "AGENTHUB_API_PROXY_TARGET": API,
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
        },
        "accounts": {
            role: {"email": f"{role}@portfolio.example.test", "password": secrets.token_urlsafe(24)}
            for role in ("operator", "approver", "viewer")
        },
    }
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    path.chmod(0o600)
    (STATE / "compose.env").write_text(f"PORTFOLIO_DB_PASSWORD={password}\n", encoding="utf-8")
    (STATE / "compose.env").chmod(0o600)
    print(f"Generated private local settings in {STATE}; no credentials printed.")


def prepare():
    configure()
    from alembic import command
    from alembic.config import Config

    from scripts.bootstrap_checkpoint import bootstrap

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    command.check(Config(str(ROOT / "alembic.ini")))
    bootstrap(os.environ["AGENTHUB_DATABASE_URL"])
    print("Explicit migrations and checkpoint bootstrap completed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "init",
            "prepare",
            "api",
            "worker",
            "beat",
            "ops",
            "web",
            "build-web",
            "seed",
            "evaluation",
            "verify",
            "begin",
            "approve",
            "deny",
        ],
    )
    parser.add_argument("--label", default="approval")
    parser.add_argument("--state-marker")
    args = parser.parse_args()
    if args.state_marker and Path(args.state_marker).resolve() != STATE.resolve():
        raise ValueError("The process marker must refer to this checkout's isolated state.")
    if args.command == "init":
        initialize()
    elif args.command == "prepare":
        prepare()
    elif args.command in {"seed", "begin", "approve", "deny", "evaluation", "verify"}:
        from scripts import portfolio_workflow

        if args.command == "seed":
            portfolio_workflow.seed()
        elif args.command == "evaluation":
            portfolio_workflow.evaluation()
        elif args.command == "verify":
            portfolio_workflow.verify()
        elif args.command == "begin":
            portfolio_workflow.begin(args.label)
        else:
            portfolio_workflow.decide(args.label, args.command)
    else:
        from scripts import portfolio_runtime

        getattr(portfolio_runtime, args.command.replace("-", "_"))()


if __name__ == "__main__":
    main()
