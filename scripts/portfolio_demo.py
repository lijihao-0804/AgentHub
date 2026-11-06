"""Run only the explicitly isolated, no-paid-model portfolio demonstration."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

from scripts.portfolio_runtime import API, ROOT, STATE, configure

CONFIG_FILES = ("private-config.json", "compose.env")
PERMISSIONS_SCRIPT = ROOT / "infra/portfolio-permissions.ps1"


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _assert_state_location() -> None:
    expected = ROOT / ".scratch/portfolio-closeout"
    if _normalized_path(STATE) != _normalized_path(expected):
        raise ValueError("Portfolio state must be the dedicated checkout-scoped directory.")

    current = Path(os.path.abspath(STATE))
    workspace = Path(os.path.abspath(ROOT))
    while True:
        if _is_reparse_point(current):
            raise ValueError("Refusing to harden a state path that traverses a reparse point.")
        if current == workspace:
            break
        if current == current.parent:
            raise ValueError("Portfolio state escaped the checkout.")
        current = current.parent


def _assert_no_state_tree_reparse_points() -> None:
    try:
        metadata = STATE.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Portfolio state must be a directory.")

    pending = [STATE]
    while pending:
        directory = pending.pop()
        if _is_reparse_point(directory):
            raise ValueError("Refusing to harden a state tree containing a reparse point.")
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if _is_reparse_point(path):
                    raise ValueError("Refusing to harden a state tree containing a reparse point.")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)


def _powershell_executable() -> str:
    for name in ("pwsh.exe", "powershell.exe"):
        executable = shutil.which(name)
        if executable:
            return executable
    raise RuntimeError("PowerShell is required to apply a protected Windows DACL.")


def _secure_windows_acl() -> None:
    result = subprocess.run(
        [
            _powershell_executable(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(PERMISSIONS_SCRIPT),
            "-WorkspaceRoot",
            str(ROOT),
            "-StatePath",
            str(STATE),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        raise RuntimeError(
            "Portfolio state ACL hardening failed"
            + (f": {detail}" if detail else f" with exit code {result.returncode}.")
        )


def secure_state() -> None:
    """Harden the existing checkout-scoped state without reading or rewriting secrets."""

    _assert_state_location()
    _assert_no_state_tree_reparse_points()
    if os.name == "nt":
        _secure_windows_acl()
        print(f"Protected portfolio state ACL verified at {STATE}.")
        return

    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_reparse_point(STATE):
        raise ValueError("Refusing to harden a state directory that is a reparse point.")
    STATE.chmod(0o700)
    for name in CONFIG_FILES:
        path = STATE / name
        if not os.path.lexists(path):
            continue
        if _is_reparse_point(path):
            raise ValueError("Refusing to harden a configuration file that is a reparse point.")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("A portfolio configuration path is not a regular file.")
        path.chmod(0o600)
    print(f"Protected portfolio state permissions verified at {STATE}.")


def initialize():
    from cryptography.fernet import Fernet

    path = STATE / "private-config.json"
    if path.exists():
        raise SystemExit("Existing demo state retained; do not overwrite its keys or database.")
    secure_state()
    if path.exists():
        raise SystemExit("Existing demo state appeared; do not overwrite its keys or database.")
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
    compose_path = STATE / "compose.env"
    compose_path.write_text(f"PORTFOLIO_DB_PASSWORD={password}\n", encoding="utf-8")
    secure_state()
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
            "secure-state",
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
    if args.state_marker and _normalized_path(args.state_marker) != _normalized_path(STATE):
        raise ValueError("The process marker must refer to this checkout's isolated state.")
    if args.command == "init":
        initialize()
    elif args.command == "secure-state":
        secure_state()
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
