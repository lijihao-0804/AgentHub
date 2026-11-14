"""Rebuild the measured local runtime under a fresh .scratch directory and replay one phase."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "benchmarks/public_rag/results/published/refusal-consistency-20261114"
OPERATORS = {"job.py", "infra.py", "lifecycle.py", "measure.py", "compose.yaml"}
OPERATOR_SHA256 = "b4e8058b61d9141adc4d535167cee50ae0a8ef83c917d4fec38bc7cd147ccbc9"
ACTIONS = (
    "stage",
    "up",
    "migrate",
    "provision",
    "prepare",
    "sources",
    "freeze",
    "live",
    "retire",
    "down",
)
SCOPES = ("independent", "historical", "historical_vietnam")


def read(path):
    return json.loads(path.read_bytes())


def lf_digest(data):
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def stage(state):
    frozen = read(REFERENCE / "execution-freeze.json")
    if state.exists():
        raise FileExistsError("Staging requires an unused directory")
    bundle = REFERENCE / "operator.zip"
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != OPERATOR_SHA256:
        raise ValueError("Execution operator archive differs, including its Compose definition")
    with ZipFile(bundle) as files:
        if set(files.namelist()) != OPERATORS or len(files.namelist()) != len(OPERATORS):
            raise ValueError("Unexpected operator archive")
        for name, expected in frozen["operator_script_sha256_lf"].items():
            if lf_digest(files.read(name)) != expected:
                raise ValueError(f"Archived operator script differs: {name}")
        state.mkdir()
        for name in sorted(OPERATORS):
            (state / name).write_bytes(files.read(name))
    archive = state / "baseline-sources.zip"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            "--output",
            str(archive),
            frozen["baseline_commit"],
            ":(glob)apps/*.py",
            ":(glob)apps/*/*.py",
            "apps/api",
            "apps/worker",
            "packages",
            "migrations",
            "alembic.ini",
            "scripts",
            "benchmarks/__init__.py",
            ":(glob)benchmarks/**/*.py",
            "benchmarks/public_rag/model-route.json",
        ],
        cwd=ROOT,
        check=True,
    )
    (state / "source").mkdir()
    with ZipFile(archive) as files:
        files.extractall(state / "source")
    base = state / "source/packages/knowledge/citation_qa.py"
    if lf_digest(base.read_bytes()) != frozen["qa_source_sha256_lf"]["A"]:
        raise ValueError("Baseline reader source differs")
    destination = state / "public"
    destination.mkdir()
    for name in ("protocol.json", "exposure.json", "rubric.md", "acceptance.md"):
        data = (REFERENCE / name).read_bytes().replace(b"\r\n", b"\n")
        (destination / name).write_bytes(data)
    if lf_digest((destination / "protocol.json").read_bytes()) != frozen["protocol_sha256_lf"]:
        raise ValueError("Frozen case definition differs")
    print(json.dumps({"state": str(state), "public_results": str(destination), "model_calls": 0}))


def execute(state, action, scope):
    if action in ("up", "migrate", "down"):
        subprocess.run([sys.executable, str(state / "infra.py"), action], cwd=ROOT, check=True)
        return
    # The worker runs as a plain script. No AgentHub modules are imported before the
    # historical source root is installed. Only the new output location is rebound.
    sys.path.insert(0, str(state))
    import lifecycle

    lifecycle.PUBLIC = state / "public"
    if action == "provision":
        lifecycle.provision()
    elif action == "prepare":
        lifecycle.prepare_scope(scope)
    elif action == "retire":
        lifecycle.retire()
    else:
        import measure

        measure.PUBLIC = lifecycle.PUBLIC
        measure.PROTOCOL = lifecycle.PUBLIC / "protocol.json"
        measure.FREEZE = lifecycle.PUBLIC / "execution-freeze.json"
        {"sources": measure.sources, "freeze": measure.freeze, "live": measure.run}[action]()


def record_job(args, state):
    label = args.action + ("-" + args.scope if args.action == "prepare" else "")
    folder = state / "jobs"
    folder.mkdir(exist_ok=True)
    runner = Path.home() / ".codex/tools/job_runner.py"
    argv = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        args.action,
        "--state",
        str(state),
        "--scope",
        args.scope,
        "--worker",
    ]
    if runner.is_file():
        spec = folder / (label + "-spec.json")
        spec.write_text(
            json.dumps({"argv": argv, "cwd": str(ROOT), "timeout_seconds": 2300}), encoding="utf-8"
        )
        supervisor = subprocess.run(
            [
                sys.executable,
                str(runner),
                "run",
                "--spec",
                str(spec),
                "--job-dir",
                str(folder / label),
            ],
            cwd=ROOT,
        )
        if supervisor.returncode:
            raise SystemExit(
                read(folder / label / "status.json")["exit_code"] or supervisor.returncode
            )
    else:
        # A plain subprocess also isolates this worker from Python's -m import state.
        raise SystemExit(subprocess.run(argv, cwd=ROOT).returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument(
        "--state", required=True, type=Path, help="Fresh direct child of repository .scratch"
    )
    parser.add_argument("--scope", default="independent", choices=SCOPES)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    state = args.state.resolve()
    if state.parent != (ROOT / ".scratch").resolve():
        parser.error("--state must be a direct child of the repository .scratch directory")
    if args.action == "stage":
        stage(state)
    elif args.worker:
        execute(state, args.action, args.scope)
    else:
        record_job(args, state)


if __name__ == "__main__":
    main()
